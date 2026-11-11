"""Fail-closed local intent controller for WhatsApp, Signal and Slack.

Read commands are local and passive. Mutation preparation is offline. The
public acknowledgement literal used by execute-action is only an accidental-
invocation guard: it is not a secret, signature, capability, or proof of user
consent, and it cannot defend against another process running as the same OS
user. Hermes MUST enforce a native allow-once approval at its tool boundary
before invoking execute-action for every external mutation. execute-action is
therefore a CLI/API boundary for native orchestration, not a model tool.

The controller provides an atomic local at-most-once dispatch claim and never
automatically replays ambiguous operations. It makes no end-to-end delivery
uniqueness guarantee; that would require provider idempotency support.
Slack mutations are rendered only as unexecuted plans for a separate approved,
authenticated connector boundary; this module holds no Slack credentials.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import sys
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import quote, urlsplit

from .context_inbox import (
    DEFAULT_CONTEXT_DB,
    _chmod_private_file,
    _chmod_sqlite_sidecars,
    _prepare_private_file_parent,
    _reject_symlink_sqlite_paths,
)

DEFAULT_ACTIONS_DB = Path("~/.hermes/second-brain/personal-messaging-actions.sqlite3").expanduser()

DEFAULT_WHATSAPP_URL = "http://127.0.0.1:3000"
DEFAULT_SIGNAL_URL = "http://127.0.0.1:8080"

#: Opt-in escape hatch for non-loopback bridges. Off by default on purpose:
#: a personal messaging bridge reachable off-host is an account takeover.
ALLOW_NON_LOOPBACK_ENV = "HERMES_PM_ALLOW_NON_LOOPBACK"
SIGNAL_ACCOUNT_ENV = "SIGNAL_ACCOUNT"

#: Provenance markers written by the read-only Signal Desktop history collector
#: (see ``signal_desktop.py``). Events carrying either marker are a passive
#: mirror of Signal Desktop with no send capability, so they must never become a
#: messaging action target. Kept in sync with ``signal_desktop`` deliberately by
#: value rather than import to avoid a read-layer -> collector dependency.
SIGNAL_DESKTOP_ACCOUNT = "signal-desktop"
SIGNAL_DESKTOP_SOURCE_PATH = "signal-desktop"

PLATFORMS = ("whatsapp", "signal", "slack")
ACTIONS = (
    "send",
    "send-file",
    "reply",
    "react",
    "unreact",
    "edit",
    "delete",
    "mark-read",
    "typing-start",
    "typing-stop",
)
#: Canonical directions. ``inbound`` is a message we received; ``outbound`` is a
#: message we sent. The legacy ``incoming``/``outgoing`` spellings are accepted
#: only as CLI/input aliases and canonicalised immediately.
DIRECTIONS = ("inbound", "outbound", "unknown")
_DIRECTION_ALIASES = {
    "inbound": "inbound",
    "incoming": "inbound",
    "received": "inbound",
    "in": "inbound",
    "outbound": "outbound",
    "outgoing": "outbound",
    "sent": "outbound",
    "out": "outbound",
}


def canonical_direction(value: str | None) -> str:
    """Map any accepted direction spelling to a canonical direction."""
    return _DIRECTION_ALIASES.get((value or "").strip().lower(), "unknown")


def direction_is_from_me(value: str | None) -> bool:
    """Whether a message with ``value`` direction was sent by this account.

    Used to derive the bridge's ``fromMe`` flag from event provenance instead of
    trusting a caller-supplied boolean. An ``unknown`` direction is refused: we
    must never guess whether a message is ours before deleting/reacting to it.
    """
    canonical = canonical_direction(value)
    if canonical == "inbound":
        return False
    if canonical == "outbound":
        return True
    raise ValidationError(
        "cannot determine message ownership from an unknown direction; "
        "prepare from a Context Inbox event with a known direction",
        code="ambiguous_direction",
    )

# Public, non-secret acknowledgement used only to catch accidental invocation.
# User authorization belongs to Hermes' native allow-once approval boundary.
ACKNOWLEDGEMENT_TOKEN = "user-request-acknowledged"

DEFAULT_TTL_SECONDS = 300
MAX_TTL_SECONDS = 900
DEFAULT_RETENTION_SECONDS = 24 * 60 * 60

SEARCH_LIMIT_MAX = 200
SEARCH_LIMIT_DEFAULT = 25
CONVERSATION_LIMIT_MAX = 200
CONVERSATION_LIMIT_DEFAULT = 25
SNIPPET_CHARS = 280
QUOTE_CHARS = 4096
MAX_MESSAGE_CHARS = 16000
MAX_MESSAGE_FILE_BYTES = MAX_MESSAGE_CHARS * 4
MAX_EMOJI_CHARS = 32
MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024

HTTP_TIMEOUT_SECONDS = 15.0
PROBE_TIMEOUT_SECONDS = 1.5
MAX_RESPONSE_BYTES = 256 * 1024

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
WHATSAPP_JID_RE = re.compile(
    r"^(?:"
    r"\d{1,64}(?::\d{1,5})?@(?:s\.whatsapp\.net|c\.us|lid)"
    r"|\d{1,64}(?:-\d{1,64})?@g\.us"
    r"|(?:status|\d{1,64})@broadcast"
    r"|\d{1,64}@newsletter"
    r")$"
)
WHATSAPP_ORDINARY_JID_RE = re.compile(
    r"^(?:"
    r"\d{1,64}(?::\d{1,5})?@(?:s\.whatsapp\.net|c\.us|lid)"
    r"|\d{1,64}(?:-\d{1,64})?@g\.us"
    r")$"
)
WHATSAPP_STANZA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~+=:/-]{0,127}$")
WHATSAPP_PARTICIPANT_RE = re.compile(
    r"^\d{1,64}(?::\d{1,5})?@(s\.whatsapp\.net|c\.us|lid)$"
)
SLACK_CHANNEL_ID_RE = re.compile(r"^[CDG][A-Z0-9]{8,20}$")
SLACK_TS_RE = re.compile(r"^[1-9]\d{9,15}\.\d{6}$")
SLACK_REACTION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_+-]{0,99}$")
SIGNAL_GROUP_RE = re.compile(r"^group:[A-Za-z0-9+/=_-]{4,120}$")
SIGNAL_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
AUDIT_DETAIL_CODE_RE = re.compile(r"^[A-Za-z0-9_.:=/-]{0,120}$")


class MessagingError(Exception):
    """Base error carrying a stable machine-readable code."""

    code = "error"

    def __init__(self, message: str, code: str | None = None, **details: Any) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": "error", "code": self.code, "error": str(self)}
        payload.update(self.details)
        return payload


class ValidationError(MessagingError):
    code = "invalid_request"


class AcknowledgementError(MessagingError):
    code = "acknowledgement_required"


class IntentStateError(MessagingError):
    code = "intent_state_invalid"


class AmbiguousTargetError(MessagingError):
    code = "ambiguous_conversation"


class UnsupportedActionError(MessagingError):
    code = "unsupported_action"


class TransportPreflightError(MessagingError):
    """Deterministic failure before the request could reach the bridge."""

    code = "transport_unavailable"


class TransportAmbiguousError(MessagingError):
    """The request may or may not have been delivered. Never replay these."""

    code = "transport_uncertain"


class RemoteRejectedError(MessagingError):
    """The bridge answered and deterministically refused the request."""

    code = "remote_rejected"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> dt.datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _epoch_millis(value: str) -> int | None:
    """Signal addresses messages by their send timestamp in milliseconds."""
    text = (value or "").strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        # Heuristic: 10-digit values are seconds, 13-digit values already ms.
        return number * 1000 if number < 10_000_000_000 else number
    parsed = _parse_iso(text)
    if parsed is None:
        return None
    return int(parsed.timestamp() * 1000)


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def _like_pattern(query: str) -> str:
    r"""Build a LIKE pattern where user input cannot inject wildcards.

    ``%`` and ``_`` are wildcards in SQL LIKE; a user searching for "50%" must
    not accidentally match everything. Escaped with a literal backslash, which
    the queries declare via ``ESCAPE '\'``.
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _bounded_int(value: Any, default: int, maximum: int, name: str) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be an integer")
    if number < 1:
        raise ValidationError(f"{name} must be >= 1")
    return min(number, maximum)


def _mask_account(account: str) -> str:
    """Never print a full phone number, not even into a local log."""
    text = (account or "").strip()
    if not text:
        return ""
    tail = text[-2:] if len(text) >= 2 else text
    return f"…{tail}"


def _sanitize_detail(text: str, limit: int = 200) -> str:
    collapsed = " ".join(str(text or "").split())
    collapsed = CONTROL_CHARS_RE.sub("", collapsed)
    return collapsed[:limit]


# --------------------------------------------------------------------------
# Context Inbox read layer (read-only)
# --------------------------------------------------------------------------

EVENT_COLUMNS = (
    "event_id",
    "platform",
    "account",
    "workspace",
    "conversation_id",
    "conversation_name",
    "conversation_type",
    "sender_id",
    "sender_display_name",
    "direction",
    "message_ts",
    "received_ts",
    "source_message_id",
    "thread_id",
)


def open_context_db(path: Path) -> sqlite3.Connection:
    """Open the Context Inbox read-only so no command can corrupt live state."""
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise MessagingError(
            f"context database not found: {resolved}",
            code="context_db_missing",
            path=str(resolved),
        )
    uri = f"file:{quote(str(resolved))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise MessagingError(
            f"cannot open context database read-only: {_sanitize_detail(exc)}",
            code="context_db_unreadable",
            path=str(resolved),
        ) from exc
    conn.row_factory = sqlite3.Row
    return conn


def _event_row_to_dict(row: sqlite3.Row, *, body: str | None = None) -> dict[str, Any]:
    data = {column: row[column] for column in EVENT_COLUMNS}
    attachments: list[Any] = []
    try:
        parsed = json.loads(row["attachment_metadata_json"] or "[]")
        if isinstance(parsed, list):
            attachments = parsed
    except (json.JSONDecodeError, TypeError, IndexError):
        attachments = []
    data["attachment_count"] = len(attachments)
    if body is not None:
        data["body"] = body
    return data


def _is_search_only_signal_source(
    platform: str | None, account: str | None, source_path: str | None
) -> bool:
    """Whether a Signal event is a passive Signal Desktop history mirror.

    Signal Desktop history rows carry no action binding, so the generic
    ``action_target_id or conversation_id`` fallback would otherwise turn a
    conversation id that merely *resembles* an E.164 number or a UUID into a
    live send target. Recognise them by their collector provenance so a mirror
    row can never be mutated.
    """
    if (platform or "").strip().lower() != "signal":
        return False
    return (
        (account or "").strip() == SIGNAL_DESKTOP_ACCOUNT
        or (source_path or "").strip() == SIGNAL_DESKTOP_SOURCE_PATH
    )


def _reject_search_only_source(
    platform: str | None, account: str | None, source_path: str | None
) -> None:
    """Fail closed at the action boundary for Signal Desktop history events."""
    if _is_search_only_signal_source(platform, account, source_path):
        raise ValidationError(
            "Signal Desktop history is search-only and cannot be a messaging "
            "action target; send from live signal-cli provenance instead",
            code="search_only_source",
        )


def search_events(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    platform: str | None = None,
    conversation: str | None = None,
    sender: str | None = None,
    direction: str | None = None,
    since: str | None = None,
    limit: int = SEARCH_LIMIT_DEFAULT,
) -> dict[str, Any]:
    limit = _bounded_int(limit, SEARCH_LIMIT_DEFAULT, SEARCH_LIMIT_MAX, "limit")
    clauses: list[str] = []
    params: list[Any] = []

    if query:
        clauses.append(
            "(body LIKE ? ESCAPE '\\' OR conversation_name LIKE ? ESCAPE '\\' "
            "OR sender_display_name LIKE ? ESCAPE '\\')"
        )
        pattern = _like_pattern(query)
        params.extend([pattern, pattern, pattern])
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    if conversation:
        clauses.append("(conversation_id = ? OR conversation_name LIKE ? ESCAPE '\\')")
        params.extend([conversation, _like_pattern(conversation)])
    if sender:
        clauses.append("(sender_id = ? OR sender_display_name LIKE ? ESCAPE '\\')")
        params.extend([sender, _like_pattern(sender)])
    if direction:
        canonical = canonical_direction(direction)
        if canonical == "unknown" and direction.strip().lower() != "unknown":
            raise ValidationError(f"direction must be one of {', '.join(DIRECTIONS)}")
        # Match the canonical spelling plus any legacy aliases that may still be
        # persisted in older rows, so a filter is not silently empty on old data.
        spellings = sorted({canonical} | {a for a, c in _DIRECTION_ALIASES.items() if c == canonical})
        placeholders = ",".join("?" for _ in spellings)
        clauses.append(f"direction IN ({placeholders})")
        params.extend(spellings)
    if since:
        parsed = _parse_iso(since)
        if parsed is None:
            raise ValidationError("since must be an ISO-8601 timestamp")
        clauses.append("message_ts >= ?")
        params.append(_iso(parsed))

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT {', '.join(EVENT_COLUMNS)}, body, attachment_metadata_json "
        f"FROM context_events{where} ORDER BY message_ts DESC, event_id DESC LIMIT ?"
    )
    rows = conn.execute(sql, (*params, limit)).fetchall()
    results = [_event_row_to_dict(row) | {"snippet": _snippet(row["body"])} for row in rows]
    return {
        "status": "ok",
        "count": len(results),
        "limit": limit,
        "truncated": len(results) == limit,
        "results": results,
    }


def list_conversations(
    conn: sqlite3.Connection,
    *,
    platform: str | None = None,
    account: str | None = None,
    workspace: str | None = None,
    query: str = "",
    limit: int = CONVERSATION_LIMIT_DEFAULT,
) -> dict[str, Any]:
    limit = _bounded_int(limit, CONVERSATION_LIMIT_DEFAULT, CONVERSATION_LIMIT_MAX, "limit")
    clauses: list[str] = []
    params: list[Any] = []
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    if account is not None:
        clauses.append("account = ?")
        params.append(account)
    if workspace is not None:
        clauses.append("workspace = ?")
        params.append(workspace)
    if query:
        clauses.append("(conversation_name LIKE ? ESCAPE '\\' OR conversation_id LIKE ? ESCAPE '\\')")
        pattern = _like_pattern(query)
        params.extend([pattern, pattern])
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT platform, account, workspace, conversation_id, MAX(conversation_name) AS conversation_name, "
        "MAX(conversation_type) AS conversation_type, COUNT(*) AS message_count, "
        "MAX(message_ts) AS last_message_ts "
        f"FROM context_events{where} GROUP BY platform, account, workspace, conversation_id "
        "ORDER BY last_message_ts DESC LIMIT ?"
    )
    rows = conn.execute(sql, (*params, limit)).fetchall()

    conversations: list[dict[str, Any]] = []
    for row in rows:
        latest = conn.execute(
            "SELECT event_id, sender_id, sender_display_name, direction, body, message_ts, "
            "source_message_id FROM context_events WHERE platform = ? AND account = ? "
            "AND workspace = ? AND conversation_id = ? "
            "ORDER BY message_ts DESC, event_id DESC LIMIT 1",
            (row["platform"], row["account"], row["workspace"], row["conversation_id"]),
        ).fetchone()
        conversations.append(
            {
                "platform": row["platform"],
                "account": row["account"],
                "workspace": row["workspace"],
                "conversation_id": row["conversation_id"],
                "conversation_name": row["conversation_name"] or "",
                "conversation_type": row["conversation_type"] or "",
                "message_count": row["message_count"],
                "last_message_ts": row["last_message_ts"] or "",
                "latest": {
                    "event_id": latest["event_id"],
                    "sender_id": latest["sender_id"],
                    "sender_display_name": latest["sender_display_name"],
                    "direction": latest["direction"],
                    "snippet": _snippet(latest["body"]),
                    "message_ts": latest["message_ts"],
                    "source_message_id": latest["source_message_id"],
                }
                if latest is not None
                else None,
            }
        )
    return {
        "status": "ok",
        "count": len(conversations),
        "limit": limit,
        "truncated": len(conversations) == limit,
        "conversations": conversations,
    }


def get_event(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    include_raw: bool = False,
    for_action: bool = False,
) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT {', '.join(EVENT_COLUMNS)}, body, attachment_metadata_json, raw_json, "
        "action_target_id, action_account_id, source_path "
        "FROM context_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise MessagingError(f"event not found: {event_id}", code="event_not_found")
    # The full body is included: this is a local, user-invoked lookup of the
    # user's own message store. raw_json stays out unless explicitly asked for,
    # because it can carry provider metadata nobody asked to surface.
    event = _event_row_to_dict(row, body=row["body"])
    # Collector provenance travels with the event so the action boundary can
    # fail closed on search-only Signal Desktop history rows.
    event["source_path"] = row["source_path"]
    try:
        event["attachments"] = json.loads(row["attachment_metadata_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        event["attachments"] = []
    if include_raw:
        event["raw_json"] = row["raw_json"]
    if for_action:
        event["_action_target_id"] = row["action_target_id"]
        event["_action_account_id"] = row["action_account_id"]
    return {"status": "ok", "event": event}


def resolve_conversation(
    conn: sqlite3.Connection,
    platform: str,
    conversation: str,
    *,
    account: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Resolve a conversation id or human name to exactly one conversation.

    Ambiguity is an error, not a coin flip: picking the wrong "Family" chat
    means sending a private message to the wrong group.
    """
    identity_clauses = ["platform = ?"]
    identity_params: list[Any] = [platform]
    if account is not None:
        identity_clauses.append("account = ?")
        identity_params.append(account)
    if workspace is not None:
        identity_clauses.append("workspace = ?")
        identity_params.append(workspace)
    identity_where = " AND ".join(identity_clauses)
    exact_rows = conn.execute(
        "SELECT platform, account, workspace, conversation_id, "
        "MAX(action_target_id) AS action_target_id, MAX(action_account_id) AS action_account_id, "
        "MAX(source_path) AS source_path, "
        "MAX(conversation_name) AS conversation_name, MAX(conversation_type) AS conversation_type, "
        "MAX(message_ts) AS last_message_ts FROM context_events WHERE "
        f"{identity_where} AND conversation_id = ? "
        "GROUP BY platform, account, workspace, conversation_id ORDER BY last_message_ts DESC LIMIT 11",
        (*identity_params, conversation),
    ).fetchall()
    if len(exact_rows) == 1:
        exact = exact_rows[0]
        _reject_search_only_source(platform, exact["account"], exact["source_path"])
        return {
            "account": exact["account"],
            "workspace": exact["workspace"],
            "conversation_id": exact["conversation_id"],
            "conversation_name": exact["conversation_name"] or "",
            "conversation_type": exact["conversation_type"] or "",
            "_action_target_id": exact["action_target_id"] or exact["conversation_id"],
            "_action_account_id": exact["action_account_id"] or "",
        }
    if len(exact_rows) > 1:
        rows = exact_rows
    else:
        rows = conn.execute(
            "SELECT platform, account, workspace, conversation_id, "
            "MAX(action_target_id) AS action_target_id, MAX(action_account_id) AS action_account_id, "
            "MAX(source_path) AS source_path, "
            "MAX(conversation_name) AS conversation_name, MAX(conversation_type) AS conversation_type, "
            "MAX(message_ts) AS last_message_ts FROM context_events WHERE "
            f"{identity_where} AND conversation_name LIKE ? ESCAPE '\\' "
            "GROUP BY platform, account, workspace, conversation_id "
            "ORDER BY last_message_ts DESC LIMIT 11",
            (*identity_params, _like_pattern(conversation)),
        ).fetchall()

    if not rows:
        raise MessagingError(
            f"no {platform} conversation matches {conversation!r}",
            code="conversation_not_found",
        )
    if len(rows) > 1:
        raise AmbiguousTargetError(
            f"{len(rows)} {platform} conversations match {conversation!r}; pass an explicit --target",
            candidates=[
                {
                    "conversation_id": row["conversation_id"],
                    "account": row["account"],
                    "workspace": row["workspace"],
                    "conversation_name": row["conversation_name"] or "",
                    "conversation_type": row["conversation_type"] or "",
                    "last_message_ts": row["last_message_ts"] or "",
                }
                for row in rows[:10]
            ],
        )
    row = rows[0]
    _reject_search_only_source(platform, row["account"], row["source_path"])
    return {
        "account": row["account"],
        "workspace": row["workspace"],
        "conversation_id": row["conversation_id"],
        "conversation_name": row["conversation_name"] or "",
        "conversation_type": row["conversation_type"] or "",
        "_action_target_id": row["action_target_id"] or row["conversation_id"],
        "_action_account_id": row["action_account_id"] or "",
    }


# --------------------------------------------------------------------------
# target validation
# --------------------------------------------------------------------------


def normalize_target(platform: str, target: str) -> str:
    text = (target or "").strip()
    if not text:
        raise ValidationError("target is required")
    if CONTROL_CHARS_RE.search(text) or len(text) > 200:
        raise ValidationError("target contains invalid characters")

    if platform == "whatsapp":
        if WHATSAPP_JID_RE.fullmatch(text):
            return text
        digits = text[1:] if text.startswith("+") else text
        if digits.isdigit() and 6 <= len(digits) <= 15:
            return f"{digits}@s.whatsapp.net"
        raise ValidationError(
            "whatsapp target must be a JID (…@s.whatsapp.net, …@g.us, …@lid) or a phone number"
        )

    if platform == "signal":
        if E164_RE.fullmatch(text) or SIGNAL_GROUP_RE.fullmatch(text) or SIGNAL_UUID_RE.fullmatch(text):
            return text
        raise ValidationError("signal target must be E.164, UUID, or group:<group_id>")

    if platform == "slack":
        if SLACK_CHANNEL_ID_RE.fullmatch(text):
            return text
        raise ValidationError("slack target must be a channel/conversation id")

    raise ValidationError(f"unknown platform: {platform}")


def signal_target_params(target: str) -> dict[str, Any]:
    if target.startswith("group:"):
        return {"groupId": target[len("group:") :]}
    return {"recipient": [target]}


# --------------------------------------------------------------------------
# loopback HTTP transport
# --------------------------------------------------------------------------


def assert_loopback(url: str, *, allow_override: bool | None = None) -> str:
    """Validate a bridge base URL and pin localhost to a literal loopback IP."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValidationError("bridge url is malformed", code="invalid_bridge_url") from exc
    if parts.scheme != "http":
        raise ValidationError("bridge url must use plain HTTP", code="invalid_bridge_url")
    if parts.username is not None or parts.password is not None:
        raise ValidationError("bridge url must not contain credentials", code="invalid_bridge_url")
    if parts.query or parts.fragment:
        raise ValidationError("bridge url must not contain a query or fragment", code="invalid_bridge_url")
    if parts.path not in ("", "/"):
        raise ValidationError("bridge url must not contain a path", code="invalid_bridge_url")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValidationError("bridge url requires a host", code="invalid_bridge_url")

    pinned_host = host
    loopback = False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        loopback = address.is_loopback
    elif host == "localhost":
        try:
            answers = socket.getaddrinfo(host, port or 80, type=socket.SOCK_STREAM)
            addresses = {ipaddress.ip_address(answer[4][0]) for answer in answers}
        except (OSError, ValueError) as exc:
            raise ValidationError(
                "localhost could not be resolved safely", code="invalid_bridge_url"
            ) from exc
        if not addresses or not all(address.is_loopback for address in addresses):
            raise ValidationError(
                "localhost resolved to a non-loopback address", code="non_loopback_refused"
            )
        loopback = True
        ipv4 = sorted(str(address) for address in addresses if address.version == 4)
        pinned_host = ipv4[0] if ipv4 else sorted(str(address) for address in addresses)[0]

    if loopback:
        rendered_host = f"[{pinned_host}]" if ":" in pinned_host else pinned_host
        return f"http://{rendered_host}{':' + str(port) if port is not None else ''}"
    if allow_override is None:
        allow_override = os.environ.get(ALLOW_NON_LOOPBACK_ENV, "") == "1"
    if allow_override:
        # Deliberately unsafe escape hatch for explicitly configured remote
        # development bridges. URL-shape checks above still apply.
        return f"http://{parts.netloc}"
    raise ValidationError(
        f"refusing non-loopback bridge host {host!r}; set {ALLOW_NON_LOOPBACK_ENV}=1 to override",
        code="non_loopback_refused",
    )


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn redirects into HTTPError; never forward a mutation elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class LoopbackHttpClient:
    """Minimal stdlib JSON-over-HTTP client with strict bounds.

    Failure classification matters more than the happy path here. A caller
    must be able to tell "we never reached the bridge" (safe to prepare a new
    intent) from "the bridge may have sent the message" (never replay).
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        allow_non_loopback: bool | None = None,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
    ) -> None:
        self.base_url = assert_loopback(base_url, allow_override=allow_non_loopback)
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        if opener is not None:
            self._opener = opener
        else:
            # Ignore HTTP(S)_PROXY and refuse redirects. The validated/pinned
            # literal endpoint is the only address this client may contact.
            built = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                NoRedirectHandler(),
            )
            self._opener = lambda request, timeout: built.open(request, timeout=timeout)

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        body = _canonical_json(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            response = self._opener(request, self.timeout)
        except urllib.error.HTTPError as exc:
            raise TransportAmbiguousError(
                "bridge returned an HTTP error after dispatch", http_status=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise TransportAmbiguousError("bridge request timed out") from exc
            if isinstance(reason, ConnectionRefusedError):
                raise TransportPreflightError("bridge refused the connection") from exc
            raise TransportAmbiguousError("bridge request failed after dispatch began") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise TransportAmbiguousError("bridge request timed out") from exc
        except OSError as exc:
            if isinstance(exc, ConnectionRefusedError):
                raise TransportPreflightError("bridge refused the connection") from exc
            raise TransportAmbiguousError("bridge request failed after dispatch began") from exc

        try:
            with response:
                raw = response.read(self.max_response_bytes + 1)
        except (socket.timeout, TimeoutError) as exc:
            raise TransportAmbiguousError("bridge response timed out") from exc
        except OSError as exc:
            raise TransportAmbiguousError(f"bridge response failed: {_sanitize_detail(exc)}") from exc

        if len(raw) > self.max_response_bytes:
            raise TransportAmbiguousError("bridge response exceeded size bound")
        if not raw.strip():
            raise TransportAmbiguousError("bridge returned an empty response")
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise TransportAmbiguousError("bridge returned a non-JSON response") from exc
        if not isinstance(parsed, dict):
            raise TransportAmbiguousError("bridge returned a non-object response")
        return parsed


# --------------------------------------------------------------------------
# intent payload construction
# --------------------------------------------------------------------------

_REQUIRES_MESSAGE = ("send", "reply", "edit")
_REQUIRES_TARGET_MESSAGE = ("reply", "react", "unreact", "edit", "delete", "mark-read")
_REQUIRES_EMOJI = ("react", "unreact")


@dataclass(frozen=True)
class PreparedPayload:
    platform: str
    action: str
    target: str
    payload: dict[str, Any]
    source_event_id: str = ""
    context: dict[str, Any] = field(default_factory=dict)


def _payload_metadata_matches(
    payload: Any, *, platform: Any, action: Any, target: Any
) -> bool:
    """Bind dispatch metadata to the same canonical payload that is hashed."""
    return isinstance(payload, dict) and (
        payload.get("platform"), payload.get("action"), payload.get("target")
    ) == (platform, action, target)


def validate_attachments(paths: Sequence[str]) -> list[str]:
    if len(paths) > MAX_ATTACHMENTS:
        raise ValidationError(f"at most {MAX_ATTACHMENTS} attachments per action")
    resolved: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser().absolute()
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            raise ValidationError(f"attachment not found: {path}", code="attachment_missing")
        except OSError as exc:
            raise ValidationError("attachment metadata is unreadable", code="attachment_invalid") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValidationError("attachment must not be a symlink", code="attachment_symlink")
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError(f"attachment is not a regular file: {path}", code="attachment_invalid")
        size = metadata.st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise ValidationError(
                f"attachment exceeds {MAX_ATTACHMENT_BYTES} bytes: {path}", code="attachment_too_large"
            )
        if not os.access(path, os.R_OK):
            raise ValidationError(f"attachment not readable: {path}", code="attachment_invalid")
        # Preserve the lexical path so a symlink swap can still be detected by
        # O_NOFOLLOW during staging. Never persist this original path.
        resolved.append(str(path))
    return resolved


def validate_message(text: str | None) -> str:
    if text is None:
        raise ValidationError("--message is required for this action")
    if not text.strip():
        raise ValidationError("--message must not be empty")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ValidationError(f"--message exceeds {MAX_MESSAGE_CHARS} characters")
    return text


def read_message_file(path: Path | str) -> str:
    source = Path(path).expanduser().absolute()
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ValidationError("safe message-file reads are unavailable", code="unsafe_message_file")
    try:
        fd = os.open(source, os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ValidationError("message file is unavailable or unsafe", code="unsafe_message_file") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_MESSAGE_FILE_BYTES:
            raise ValidationError("message file is invalid", code="unsafe_message_file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, MAX_MESSAGE_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_MESSAGE_FILE_BYTES:
                raise ValidationError("message file is too large", code="message_too_large")
    finally:
        os.close(fd)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("message file must be UTF-8", code="invalid_message_encoding") from exc


def read_message_argument(args: argparse.Namespace) -> str | None:
    if getattr(args, "message_stdin", False):
        value = sys.stdin.read(MAX_MESSAGE_CHARS + 1)
        if len(value) > MAX_MESSAGE_CHARS:
            raise ValidationError("stdin message is too large", code="message_too_large")
        return value
    message_file = getattr(args, "message_file", None)
    if message_file is not None:
        return read_message_file(message_file)
    return getattr(args, "message", None)


def validate_emoji(value: str | None) -> str:
    if not value or not value.strip():
        raise ValidationError("--emoji is required for react/unreact")
    text = value.strip()
    if len(text) > MAX_EMOJI_CHARS or CONTROL_CHARS_RE.search(text):
        raise ValidationError("--emoji is invalid")
    return text


def _is_grapheme_extend(character: str) -> bool:
    """Recognise the stdlib-visible continuations used by common emoji.

    Python's standard library has no Unicode ``\X`` grapheme segmenter.  This
    deliberately conservative subset accepts combining/variation marks, emoji
    skin tones, and well-formed ZWJ chains; anything it cannot prove belongs to
    the first displayed grapheme is rejected before the bridge sees it.
    """
    return unicodedata.category(character) in {"Mn", "Mc", "Me"} or (
        "\U0001f3fb" <= character <= "\U0001f3ff"
    )


def _is_single_unicode_grapheme(text: str) -> bool:
    """Conservatively approximate ``Intl.Segmenter(..., grapheme)``."""
    if not text or any(character.isspace() for character in text):
        return False

    characters = list(text)
    if len(characters) == 2 and all(
        "\U0001f1e6" <= character <= "\U0001f1ff" for character in characters
    ):
        return True

    first = characters[0]
    if (
        _is_grapheme_extend(first)
        or first == "\u200d"
        or unicodedata.category(first).startswith("C")
    ):
        return False

    index = 1
    while index < len(characters):
        character = characters[index]
        if _is_grapheme_extend(character):
            index += 1
            continue
        if character != "\u200d":
            return False
        index += 1
        if index >= len(characters):
            return False
        joined = characters[index]
        if (
            _is_grapheme_extend(joined)
            or joined == "\u200d"
            or joined.isspace()
            or unicodedata.category(joined).startswith("C")
        ):
            return False
        index += 1
    return True


def validate_whatsapp_reaction(value: str | None, *, remove: bool) -> str:
    """Validate the exact reaction value accepted by the WhatsApp bridge."""
    if not isinstance(value, str) or not value:
        raise ValidationError("--emoji is required for react/unreact", code="invalid_reaction")
    utf16_units = sum(2 if ord(character) > 0xFFFF else 1 for character in value)
    if (
        utf16_units > MAX_EMOJI_CHARS
        or CONTROL_CHARS_RE.search(value)
        or any(character.isspace() for character in value)
    ):
        raise ValidationError("--emoji is not a valid WhatsApp reaction", code="invalid_reaction")
    if not remove and not _is_single_unicode_grapheme(value):
        raise ValidationError(
            "--emoji must be one Unicode grapheme for WhatsApp reactions",
            code="invalid_reaction",
        )
    return value


def validate_slack_reaction(value: str | None) -> str:
    """Return the Composio/Slack reaction name after optional colon removal."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("--emoji is required for react/unreact", code="invalid_reaction")
    text = value.strip()
    if CONTROL_CHARS_RE.search(text):
        raise ValidationError("--emoji is not a valid Slack reaction", code="invalid_reaction")
    if text.startswith(":") or text.endswith(":"):
        if not (text.startswith(":") and text.endswith(":") and len(text) > 2):
            raise ValidationError("--emoji is not a valid Slack reaction", code="invalid_reaction")
        text = text[1:-1]
    if not SLACK_REACTION_NAME_RE.fullmatch(text) and not _is_unicode_reaction(text):
        raise ValidationError("--emoji is not a valid Slack reaction name", code="invalid_reaction")
    return text


def _is_unicode_reaction(text: str) -> bool:
    if not text or len(text) > MAX_EMOJI_CHARS or any(character.isspace() for character in text):
        return False
    has_symbol = False
    for character in text:
        category = unicodedata.category(character)
        if category in {"So", "Sk"}:
            has_symbol = True
            continue
        if category in {"Mn", "Me"} or character == "\u200d":
            continue
        return False
    return has_symbol


def build_payload(
    *,
    platform: str,
    action: str,
    target: str | None = None,
    conversation_id: str | None = None,
    event: dict[str, Any] | None = None,
    account: str | None = None,
    workspace: str | None = None,
    connector_id: str | None = None,
    endpoint_url: str | None = None,
    execution_account: str | None = None,
    message: str | None = None,
    files: Sequence[str] = (),
    emoji: str | None = None,
    target_message_id: str | None = None,
    target_timestamp: str | None = None,
    target_author: str | None = None,
    target_direction: str | None = None,
    thread_ts: str | None = None,
) -> PreparedPayload:
    """Turn CLI arguments plus optional Context Inbox context into a payload.

    The payload is platform-neutral; adapters translate it at execute time.
    Keeping it neutral means the hash covers user intent rather than wire
    formatting, so an adapter fix never invalidates a pending intent's hash.
    """
    if platform not in PLATFORMS:
        raise ValidationError(f"platform must be one of {', '.join(PLATFORMS)}")
    if action not in ACTIONS:
        raise ValidationError(f"action must be one of {', '.join(ACTIONS)}")

    context: dict[str, Any] = {}
    source_event_id = ""
    canonical_conversation_id = conversation_id or target or ""
    if event is not None:
        source_event_id = event["event_id"]
        if event["platform"] != platform:
            raise ValidationError(
                f"event {source_event_id} is a {event['platform']} event, not {platform}",
                code="platform_mismatch",
            )
        # Fail closed before any target/quote/message is derived: a Signal
        # Desktop history event has no action binding and must never mutate.
        _reject_search_only_source(
            event.get("platform"), event.get("account"), event.get("source_path")
        )
        event_target = event.get("_action_target_id") or event["conversation_id"]
        if target is not None and normalize_target(platform, target) != normalize_target(
            platform, event_target
        ):
            raise ValidationError(
                "explicit target conflicts with --event-id provenance",
                code="event_target_conflict",
            )
        if account is not None and account != event.get("account", ""):
            raise ValidationError(
                "explicit account conflicts with --event-id provenance",
                code="event_identity_conflict",
            )
        if workspace is not None and workspace != event.get("workspace", ""):
            raise ValidationError(
                "explicit workspace conflicts with --event-id provenance",
                code="event_identity_conflict",
            )
        if any(
            value is not None
            for value in (
                target_message_id,
                target_timestamp,
                target_author,
                target_direction,
                thread_ts,
            )
        ):
            raise ValidationError(
                "manual message metadata cannot be combined with --event-id",
                code="mixed_event_metadata",
            )
        event_execution_account = event.get("_action_account_id") or ""
        if execution_account is not None and event_execution_account and execution_account != event_execution_account:
            raise ValidationError(
                "execution account conflicts with --event-id provenance",
                code="event_identity_conflict",
            )
        target = event_target
        account = event.get("account", "")
        workspace = event.get("workspace", "")
        execution_account = event_execution_account or execution_account
        canonical_conversation_id = event["conversation_id"]
        target_message_id = event["source_message_id"]
        target_author = event["sender_id"]
        target_timestamp = event["message_ts"]
        thread_ts = event["thread_id"] or event["source_message_id"]
        context = {
            "conversation_name": event.get("conversation_name", ""),
            "conversation_type": event.get("conversation_type", ""),
            "sender_display_name": event.get("sender_display_name", ""),
            "direction": event.get("direction", ""),
            "quote_text": str(event.get("body", ""))[:QUOTE_CHARS],
        }

    resolved_target = normalize_target(platform, target or "")
    if (
        platform == "whatsapp"
        and action == "mark-read"
        and not WHATSAPP_ORDINARY_JID_RE.fullmatch(resolved_target)
    ):
        raise ValidationError(
            "WhatsApp mark-read requires an ordinary user or group JID",
            code="invalid_whatsapp_mark_read_target",
        )
    canonical_conversation_id = canonical_conversation_id or resolved_target
    account = account or ""
    workspace = workspace or ""
    default_connector = {
        "whatsapp": "whatsapp-loopback-bridge",
        "signal": "signal-cli-jsonrpc",
        "slack": "hermes_slack_personal",
    }[platform]
    connector_id = (connector_id or default_connector).strip()
    if not connector_id or CONTROL_CHARS_RE.search(connector_id) or len(connector_id) > 120:
        raise ValidationError("connector identity is invalid", code="invalid_connector_identity")
    if platform == "whatsapp":
        endpoint_url = assert_loopback(endpoint_url or DEFAULT_WHATSAPP_URL)
    elif platform == "signal":
        endpoint_url = assert_loopback(endpoint_url or DEFAULT_SIGNAL_URL)
        execution_account = execution_account or (account if E164_RE.fullmatch(account) else "")
        if execution_account and not E164_RE.fullmatch(execution_account):
            raise ValidationError("signal execution account must be E.164", code="signal_account_invalid")
    else:
        endpoint_url = ""

    payload: dict[str, Any] = {
        "platform": platform,
        "action": action,
        "target": resolved_target,
        "binding": {
            "target_identity": {
                "platform": platform,
                "account": account,
                "workspace": workspace,
                "conversation_id": canonical_conversation_id,
            },
            "connector_id": connector_id,
            "endpoint_url": endpoint_url,
            "source_event_id": source_event_id,
        },
    }
    if platform == "signal":
        payload["binding"]["execution_account"] = execution_account or ""

    if action in _REQUIRES_MESSAGE:
        validated_message = validate_message(message)
        if platform == "whatsapp" and action == "reply" and len(validated_message) > 10000:
            raise ValidationError(
                "WhatsApp reply exceeds the bridge's 10000-character limit",
                code="message_too_large",
            )
        payload["message"] = validated_message
    elif action == "send-file" and message is not None and message.strip():
        payload["message"] = validate_message(message)
    elif message is not None and message.strip():
        raise ValidationError(f"--message is not accepted for action {action}")

    if action == "send-file":
        if platform == "slack":
            raise UnsupportedActionError(
                "Slack send-file is unsupported until a local file can be staged as a Composio FileUploadable",
                code="unsupported_on_platform",
            )
        if not files:
            raise ValidationError("--file is required for send-file")
        if platform == "whatsapp" and len(files) != 1:
            raise ValidationError(
                "whatsapp send-file supports exactly one attachment",
                code="too_many_attachments",
            )
        payload["attachments"] = validate_attachments(files)
    elif files:
        raise ValidationError(f"--file is not accepted for action {action}")

    if action in _REQUIRES_EMOJI:
        if platform == "slack":
            payload["emoji"] = validate_slack_reaction(emoji)
        elif platform == "whatsapp":
            payload["emoji"] = validate_whatsapp_reaction(emoji, remove=action == "unreact")
        else:
            payload["emoji"] = validate_emoji(emoji)
        payload["remove"] = action == "unreact"
    elif emoji:
        raise ValidationError(f"--emoji is not accepted for action {action}")

    if action in _REQUIRES_TARGET_MESSAGE:
        if not (target_message_id or target_timestamp):
            raise ValidationError(
                f"action {action} needs --event-id or --target-message-id/--target-timestamp",
                code="missing_target_message",
            )
        payload["target_message_id"] = (target_message_id or "").strip()
        payload["target_timestamp"] = (target_timestamp or "").strip()
        payload["target_author"] = (target_author or "").strip()
        if event is not None:
            payload["target_from_me"] = direction_is_from_me(event.get("direction"))
        elif platform == "whatsapp":
            if action == "reply":
                raise ValidationError(
                    "WhatsApp replies require an event-provided quotedText",
                    code="whatsapp_reply_requires_event",
                )
            canonical_target_direction = canonical_direction(target_direction)
            if canonical_target_direction == "unknown":
                raise ValidationError(
                    "manual WhatsApp message metadata requires --target-direction inbound|outbound",
                    code="missing_target_direction",
                )
            payload["target_from_me"] = canonical_target_direction == "outbound"
        if platform == "whatsapp" and not WHATSAPP_STANZA_ID_RE.fullmatch(
            payload["target_message_id"]
        ):
            raise ValidationError(
                "whatsapp message actions require a valid stanza id",
                code="invalid_whatsapp_stanza_id",
            )
        if platform == "whatsapp":
            if action in ("edit", "delete") and not payload["target_from_me"]:
                raise ValidationError(
                    f"WhatsApp {action} can only target an outbound message",
                    code="target_not_outbound",
                )
            if resolved_target.endswith("@g.us") and payload["target_author"] and not (
                WHATSAPP_PARTICIPANT_RE.fullmatch(payload["target_author"])
            ):
                raise ValidationError(
                    "WhatsApp group participant is invalid", code="invalid_whatsapp_participant"
                )
        if platform == "signal":
            if (
                event is not None
                and canonical_direction(event.get("direction")) == "outbound"
                and event.get("_action_account_id")
            ):
                payload["target_author"] = event["_action_account_id"]
            millis = _epoch_millis(payload["target_timestamp"]) or _epoch_millis(payload["target_message_id"])
            if millis is None:
                raise ValidationError(
                    "signal actions need a resolvable message timestamp",
                    code="missing_target_timestamp",
                )
            payload["target_timestamp_millis"] = millis
            if action in ("reply", "react", "unreact") and not payload["target_author"]:
                raise ValidationError(
                    "signal reply/react needs the original sender (--target-author or --event-id)",
                    code="missing_target_author",
                )
            if payload["target_author"] and not (
                E164_RE.fullmatch(payload["target_author"])
                or SIGNAL_UUID_RE.fullmatch(payload["target_author"])
            ):
                raise ValidationError(
                    "signal sender reference must be E.164 or UUID",
                    code="invalid_signal_sender",
                )
            if action in ("edit", "delete"):
                if event is None:
                    raise ValidationError(
                        f"Signal {action} requires event direction provenance",
                        code="missing_target_direction",
                    )
                if not payload["target_from_me"]:
                    raise ValidationError(
                        f"Signal {action} can only target an outbound message",
                        code="target_not_outbound",
                    )
        if platform == "slack":
            reference = (payload["target_message_id"] or payload["target_timestamp"]).strip()
            if not SLACK_TS_RE.fullmatch(reference):
                raise ValidationError(
                    "slack message actions require a valid Slack timestamp",
                    code="invalid_slack_timestamp",
                )
            payload["target_message_id"] = reference
            if action == "reply":
                resolved_thread = (thread_ts or reference).strip()
                if not SLACK_TS_RE.fullmatch(resolved_thread):
                    raise ValidationError(
                        "Slack replies require a valid thread_ts",
                        code="invalid_slack_thread_ts",
                    )
                payload["thread_ts"] = resolved_thread

    if action == "reply" and event is not None:
        # The bridge requires quotedText even for media-only messages whose
        # textual quote is empty. It comes only from the referenced event.
        payload["quote_text"] = context.get("quote_text", "")

    return PreparedPayload(
        platform=platform,
        action=action,
        target=resolved_target,
        payload=payload,
        source_event_id=source_event_id,
        context=context,
    )


# --------------------------------------------------------------------------
# intent store
# --------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_IN_FLIGHT = "in_flight"
STATUS_EXTERNAL_PENDING = "external_pending"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_UNCERTAIN = "uncertain"
STATUS_EXPIRED = "expired"

TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_UNCERTAIN, STATUS_EXPIRED)
ALLOWED_TRANSITIONS = {
    STATUS_PENDING: {STATUS_IN_FLIGHT, STATUS_EXTERNAL_PENDING, STATUS_EXPIRED},
    STATUS_IN_FLIGHT: {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_UNCERTAIN},
    STATUS_EXTERNAL_PENDING: {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_UNCERTAIN},
}


class ActionStore:
    """Private SQLite store of mutation intents and their bounded audit trail."""

    def __init__(self, db_path: Path | str = DEFAULT_ACTIONS_DB, clock: Callable[[], dt.datetime] = _utc_now):
        self.db_path = Path(db_path).expanduser()
        self.staging_root = self.db_path.parent / f".{self.db_path.name}.staging"
        self.handle_key_path = self.db_path.parent / f".{self.db_path.name}.target-hmac.key"
        self.clock = clock
        _prepare_private_file_parent(self.db_path.parent)
        _reject_symlink_sqlite_paths(self.db_path)
        _chmod_private_file(self.db_path)
        _chmod_sqlite_sidecars(self.db_path)
        self._handle_key = self._load_or_create_handle_key()
        self._init_schema()

    def _load_or_create_handle_key(self) -> bytes:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.handle_key_path, flags, 0o600)
        except FileExistsError:
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if nofollow is None:
                raise ValidationError("cannot safely open target-handle key", code="unsafe_key_file")
            try:
                fd = os.open(
                    self.handle_key_path,
                    os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
                )
            except OSError as exc:
                raise ValidationError("target-handle key is unsafe", code="unsafe_key_file") from exc
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    raise ValidationError("target-handle key is unsafe", code="unsafe_key_file")
                key = os.read(fd, 33)
            finally:
                os.close(fd)
            if len(key) != 32:
                raise ValidationError("target-handle key is invalid", code="invalid_key_file")
            os.chmod(self.handle_key_path, 0o600)
            return key
        else:
            key = secrets.token_bytes(32)
            try:
                view = memoryview(key)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(self.handle_key_path, 0o600)
            return key

    def _target_handle(self, payload: dict[str, Any]) -> str:
        binding = payload.get("binding", {})
        identity = binding.get("target_identity", {}) if isinstance(binding, dict) else {}
        digest = hmac.new(
            self._handle_key,
            _canonical_json(identity).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]
        return f"target_{digest}"

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _chmod_private_file(self.db_path)
        _chmod_sqlite_sidecars(self.db_path)
        return conn

    def _init_schema(self) -> None:
        conn = self.connect()
        try:
            # Ensure new immutable columns exist before creating triggers on a
            # database produced by an earlier controller build.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS action_intents(
                  intent_id TEXT PRIMARY KEY,
                  platform TEXT NOT NULL,
                  action TEXT NOT NULL,
                  target TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  payload_hash TEXT NOT NULL,
                  target_handle TEXT NOT NULL DEFAULT '',
                  source_event_id TEXT NOT NULL DEFAULT '',
                  created_ts TEXT NOT NULL,
                  expires_ts TEXT NOT NULL,
                  status TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  claimed_ts TEXT NOT NULL DEFAULT '',
                  finished_ts TEXT NOT NULL DEFAULT '',
                  result_code TEXT NOT NULL DEFAULT '',
                  remote_ref TEXT NOT NULL DEFAULT ''
                )
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(action_intents)")}
            if "target_handle" not in columns:
                conn.execute(
                    "ALTER TABLE action_intents ADD COLUMN target_handle TEXT NOT NULL DEFAULT ''"
                )
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS action_intents(
                  intent_id TEXT PRIMARY KEY,
                  platform TEXT NOT NULL,
                  action TEXT NOT NULL,
                  target TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  payload_hash TEXT NOT NULL,
                  target_handle TEXT NOT NULL DEFAULT '',
                  source_event_id TEXT NOT NULL DEFAULT '',
                  created_ts TEXT NOT NULL,
                  expires_ts TEXT NOT NULL,
                  status TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  claimed_ts TEXT NOT NULL DEFAULT '',
                  finished_ts TEXT NOT NULL DEFAULT '',
                  result_code TEXT NOT NULL DEFAULT '',
                  remote_ref TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS action_audit(
                  audit_id TEXT PRIMARY KEY,
                  intent_id TEXT NOT NULL,
                  ts TEXT NOT NULL,
                  event TEXT NOT NULL,
                  platform TEXT NOT NULL,
                  action TEXT NOT NULL,
                  status TEXT NOT NULL,
                  payload_hash TEXT NOT NULL,
                  target_hash TEXT NOT NULL,
                  detail TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_action_audit_intent ON action_audit(intent_id, ts);

                -- The payload a user approved must never change underneath an
                -- execution. Only lifecycle columns are writable.
                CREATE TRIGGER IF NOT EXISTS action_intents_immutable
                BEFORE UPDATE ON action_intents
                FOR EACH ROW WHEN
                  OLD.intent_id IS NOT NEW.intent_id
                  OR OLD.platform IS NOT NEW.platform
                  OR OLD.action IS NOT NEW.action
                  OR OLD.target IS NOT NEW.target
                  OR OLD.payload_json IS NOT NEW.payload_json
                  OR OLD.payload_hash IS NOT NEW.payload_hash
                  OR OLD.target_handle IS NOT NEW.target_handle
                  OR OLD.source_event_id IS NOT NEW.source_event_id
                  OR OLD.created_ts IS NOT NEW.created_ts
                  OR OLD.expires_ts IS NOT NEW.expires_ts
                BEGIN
                  SELECT RAISE(ABORT, 'action intent payload is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS action_audit_append_only_update
                BEFORE UPDATE ON action_audit
                BEGIN
                  SELECT RAISE(ABORT, 'audit log is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS action_audit_append_only_delete
                BEFORE DELETE ON action_audit
                BEGIN
                  SELECT RAISE(ABORT, 'audit log is append-only');
                END;
                """
            )
        finally:
            conn.close()

    # -- audit ----------------------------------------------------------

    def _audit(
        self,
        conn: sqlite3.Connection,
        intent: dict[str, Any],
        event: str,
        status: str,
        detail: str = "",
    ) -> None:
        """Record what happened, never what was said.

        Bodies, attachments, emoji and raw targets are all excluded; the
        payload digest plus keyed target handle correlate an audit row with an
        intent without turning the log into a second copy of the message.
        """
        detail_code = detail if AUDIT_DETAIL_CODE_RE.fullmatch(detail) else "detail_redacted"
        conn.execute(
            "INSERT INTO action_audit(audit_id, intent_id, ts, event, platform, action, status, "
            "payload_hash, target_hash, detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                secrets.token_hex(16),
                intent["intent_id"],
                _iso(self.clock()),
                event,
                intent["platform"],
                intent["action"],
                status,
                intent["payload_hash"],
                intent["target_handle"],
                detail_code,
            ),
        )

    # -- lifecycle ------------------------------------------------------

    def _ensure_staging_root(self) -> None:
        if self.staging_root.exists() or self.staging_root.is_symlink():
            metadata = self.staging_root.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValidationError("attachment staging root is unsafe", code="attachment_staging_unsafe")
        else:
            self.staging_root.mkdir(mode=0o700)
        os.chmod(self.staging_root, 0o700)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _stage_attachments(self, intent_id: str, paths: Sequence[str]) -> list[dict[str, Any]]:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise ValidationError(
                "this platform cannot safely stage attachments", code="attachment_nofollow_unavailable"
            )
        self._ensure_staging_root()
        temporary = self.staging_root / f".tmp-{intent_id}-{secrets.token_hex(6)}"
        final = self.staging_root / intent_id
        temporary.mkdir(mode=0o700)
        staged: list[dict[str, Any]] = []
        try:
            for index, raw in enumerate(paths):
                source = Path(raw)
                try:
                    source_lstat = source.lstat()
                except FileNotFoundError as exc:
                    raise ValidationError("attachment disappeared", code="attachment_missing") from exc
                if stat.S_ISLNK(source_lstat.st_mode):
                    raise ValidationError("attachment must not be a symlink", code="attachment_symlink")
                source_fd = os.open(source, os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0))
                suffix = source.suffix.lower()
                if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                    suffix = ""
                staged_name = f"attachment-{index}{suffix}"
                temporary_path = temporary / staged_name
                destination_fd = -1
                try:
                    source_stat = os.fstat(source_fd)
                    if not stat.S_ISREG(source_stat.st_mode):
                        raise ValidationError(
                            "attachment is not a regular file", code="attachment_invalid"
                        )
                    if source_stat.st_size > MAX_ATTACHMENT_BYTES:
                        raise ValidationError(
                            "attachment exceeds the size limit", code="attachment_too_large"
                        )
                    destination_fd = os.open(
                        temporary_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                    )
                    digest = hashlib.sha256()
                    total = 0
                    while True:
                        chunk = os.read(source_fd, 1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_ATTACHMENT_BYTES:
                            raise ValidationError(
                                "attachment exceeds the size limit", code="attachment_too_large"
                            )
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(destination_fd, view)
                            view = view[written:]
                    os.fsync(destination_fd)
                finally:
                    os.close(source_fd)
                    if destination_fd >= 0:
                        os.close(destination_fd)
                os.chmod(temporary_path, 0o600)
                staged.append(
                    {
                        "staged_path": str(final / staged_name),
                        "sha256": digest.hexdigest(),
                        "size": total,
                    }
                )
            self._fsync_directory(temporary)
            os.replace(temporary, final)
            self._fsync_directory(self.staging_root)
            return staged
        except Exception:
            if temporary.exists() and not temporary.is_symlink():
                shutil.rmtree(temporary)
            raise

    def verify_staged_attachments(self, intent: dict[str, Any], payload: dict[str, Any]) -> None:
        attachments = payload.get("attachments", [])
        for item in attachments:
            if not isinstance(item, dict):
                raise ValidationError(
                    "intent contains an unstaged attachment", code="attachment_integrity_failed"
                )
            staged = Path(str(item.get("staged_path") or ""))
            expected_parent = self.staging_root / intent["intent_id"]
            if staged.parent != expected_parent or not staged.name.startswith("attachment-"):
                raise ValidationError(
                    "staged attachment location is invalid", code="attachment_integrity_failed"
                )
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if nofollow is None:
                raise ValidationError(
                    "attachment verification is unavailable", code="attachment_integrity_failed"
                )
            try:
                fd = os.open(staged, os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0))
            except OSError as exc:
                raise ValidationError(
                    "staged attachment is unavailable", code="attachment_integrity_failed"
                ) from exc
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValidationError(
                        "staged attachment is not regular", code="attachment_integrity_failed"
                    )
                digest = hashlib.sha256()
                total = 0
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ATTACHMENT_BYTES:
                        raise ValidationError(
                            "staged attachment exceeds its bound", code="attachment_integrity_failed"
                        )
                    digest.update(chunk)
            finally:
                os.close(fd)
            if total != item.get("size") or digest.hexdigest() != item.get("sha256"):
                raise ValidationError(
                    "staged attachment digest mismatch", code="attachment_integrity_failed"
                )

    def _purge_staging_directory(self, directory: Path) -> bool:
        if directory.parent != self.staging_root:
            return False
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(metadata.st_mode):
            directory.unlink()
            return True
        if stat.S_ISDIR(metadata.st_mode):
            shutil.rmtree(directory)
            return True
        directory.unlink()
        return True

    def purge_staged(self, intent_id: str) -> bool:
        if not re.fullmatch(r"pmi_[0-9a-f]{32}", intent_id):
            return False
        directory = self.staging_root / intent_id
        return self._purge_staging_directory(directory)

    def create_intent(self, prepared: PreparedPayload, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, Any]:
        ttl = _bounded_int(ttl_seconds, DEFAULT_TTL_SECONDS, MAX_TTL_SECONDS, "ttl-seconds")
        now = self.clock()
        intent_id = f"pmi_{secrets.token_hex(16)}"
        try:
            # This canonical round-trip is also a deep copy. Once creation
            # starts, later mutation of PreparedPayload.payload cannot change
            # the bytes that are staged, hashed, persisted, or dispatched.
            payload = json.loads(_canonical_json(prepared.payload))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError(
                "prepared payload is not canonical JSON", code="prepared_payload_invalid"
            ) from exc
        if not _payload_metadata_matches(
            payload,
            platform=prepared.platform,
            action=prepared.action,
            target=prepared.target,
        ):
            raise ValidationError(
                "prepared payload metadata mismatch; refusing to create intent",
                code="prepared_payload_mismatch",
            )
        binding = payload.get("binding", {})
        if prepared.platform == "signal" and not binding.get("execution_account"):
            raise ValidationError(
                "Signal account must be bound when the intent is prepared",
                code="signal_account_missing",
            )
        if payload.get("attachments"):
            payload["attachments"] = self._stage_attachments(intent_id, payload["attachments"])
        intent = {
            "intent_id": intent_id,
            "platform": prepared.platform,
            "action": prepared.action,
            "target": prepared.target,
            "payload_json": _canonical_json(payload),
            "payload_hash": _payload_hash(payload),
            "target_handle": self._target_handle(payload),
            "source_event_id": prepared.source_event_id,
            "created_ts": _iso(now),
            "expires_ts": _iso(now + dt.timedelta(seconds=ttl)),
            "status": STATUS_PENDING,
        }
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO action_intents(intent_id, platform, action, target, payload_json, "
                "payload_hash, target_handle, source_event_id, created_ts, expires_ts, status) "
                "VALUES(:intent_id,:platform,:action,:target,:payload_json,:payload_hash,"
                ":target_handle,:source_event_id,:created_ts,:expires_ts,:status)",
                intent,
            )
            self._audit(conn, intent, "prepared", STATUS_PENDING, f"ttl={ttl}s")
            conn.execute("COMMIT")
        except Exception:
            self.purge_staged(intent_id)
            raise
        finally:
            conn.close()
        return intent

    def load_intent(self, intent_id: str) -> dict[str, Any]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM action_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise MessagingError(f"intent not found: {intent_id}", code="intent_not_found")
        return dict(row)

    def list_intents(self, *, status: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        limit = _bounded_int(limit, 25, SEARCH_LIMIT_MAX, "limit")
        conn = self.connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM action_intents WHERE status = ? ORDER BY created_ts DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM action_intents ORDER BY created_ts DESC LIMIT ?", (limit,)
                ).fetchall()
        finally:
            conn.close()
        return [summarize_intent(dict(row)) for row in rows]

    def verify_executable(self, intent: dict[str, Any]) -> dict[str, Any]:
        """Check everything that does not require touching the network."""
        if intent["status"] != STATUS_PENDING:
            raise IntentStateError(
                f"intent {intent['intent_id']} is {intent['status']}, not pending; prepare a new intent",
                code="intent_not_pending",
                intent_status=intent["status"],
            )
        expires = _parse_iso(intent["expires_ts"])
        if expires is None or self.clock() > expires:
            self._mark_expired(intent)
            raise IntentStateError(
                f"intent {intent['intent_id']} expired at {intent['expires_ts']}; prepare a new intent",
                code="intent_expired",
            )
        try:
            payload = json.loads(intent["payload_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise IntentStateError("intent payload is unreadable", code="intent_corrupt") from exc
        if _payload_hash(payload) != intent["payload_hash"]:
            raise IntentStateError(
                f"intent {intent['intent_id']} payload hash mismatch; refusing to execute",
                code="intent_tampered",
            )
        if not _payload_metadata_matches(
            payload,
            platform=intent.get("platform"),
            action=intent.get("action"),
            target=intent.get("target"),
        ):
            raise IntentStateError(
                "intent metadata does not match its payload; refusing to execute",
                code="intent_tampered",
            )
        if intent.get("target_handle") != self._target_handle(payload):
            raise IntentStateError(
                "intent target handle mismatch; refusing to execute",
                code="intent_tampered",
            )
        if payload.get("binding", {}).get("source_event_id", "") != intent.get(
            "source_event_id", ""
        ):
            raise IntentStateError(
                "intent source-event provenance mismatch; refusing to execute",
                code="intent_tampered",
            )
        return payload

    def _mark_expired(self, intent: dict[str, Any]) -> None:
        transitioned = False
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE action_intents SET status = ?, finished_ts = ?, result_code = ? "
                "WHERE intent_id = ? AND status = ? AND expires_ts <= ?",
                (
                    STATUS_EXPIRED,
                    _iso(self.clock()),
                    "expired",
                    intent["intent_id"],
                    STATUS_PENDING,
                    _iso(self.clock()),
                ),
            )
            if cursor.rowcount == 1:
                self._audit(conn, intent, "expired", STATUS_EXPIRED, "expired")
                transitioned = True
            conn.execute("COMMIT")
        finally:
            conn.close()
        if transitioned:
            self.purge_staged(intent["intent_id"])

    def claim(self, intent: dict[str, Any], *, next_status: str = STATUS_IN_FLIGHT) -> None:
        """Atomically move pending -> in-flight before any network I/O.

        The status/expiry predicate is one atomic local at-most-once claim. It
        cannot establish a provider-level delivery uniqueness guarantee.
        """
        if next_status not in ALLOWED_TRANSITIONS[STATUS_PENDING] - {STATUS_EXPIRED}:
            raise IntentStateError("invalid claim transition", code="invalid_transition")
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = _iso(self.clock())
            cursor = conn.execute(
                "UPDATE action_intents SET status = ?, claimed_ts = ?, attempts = attempts + 1 "
                "WHERE intent_id = ? AND status = ? AND expires_ts > ?",
                (next_status, now, intent["intent_id"], STATUS_PENDING, now),
            )
            if cursor.rowcount != 1:
                current = conn.execute(
                    "SELECT status, expires_ts FROM action_intents WHERE intent_id = ?",
                    (intent["intent_id"],),
                ).fetchone()
                if current is not None and current["status"] == STATUS_PENDING and current[
                    "expires_ts"
                ] <= now:
                    expired = conn.execute(
                        "UPDATE action_intents SET status=?, finished_ts=?, result_code=? "
                        "WHERE intent_id=? AND status=? AND expires_ts <= ?",
                        (
                            STATUS_EXPIRED,
                            now,
                            "expired",
                            intent["intent_id"],
                            STATUS_PENDING,
                            now,
                        ),
                    )
                    if expired.rowcount == 1:
                        self._audit(conn, intent, "expired", STATUS_EXPIRED, "expired")
                        conn.execute("COMMIT")
                        self.purge_staged(intent["intent_id"])
                        raise IntentStateError(
                            f"intent {intent['intent_id']} expired before it could be claimed",
                            code="intent_expired",
                        )
                conn.execute("ROLLBACK")
                raise IntentStateError(
                    f"intent {intent['intent_id']} was already claimed; refusing to replay",
                    code="intent_already_claimed",
                )
            self._audit(conn, intent, "claimed", next_status)
            conn.execute("COMMIT")
        finally:
            conn.close()

    def finish(
        self,
        intent: dict[str, Any],
        *,
        status: str,
        result_code: str,
        remote_ref: str = "",
        detail: str = "",
        expected_status: str = STATUS_IN_FLIGHT,
    ) -> None:
        if status not in ALLOWED_TRANSITIONS.get(expected_status, set()):
            raise IntentStateError("invalid terminal transition", code="invalid_transition")
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE action_intents SET status = ?, finished_ts = ?, result_code = ?, remote_ref = ? "
                "WHERE intent_id = ? AND status = ?",
                (
                    status,
                    _iso(self.clock()),
                    result_code,
                    _sanitize_detail(remote_ref, 120),
                    intent["intent_id"],
                    expected_status,
                ),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                raise IntentStateError(
                    f"intent {intent['intent_id']} is no longer {expected_status}",
                    code="transition_conflict",
                )
            self._audit(conn, intent, "result", status, result_code)
            conn.execute("COMMIT")
        finally:
            conn.close()
        self.purge_staged(intent["intent_id"])

    def reconcile_stale(self, *, age_seconds: int = 900) -> dict[str, Any]:
        age = _bounded_int(age_seconds, 900, 30 * 24 * 60 * 60, "age-seconds")
        cutoff = _iso(self.clock() - dt.timedelta(seconds=age))
        reconciled: list[str] = []
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM action_intents WHERE status IN (?, ?) "
                "AND claimed_ts != '' AND claimed_ts <= ? ORDER BY claimed_ts, intent_id",
                (STATUS_IN_FLIGHT, STATUS_EXTERNAL_PENDING, cutoff),
            ).fetchall()
            for row in rows:
                intent = dict(row)
                cursor = conn.execute(
                    "UPDATE action_intents SET status=?, finished_ts=?, result_code=? "
                    "WHERE intent_id=? AND status=? AND claimed_ts <= ?",
                    (
                        STATUS_UNCERTAIN,
                        _iso(self.clock()),
                        "stale_dispatch",
                        intent["intent_id"],
                        intent["status"],
                        cutoff,
                    ),
                )
                if cursor.rowcount == 1:
                    self._audit(
                        conn,
                        intent,
                        "reconciled",
                        STATUS_UNCERTAIN,
                        "stale_dispatch",
                    )
                    reconciled.append(intent["intent_id"])
            conn.execute("COMMIT")
        finally:
            conn.close()
        for intent_id in reconciled:
            self.purge_staged(intent_id)
        return {"status": "ok", "reconciled": len(reconciled), "intent_ids": reconciled}

    def purge_terminal(
        self, *, retention_seconds: int = DEFAULT_RETENTION_SECONDS
    ) -> dict[str, Any]:
        retention = _bounded_int(
            retention_seconds,
            DEFAULT_RETENTION_SECONDS,
            365 * 24 * 60 * 60,
            "retention-seconds",
        )
        now = self.clock()
        now_text = _iso(now)
        cutoff_moment = now - dt.timedelta(seconds=retention)
        cutoff = _iso(cutoff_moment)
        expired_ids: list[str] = []
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            expired_rows = conn.execute(
                "SELECT * FROM action_intents WHERE status=? AND expires_ts <= ?",
                (STATUS_PENDING, now_text),
            ).fetchall()
            for row in expired_rows:
                intent = dict(row)
                cursor = conn.execute(
                    "UPDATE action_intents SET status=?, finished_ts=?, result_code=? "
                    "WHERE intent_id=? AND status=? AND expires_ts <= ?",
                    (
                        STATUS_EXPIRED,
                        now_text,
                        "expired",
                        intent["intent_id"],
                        STATUS_PENDING,
                        now_text,
                    ),
                )
                if cursor.rowcount == 1:
                    self._audit(conn, intent, "expired", STATUS_EXPIRED, "expired")
                    expired_ids.append(intent["intent_id"])
            rows = conn.execute(
                "SELECT intent_id FROM action_intents WHERE status IN (?,?,?,?) "
                "AND finished_ts != '' AND finished_ts <= ?",
                (*TERMINAL_STATUSES, cutoff),
            ).fetchall()
            intent_ids = [row["intent_id"] for row in rows]
            purged = 0
            for intent_id in intent_ids:
                cursor = conn.execute(
                    "DELETE FROM action_intents WHERE intent_id=? AND status IN (?,?,?,?) "
                    "AND finished_ts <= ?",
                    (intent_id, *TERMINAL_STATUSES, cutoff),
                )
                purged += cursor.rowcount
            known_ids = {
                row["intent_id"] for row in conn.execute("SELECT intent_id FROM action_intents")
            }
            conn.execute("COMMIT")
        finally:
            conn.close()
        purged_staged = 0
        for intent_id in {*intent_ids, *expired_ids}:
            purged_staged += int(self.purge_staged(intent_id))
        if self.staging_root.exists() and not self.staging_root.is_symlink():
            for child in self.staging_root.iterdir():
                if child.name in known_ids:
                    continue
                try:
                    modified = child.lstat().st_mtime
                except FileNotFoundError:
                    continue
                if modified <= cutoff_moment.timestamp():
                    purged_staged += int(self._purge_staging_directory(child))
        return {
            "status": "ok",
            "retention_seconds": retention,
            "purged_intents": purged,
            "expired_intents": len(expired_ids),
            "purged_staged_artifacts": purged_staged,
        }

    def audit_only(self, intent: dict[str, Any], event: str, status: str, detail: str = "") -> None:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._audit(conn, intent, event, status, detail)
            conn.execute("COMMIT")
        finally:
            conn.close()

    def audit_trail(self, intent_id: str, limit: int = 50) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT ts, event, status, detail FROM action_audit WHERE intent_id = ? "
                "ORDER BY rowid LIMIT ?",
                (intent_id, limit),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM action_intents GROUP BY status"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) AS count FROM action_intents").fetchone()["count"]
        finally:
            conn.close()
        return {"total": total, "by_status": {row["status"]: row["count"] for row in rows}}


def summarize_intent(intent: dict[str, Any]) -> dict[str, Any]:
    """Bounded view of an intent: lifecycle metadata, never the message body."""
    return {
        "intent_id": intent["intent_id"],
        "platform": intent["platform"],
        "action": intent["action"],
        "target_handle": intent.get("target_handle") or "unavailable",
        "payload_hash": intent["payload_hash"],
        "source_event_id": intent["source_event_id"],
        "created_ts": intent["created_ts"],
        "expires_ts": intent["expires_ts"],
        "status": intent["status"],
        "attempts": intent["attempts"],
        "result_code": intent["result_code"],
    }


def _open_actions_db_readonly(path: Path | str) -> sqlite3.Connection | None:
    resolved = Path(path).expanduser()
    try:
        metadata = resolved.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MessagingError("actions database path is unsafe", code="actions_db_unsafe")
    uri = f"file:{quote(str(resolved))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def list_intents_readonly(
    path: Path | str, *, status: str | None = None, limit: int = 25
) -> list[dict[str, Any]]:
    bounded = _bounded_int(limit, 25, SEARCH_LIMIT_MAX, "limit")
    conn = _open_actions_db_readonly(path)
    if conn is None:
        return []
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM action_intents WHERE status=? ORDER BY created_ts DESC LIMIT ?",
                (status, bounded),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM action_intents ORDER BY created_ts DESC LIMIT ?", (bounded,)
            ).fetchall()
    except sqlite3.Error as exc:
        raise MessagingError("actions database is unreadable", code="actions_db_unreadable") from exc
    finally:
        conn.close()
    return [summarize_intent(dict(row)) for row in rows]


def read_action_stats(path: Path | str) -> dict[str, Any]:
    conn = _open_actions_db_readonly(path)
    if conn is None:
        return {"available": False, "total": 0, "by_status": {}}
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM action_intents GROUP BY status"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS count FROM action_intents").fetchone()["count"]
    except sqlite3.Error as exc:
        return {
            "available": True,
            "total": 0,
            "by_status": {},
            "detail": type(exc).__name__,
        }
    finally:
        conn.close()
    return {
        "available": True,
        "total": total,
        "by_status": {row["status"]: row["count"] for row in rows},
    }


# --------------------------------------------------------------------------
# adapters
# --------------------------------------------------------------------------


class WhatsAppAdapter:
    """Talks to a local WhatsApp bridge over loopback HTTP.

    Targets and message ids are always JSON values in a request body; nothing
    here ever reaches a shell.
    """

    name = "whatsapp"

    ROUTES = {
        "send": "/send",
        "send-file": "/send-media",
        "reply": "/reply",
        "react": "/reaction",
        "unreact": "/reaction",
        "edit": "/edit",
        "delete": "/delete",
        "mark-read": "/read",
        "typing-start": "/typing",
        "typing-stop": "/typing",
    }

    def __init__(self, client: LoopbackHttpClient):
        self.client = client

    def build_request(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        action = payload["action"]
        path = self.ROUTES.get(action)
        if path is None:
            raise UnsupportedActionError(f"whatsapp bridge has no route for {action}")
        target = payload["target"]
        body: dict[str, Any]
        if action == "send":
            body = {"chatId": target, "message": payload["message"]}
        elif action == "send-file":
            attachments = payload.get("attachments", [])
            if len(attachments) != 1 or not isinstance(attachments[0], dict):
                raise ValidationError(
                    "whatsapp send-file supports exactly one staged attachment",
                    code="invalid_attachment_count",
                )
            body = {"chatId": target, "filePath": attachments[0]["staged_path"]}
            if payload.get("message"):
                body["caption"] = payload["message"]
        elif action == "reply":
            body = {
                "chatId": target,
                "messageId": payload["target_message_id"],
                "message": payload["message"],
                "quotedText": payload.get("quote_text", ""),
                "fromMe": bool(payload["target_from_me"]),
            }
            if target.endswith("@g.us") and payload.get("target_author"):
                body["participant"] = payload["target_author"]
        elif action in ("react", "unreact"):
            body = {
                "chatId": target,
                "messageId": payload["target_message_id"],
                "emoji": payload["emoji"],
                "fromMe": bool(payload["target_from_me"]),
                "remove": bool(payload["remove"]),
            }
            if target.endswith("@g.us") and payload.get("target_author"):
                body["participant"] = payload["target_author"]
        elif action == "edit":
            body = {
                "chatId": target,
                "messageId": payload["target_message_id"],
                "message": payload["message"],
            }
        elif action == "delete":
            body = {"chatId": target, "messageId": payload["target_message_id"]}
            if target.endswith("@g.us") and payload.get("target_author"):
                body["participant"] = payload["target_author"]
        elif action == "mark-read":
            body = {
                "chatId": target,
                "messageId": payload["target_message_id"],
                "fromMe": bool(payload["target_from_me"]),
            }
            if target.endswith("@g.us") and payload.get("target_author"):
                body["participant"] = payload["target_author"]
        else:  # typing-start / typing-stop
            body = {"chatId": target}
            if action == "typing-stop":
                body["stop"] = True
        return path, body

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        path, body = self.build_request(payload)
        response = self.client.post_json(path, body)
        return _interpret_bridge_response(response, action=payload["action"])


def _interpret_bridge_response(response: dict[str, Any], *, action: str) -> dict[str, Any]:
    if response.get("success") is False:
        raise RemoteRejectedError("WhatsApp bridge reported failure", code="remote_rejected")
    if response.get("success") is not True:
        raise TransportAmbiguousError(
            "WhatsApp bridge response lacked a positive success acknowledgement",
            code="invalid_bridge_ack",
        )
    if response.get("error") is not None:
        raise TransportAmbiguousError(
            "WhatsApp bridge returned a contradictory acknowledgement",
            code="invalid_bridge_ack",
        )
    remote_ref = ""
    for key in ("id", "messageId", "message_id", "key"):
        value = response.get(key)
        if isinstance(value, str):
            remote_ref = value
            break
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            remote_ref = value["id"]
            break
    if action in ("send", "reply") and not remote_ref:
        raise TransportAmbiguousError(
            "WhatsApp send/reply acknowledgement lacked a message id",
            code="invalid_bridge_ack",
        )
    if remote_ref and not WHATSAPP_STANZA_ID_RE.fullmatch(remote_ref):
        raise TransportAmbiguousError(
            "WhatsApp acknowledgement contained an invalid message id",
            code="invalid_bridge_ack",
        )
    return {"remote_ref": remote_ref}


class SignalAdapter:
    """signal-cli JSON-RPC over loopback HTTP."""

    name = "signal"

    def __init__(self, client: LoopbackHttpClient, account: str, rpc_path: str = "/api/v1/rpc"):
        if not account:
            raise ValidationError(
                f"signal account is not configured; set {SIGNAL_ACCOUNT_ENV}",
                code="signal_account_missing",
            )
        if not E164_RE.match(account):
            raise ValidationError(f"{SIGNAL_ACCOUNT_ENV} must be E.164", code="signal_account_invalid")
        self.client = client
        self.account = account
        self.rpc_path = rpc_path

    def build_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = payload["action"]
        target = payload["target"]
        is_group = target.startswith("group:")
        params: dict[str, Any] = {"account": self.account}
        params.update(signal_target_params(target))

        if action in ("send", "send-file", "reply", "edit"):
            method = "send"
            if payload.get("message"):
                params["message"] = payload["message"]
            if action == "send-file":
                attachments = payload.get("attachments", [])
                if not attachments or any(not isinstance(item, dict) for item in attachments):
                    raise ValidationError(
                        "signal attachments must be staged", code="attachment_integrity_failed"
                    )
                params["attachments"] = [item["staged_path"] for item in attachments]
            if action == "reply":
                params["quoteTimestamp"] = payload["target_timestamp_millis"]
                params["quoteAuthor"] = payload["target_author"]
                if payload.get("quote_text"):
                    params["quoteMessage"] = payload["quote_text"]
            if action == "edit":
                params["editTimestamp"] = payload["target_timestamp_millis"]
        elif action in ("react", "unreact"):
            method = "sendReaction"
            params["emoji"] = payload["emoji"]
            params["targetAuthor"] = payload["target_author"]
            params["targetTimestamp"] = payload["target_timestamp_millis"]
            if payload["remove"]:
                params["remove"] = True
        elif action == "delete":
            method = "remoteDelete"
            params["targetTimestamp"] = payload["target_timestamp_millis"]
        elif action == "mark-read":
            if is_group:
                # sendReceipt addresses an individual recipient. Guessing a
                # per-member fan-out would send receipts the user never asked
                # for, so this is reported as unsupported instead.
                raise UnsupportedActionError(
                    "signal read receipts are not supported for groups",
                    code="unsupported_on_platform",
                )
            method = "sendReceipt"
            params["targetTimestamp"] = payload["target_timestamp_millis"]
            params["type"] = "read"
        elif action in ("typing-start", "typing-stop"):
            method = "sendTyping"
            if action == "typing-stop":
                params["stop"] = True
        else:
            raise UnsupportedActionError(f"signal has no route for {action}")

        return {"jsonrpc": "2.0", "id": secrets.token_hex(8), "method": method, "params": params}

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self.build_request(payload)
        response = self.client.post_json(self.rpc_path, request)
        if response.get("jsonrpc") != "2.0" or response.get("id") != request["id"]:
            raise TransportAmbiguousError(
                "Signal returned an invalid JSON-RPC envelope", code="invalid_bridge_ack"
            )
        error = response.get("error")
        if error is not None:
            raise RemoteRejectedError("Signal JSON-RPC request was rejected", code="remote_rejected")
        if "result" not in response:
            raise TransportAmbiguousError(
                "Signal response omitted its result", code="invalid_bridge_ack"
            )
        result = response["result"]
        timestamp_actions = {"send", "send-file", "reply", "edit", "react", "unreact", "delete"}
        if payload["action"] in timestamp_actions:
            if not isinstance(result, dict) or not isinstance(result.get("timestamp"), (int, str)):
                raise TransportAmbiguousError(
                    "Signal response had an unexpected result shape", code="invalid_bridge_ack"
                )
            remote_ref = str(result["timestamp"])
            if not remote_ref.isdigit() or int(remote_ref) <= 0:
                raise TransportAmbiguousError(
                    "Signal response had an invalid timestamp result", code="invalid_bridge_ack"
                )
            recipient_state = _classify_signal_recipient_results(result)
            if recipient_state == "rejected" and not payload["target"].startswith("group:"):
                raise RemoteRejectedError(
                    "Signal recipient rejected the request", code="remote_rejected"
                )
            if recipient_state in {"rejected", "partial"}:
                raise TransportAmbiguousError(
                    "Signal recipient acknowledgement was not uniformly successful"
                )
            if recipient_state == "malformed":
                raise TransportAmbiguousError(
                    "Signal response had malformed recipient results", code="invalid_bridge_ack"
                )
        elif result is not None:
            remote_ref = ""
        else:
            raise TransportAmbiguousError(
                "Signal response had an unexpected result shape", code="invalid_bridge_ack"
            )
        return {"remote_ref": remote_ref}


def _classify_signal_recipient_results(result: dict[str, Any]) -> str:
    """Classify optional signal-cli per-recipient acknowledgements.

    The returned state is intentionally categorical: remote error bodies and
    recipient addresses must never escape into intent results or audit rows.
    """
    if "results" not in result:
        return "not_reported"
    entries = result["results"]
    if not isinstance(entries, list) or not entries:
        return "malformed"

    confirmed_success = False
    explicit_failure = False
    malformed = False
    for entry in entries:
        if not isinstance(entry, dict):
            malformed = True
            continue

        entry_success = False
        entry_failure = False
        if "type" in entry:
            result_type = entry["type"]
            if not isinstance(result_type, str) or not result_type:
                malformed = True
            elif result_type == "SUCCESS":
                entry_success = True
            else:
                entry_failure = True
        if "success" in entry:
            success = entry["success"]
            if success is True:
                entry_success = True
            elif success is False:
                entry_failure = True
            else:
                malformed = True
        if any(entry.get(key) is not None for key in ("error", "failure")):
            entry_failure = True
        if not entry_success and not entry_failure:
            malformed = True

        confirmed_success = confirmed_success or entry_success
        explicit_failure = explicit_failure or entry_failure

    if malformed:
        return "malformed"
    if explicit_failure and confirmed_success:
        return "partial"
    if explicit_failure:
        return "rejected"
    return "success"


# --------------------------------------------------------------------------
# Slack external routing (render only, never execute)
# --------------------------------------------------------------------------

#: Composio tool slugs this controller is confident about. Anything absent is
#: reported as unsupported rather than guessed: inventing a slug would make the
#: companion skill fail at call time, or worse, call the wrong tool.
#: Argument schemas were independently verified live against Composio on
#: 2026-07-21: send/reply use channel+markdown_text(+thread_ts), reactions use
#: channel+name+timestamp, edit uses channel+ts+markdown_text, and delete/read
#: use channel+ts. No volatile Composio connection id is embedded here.
SLACK_TOOL_ROUTES = {
    "send": "SLACK_SEND_MESSAGE",
    "reply": "SLACK_SEND_MESSAGE",
    "react": "SLACK_ADD_REACTION_TO_AN_ITEM",
    "unreact": "SLACK_REMOVE_REACTION_FROM_ITEM",
    "edit": "SLACK_UPDATES_A_SLACK_MESSAGE",
    "delete": "SLACK_DELETES_A_MESSAGE_FROM_A_CHAT",
    "mark-read": "SLACK_SET_READ_CURSOR_IN_A_CONVERSATION",
}

SLACK_UNSUPPORTED_REASON = {
    "send-file": "local paths cannot be converted safely to a Composio FileUploadable",
    "typing-start": "Slack typing indicators are not exposed as a Composio tool",
    "typing-stop": "Slack typing indicators are not exposed as a Composio tool",
}


def slack_plan_hash(payload_hash: str, intent_id: str, route: dict[str, Any]) -> str:
    """Deterministically bind a rendered plan to its immutable intent payload."""
    return hashlib.sha256(
        _canonical_json(
            {
                "intent_id": intent_id,
                "payload_hash": payload_hash,
                "route": route,
            }
        ).encode("utf-8")
    ).hexdigest()


def render_slack_external_action(intent: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Describe the authenticated call the Hermes skill must make.

    This never contacts Slack. It returns routing data only, so the decision to
    actually write to Slack still happens in the authenticated layer.
    """
    action = payload["action"]
    tool = SLACK_TOOL_ROUTES.get(action)
    if tool is None:
        raise UnsupportedActionError(
            f"slack {action} has no verified Composio route: {SLACK_UNSUPPORTED_REASON.get(action, 'unknown tool')}",
            code="unsupported_on_platform",
            platform="slack",
            action=action,
        )

    channel = payload["target"]
    if action == "send":
        arguments: dict[str, Any] = {"channel": channel, "markdown_text": payload["message"]}
    elif action == "reply":
        arguments = {
            "channel": channel,
            "markdown_text": payload["message"],
            "thread_ts": payload["thread_ts"],
        }
    elif action in ("react", "unreact"):
        arguments = {
            "channel": channel,
            "name": payload["emoji"],
            "timestamp": payload["target_message_id"],
        }
    elif action == "edit":
        arguments = {
            "channel": channel,
            "ts": payload["target_message_id"],
            "markdown_text": payload["message"],
        }
    elif action in ("delete", "mark-read"):
        arguments = {"channel": channel, "ts": payload["target_message_id"]}
    else:  # defensive: the route table and cases above must remain in lockstep
        raise UnsupportedActionError(
            f"slack {action} is unsupported",
            code="unsupported_on_platform",
        )

    route = {
        "provider": "composio",
        "connection": payload["binding"]["connector_id"],
        "tool_slug": tool,
        "arguments": arguments,
    }
    return {
        "status": "unexecuted_plan",
        "executed": False,
        "delivery_status": "not_dispatched",
        "intent_id": intent["intent_id"],
        "platform": "slack",
        "action": action,
        "payload_hash": intent["payload_hash"],
        "plan_hash": slack_plan_hash(intent["payload_hash"], intent["intent_id"], route),
        "expires_ts": intent["expires_ts"],
        "route": route,
        "next_step": (
            "Only inside Hermes' native allow-once approved connector boundary, invoke the "
            "authenticated Composio tool, then record its categorical result."
        ),
    }


def redact_slack_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Return plan metadata without message text, channel ids, or other refs."""
    route = plan["route"]
    return {
        **{key: value for key, value in plan.items() if key not in {"route", "next_step"}},
        "route": {
            "provider": route["provider"],
            "connection": route["connection"],
            "tool_slug": route["tool_slug"],
            "argument_fields": sorted(route["arguments"]),
            "redacted": True,
        },
        "next_step": "Use a deliberate local reveal only inside Hermes' native approved tool boundary.",
    }


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------


@dataclass
class ExecutionConfig:
    whatsapp_url: str = DEFAULT_WHATSAPP_URL
    signal_url: str = DEFAULT_SIGNAL_URL
    signal_account: str = ""
    timeout: float = HTTP_TIMEOUT_SECONDS
    allow_non_loopback: bool | None = None
    client_factory: Callable[[str, float, bool | None], LoopbackHttpClient] | None = None

    def client(self, base_url: str) -> LoopbackHttpClient:
        if self.client_factory is not None:
            return self.client_factory(base_url, self.timeout, self.allow_non_loopback)
        return LoopbackHttpClient(
            base_url, timeout=self.timeout, allow_non_loopback=self.allow_non_loopback
        )


def validate_execution_binding(payload: dict[str, Any], config: ExecutionConfig) -> None:
    """Refuse runtime connector/account substitution after intent preparation."""
    binding = payload.get("binding")
    if not isinstance(binding, dict):
        raise IntentStateError("intent has no execution binding", code="intent_binding_missing")
    platform = payload.get("platform")
    if platform == "whatsapp":
        runtime_url = assert_loopback(
            config.whatsapp_url, allow_override=config.allow_non_loopback
        )
        if runtime_url != binding.get("endpoint_url"):
            raise ValidationError(
                "WhatsApp endpoint differs from the prepared intent",
                code="execution_binding_mismatch",
            )
    elif platform == "signal":
        runtime_url = assert_loopback(config.signal_url, allow_override=config.allow_non_loopback)
        runtime_account = config.signal_account or os.environ.get(SIGNAL_ACCOUNT_ENV, "").strip()
        if runtime_url != binding.get("endpoint_url") or runtime_account != binding.get(
            "execution_account"
        ):
            raise ValidationError(
                "Signal endpoint/account differs from the prepared intent",
                code="execution_binding_mismatch",
            )
    elif platform == "slack":
        if not binding.get("connector_id"):
            raise IntentStateError("Slack intent has no connector binding", code="intent_binding_missing")


def build_adapter(platform: str, config: ExecutionConfig, payload: dict[str, Any] | None = None):
    binding = (payload or {}).get("binding", {})
    if platform == "whatsapp":
        return WhatsAppAdapter(config.client(binding.get("endpoint_url") or config.whatsapp_url))
    if platform == "signal":
        account = binding.get("execution_account") or config.signal_account or os.environ.get(
            SIGNAL_ACCOUNT_ENV, ""
        ).strip()
        return SignalAdapter(config.client(binding.get("endpoint_url") or config.signal_url), account)
    raise ValidationError(f"no local adapter for platform {platform}")


def execute_intent(
    store: ActionStore,
    intent_id: str,
    *,
    acknowledgement: str,
    config: ExecutionConfig,
    dry_run: bool = False,
    reveal_local_plan: bool = False,
) -> dict[str, Any]:
    """Dispatch one intent after Hermes has enforced native allow-once approval.

    The acknowledgement below is public and is not an authorization mechanism.
    """
    if acknowledgement != ACKNOWLEDGEMENT_TOKEN:
        raise AcknowledgementError(
            f"execute-action requires the non-secret --acknowledgement {ACKNOWLEDGEMENT_TOKEN}; "
            "Hermes must separately enforce native allow-once approval before this call",
            code="acknowledgement_required",
        )

    intent = store.load_intent(intent_id)
    payload = store.verify_executable(intent)
    validate_execution_binding(payload, config)
    summary = summarize_intent(intent)

    if intent["platform"] == "slack":
        full_route = render_slack_external_action(intent, payload)
        route = full_route if reveal_local_plan else redact_slack_plan(full_route)
        if dry_run:
            return {**route, "dry_run": True, "intent": summary}
        # Claimed, but explicitly not "delivered": only the authenticated
        # Composio call can decide that, reported back via record-external-result.
        store.claim(intent, next_status=STATUS_EXTERNAL_PENDING)
        store.audit_only(
            intent,
            "external_route",
            STATUS_EXTERNAL_PENDING,
            full_route["route"]["tool_slug"],
        )
        return {**route, "intent": {**summary, "status": STATUS_EXTERNAL_PENDING}}

    adapter = build_adapter(intent["platform"], config, payload)
    # Building the request first surfaces unsupported combinations before the
    # intent is claimed, so the user can prepare a corrected one.
    request_preview = adapter.build_request(payload)

    if dry_run:
        store.verify_staged_attachments(intent, payload)
        return {
            "status": "dry_run",
            "intent": summary,
            "would_call": _describe_request(intent["platform"], request_preview),
            "claimed": False,
            "note": "no intent claimed, no network call made",
        }

    store.claim(intent)

    try:
        # Verify the immutable staged bytes after the atomic claim and before
        # any transport call. A mismatch is a proven local failure.
        store.verify_staged_attachments(intent, payload)
        result = adapter.execute(payload)
    except TransportAmbiguousError as exc:
        # Delivery is unknown. Marking this uncertain (never pending) preserves
        # local at-most-once dispatch: no automatic retry can resend the payload.
        store.finish(
            intent,
            status=STATUS_UNCERTAIN,
            result_code=exc.code,
            detail=_sanitize_detail(exc),
        )
        return {
            "status": STATUS_UNCERTAIN,
            "code": exc.code,
            "intent": {**summary, "status": STATUS_UNCERTAIN},
            "error": _sanitize_detail(exc),
            "guidance": "delivery is unknown; verify in the app before preparing another intent",
        }
    except RemoteRejectedError as exc:
        # A negative application acknowledgement arrived only after the POST.
        # A buggy bridge could have acted before emitting it, so fail closed.
        store.finish(
            intent,
            status=STATUS_UNCERTAIN,
            result_code=exc.code,
        )
        return {
            "status": STATUS_UNCERTAIN,
            "code": exc.code,
            "intent": {**summary, "status": STATUS_UNCERTAIN},
            "error": exc.code,
            "guidance": "delivery is unknown; verify in the app before preparing another intent",
        }
    except (TransportPreflightError, UnsupportedActionError, ValidationError) as exc:
        store.finish(intent, status=STATUS_FAILED, result_code=exc.code, detail=_sanitize_detail(exc))
        return {
            "status": STATUS_FAILED,
            "code": exc.code,
            "intent": {**summary, "status": STATUS_FAILED},
            "error": _sanitize_detail(exc),
            "guidance": "nothing was delivered; prepare a new intent to retry",
        }
    except Exception as exc:  # noqa: BLE001 - unknown failures are ambiguous by default
        store.finish(intent, status=STATUS_UNCERTAIN, result_code="unexpected_error", detail=type(exc).__name__)
        return {
            "status": STATUS_UNCERTAIN,
            "code": "unexpected_error",
            "intent": {**summary, "status": STATUS_UNCERTAIN},
            "error": type(exc).__name__,
            "guidance": "delivery is unknown; verify in the app before preparing another intent",
        }

    store.finish(
        intent,
        status=STATUS_SUCCEEDED,
        result_code="delivered",
        remote_ref=result.get("remote_ref", ""),
    )
    return {
        "status": STATUS_SUCCEEDED,
        "intent": {**summary, "status": STATUS_SUCCEEDED},
        "remote_ref": _sanitize_detail(result.get("remote_ref", ""), 120),
    }


def _prepare_whatsapp_intent(
    *,
    text: str,
    group: str | None,
    conversation: str | None,
    target: str | None,
    context_db: Path | str,
    actions_db: Path | str,
    whatsapp_url: str | None,
    ttl_seconds: int,
    account: str | None,
    workspace: str | None,
    config: ExecutionConfig | None,
) -> tuple[ActionStore, dict[str, Any]]:
    """Create one WhatsApp send intent without making a transport call."""
    selectors = (group, conversation, target)
    if sum(selector is not None for selector in selectors) != 1:
        raise ValidationError(
            "exactly one of group, conversation, or target is required",
            code="destination_selector_invalid",
        )
    if group is not None and not group.strip():
        raise ValidationError("group must not be blank", code="destination_selector_invalid")
    if conversation is not None and not conversation.strip():
        raise ValidationError("conversation must not be blank", code="destination_selector_invalid")

    # A supplied config is a test/orchestration runtime choice, not a bypass;
    # an explicit whatsapp_url that disagrees with it would otherwise leave a
    # pending intent bound to a URL that execute_intent later refuses,
    # orphaning the intent. Catch the conflict before any intent is created.
    if whatsapp_url is not None and config is not None:
        explicit_url = assert_loopback(whatsapp_url, allow_override=config.allow_non_loopback)
        config_url = assert_loopback(config.whatsapp_url, allow_override=config.allow_non_loopback)
        if explicit_url != config_url:
            raise ValidationError(
                "whatsapp_url conflicts with the execution config bound to this intent",
                code="execution_binding_mismatch",
            )

    resolved_target = target
    canonical_conversation_id: str | None = None
    resolved_account = account
    resolved_workspace = workspace
    if group is not None or conversation is not None:
        selector = group if group is not None else conversation
        conn = open_context_db(context_db)
        try:
            resolved = resolve_conversation(
                conn,
                "whatsapp",
                selector or "",
                account=account,
                workspace=workspace,
            )
        finally:
            conn.close()
        if group is not None and resolved["conversation_type"] != "group":
            raise ValidationError(
                "the selected WhatsApp conversation is not a group",
                code="not_a_group",
            )
        resolved_target = resolved.get("_action_target_id") or resolved["conversation_id"]
        canonical_conversation_id = resolved["conversation_id"]
        resolved_account = resolved["account"]
        resolved_workspace = resolved["workspace"]

    # A supplied config is a test/orchestration runtime choice, not a bypass:
    # its URL is bound into the intent and execute_intent validates it again.
    endpoint_url = whatsapp_url
    if endpoint_url is None:
        endpoint_url = config.whatsapp_url if config is not None else DEFAULT_WHATSAPP_URL
    prepared = build_payload(
        platform="whatsapp",
        action="send",
        target=resolved_target,
        conversation_id=canonical_conversation_id,
        account=resolved_account,
        workspace=resolved_workspace,
        endpoint_url=endpoint_url,
        message=text,
    )
    store = ActionStore(actions_db)
    intent = store.create_intent(prepared, ttl_seconds=ttl_seconds)
    return store, intent


def prepare_whatsapp(
    *,
    text: str,
    group: str | None = None,
    conversation: str | None = None,
    target: str | None = None,
    context_db: Path | str = DEFAULT_CONTEXT_DB,
    actions_db: Path | str = DEFAULT_ACTIONS_DB,
    whatsapp_url: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    account: str | None = None,
    workspace: str | None = None,
    config: ExecutionConfig | None = None,
) -> dict[str, Any]:
    """Prepare one redacted, offline WhatsApp send intent.

    This function stores an expiring pending intent only; it never opens a
    transport connection. ``timeout`` is accepted for API symmetry with
    :func:`send_whatsapp` and is intentionally unused during preparation.
    """
    del timeout
    _store, intent = _prepare_whatsapp_intent(
        text=text,
        group=group,
        conversation=conversation,
        target=target,
        context_db=context_db,
        actions_db=actions_db,
        whatsapp_url=whatsapp_url,
        ttl_seconds=ttl_seconds,
        account=account,
        workspace=workspace,
        config=config,
    )
    return {
        "status": "prepared",
        "intent": summarize_intent({**intent, "attempts": 0, "result_code": ""}),
        "context": {},
        "next_step": (
            "Hermes must obtain native allow-once approval for this exact mutation before "
            f"executing intent {intent['intent_id']}. The acknowledgement is not authorization."
        ),
    }


def send_whatsapp(
    *,
    text: str,
    group: str | None = None,
    conversation: str | None = None,
    target: str | None = None,
    context_db: Path | str = DEFAULT_CONTEXT_DB,
    actions_db: Path | str = DEFAULT_ACTIONS_DB,
    whatsapp_url: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    account: str | None = None,
    workspace: str | None = None,
    config: ExecutionConfig | None = None,
) -> dict[str, Any]:
    """Prepare and dispatch one WhatsApp send through the intent pipeline.

    The caller MUST already have enforced native allow-once user approval for
    this exact external mutation. The acknowledgement passed internally is a
    public accidental-invocation guard, not authorization. This helper makes
    exactly one execute attempt and never retries an uncertain delivery.
    """
    store, intent = _prepare_whatsapp_intent(
        text=text,
        group=group,
        conversation=conversation,
        target=target,
        context_db=context_db,
        actions_db=actions_db,
        whatsapp_url=whatsapp_url,
        ttl_seconds=ttl_seconds,
        account=account,
        workspace=workspace,
        config=config,
    )
    runtime_config = config
    if runtime_config is None:
        runtime_config = ExecutionConfig(
            whatsapp_url=whatsapp_url or DEFAULT_WHATSAPP_URL,
            timeout=timeout,
        )
    return execute_intent(
        store,
        intent["intent_id"],
        acknowledgement=ACKNOWLEDGEMENT_TOKEN,
        config=runtime_config,
    )


def _describe_request(platform: str, request_preview: Any) -> dict[str, Any]:
    """Redact user text out of a dry-run preview; show shape, not content."""
    if platform == "whatsapp":
        path, body = request_preview
        return {"transport": "loopback_http", "path": path, "fields": sorted(body)}
    return {
        "transport": "loopback_http_jsonrpc",
        "method": request_preview.get("method", ""),
        "fields": sorted(request_preview.get("params", {})),
    }


def record_external_result(
    store: ActionStore,
    intent_id: str,
    *,
    result: str,
    acknowledgement: str,
    plan_hash: str = "",
    remote_ref: str = "",
) -> dict[str, Any]:
    """Record a native connector's categorical result for the exact plan hash.

    This reports the external outcome; it neither executes Slack nor proves
    user authorization. Hermes' native approved boundary owns both duties.
    """
    if acknowledgement != ACKNOWLEDGEMENT_TOKEN:
        raise AcknowledgementError(
            f"record-external-result requires --acknowledgement {ACKNOWLEDGEMENT_TOKEN}",
            code="acknowledgement_required",
        )
    mapping = {
        "success": (STATUS_SUCCEEDED, "external_delivered"),
        "failed": (STATUS_FAILED, "external_failed"),
        "uncertain": (STATUS_UNCERTAIN, "external_uncertain"),
    }
    if result not in mapping:
        raise ValidationError("result must be one of success, failed, uncertain")

    intent = store.load_intent(intent_id)
    if intent["platform"] != "slack":
        raise IntentStateError(
            "record-external-result only applies to slack intents", code="intent_not_external"
        )
    if intent["status"] != STATUS_EXTERNAL_PENDING:
        raise IntentStateError(
            f"intent {intent_id} is {intent['status']}, not {STATUS_EXTERNAL_PENDING}",
            code="intent_not_external_pending",
            intent_status=intent["status"],
        )
    try:
        payload = json.loads(intent["payload_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise IntentStateError("intent payload is unreadable", code="intent_corrupt") from exc
    if _payload_hash(payload) != intent["payload_hash"] or intent.get(
        "target_handle"
    ) != store._target_handle(payload):
        raise IntentStateError("intent integrity check failed", code="intent_tampered")
    expected_plan_hash = render_slack_external_action(intent, payload)["plan_hash"]
    if not re.fullmatch(r"[0-9a-f]{64}", plan_hash) or not hmac.compare_digest(
        plan_hash, expected_plan_hash
    ):
        raise IntentStateError(
            "external result does not match the rendered plan",
            code="external_plan_mismatch",
        )
    if remote_ref and not SLACK_TS_RE.fullmatch(remote_ref):
        raise ValidationError("Slack remote reference must be a valid timestamp")

    status, code = mapping[result]
    store.finish(
        intent,
        status=status,
        result_code=code,
        remote_ref=remote_ref,
        expected_status=STATUS_EXTERNAL_PENDING,
    )
    return {"status": "recorded", "intent": {**summarize_intent(intent), "status": status}}


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def probe_loopback(url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict[str, Any]:
    """TCP-connect only. Never sends a request, so it can never mutate."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"reachable": True, "detail": "socket accepted"}
    except OSError as exc:
        return {"reachable": False, "detail": _sanitize_detail(type(exc).__name__)}


def build_status(
    *,
    context_db: Path,
    actions_db: Path,
    whatsapp_url: str = DEFAULT_WHATSAPP_URL,
    signal_url: str = DEFAULT_SIGNAL_URL,
    signal_account: str | None = None,
    probe: bool = False,
    prober: Callable[[str, float], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prober = prober or probe_loopback
    context: dict[str, Any] = {"available": False}
    try:
        conn = open_context_db(context_db)
    except MessagingError as exc:
        context["detail"] = exc.code
    else:
        try:
            total = conn.execute("SELECT COUNT(*) AS count FROM context_events").fetchone()["count"]
            rows = conn.execute(
                "SELECT platform, COUNT(*) AS count, MAX(message_ts) AS last_message_ts "
                "FROM context_events GROUP BY platform ORDER BY platform"
            ).fetchall()
            context.update(
                {
                    "available": True,
                    "events": total,
                    "platforms": {
                        row["platform"]: {
                            "events": row["count"],
                            "last_message_ts": row["last_message_ts"] or "",
                        }
                        for row in rows
                    },
                }
            )
        except sqlite3.Error as exc:
            context["detail"] = _sanitize_detail(exc)
        finally:
            conn.close()

    def bridge(url: str, extra: dict[str, Any]) -> dict[str, Any]:
        try:
            normalized = assert_loopback(url)
            loopback_ok = True
            detail = ""
        except ValidationError as exc:
            normalized = url
            loopback_ok = False
            detail = _sanitize_detail(exc)
        info: dict[str, Any] = {"base_url": normalized, "loopback": loopback_ok, **extra}
        if detail:
            info["detail"] = detail
        if probe and loopback_ok:
            info.update(prober(normalized, PROBE_TIMEOUT_SECONDS))
        else:
            info.setdefault("reachable", None)
        return info

    account = signal_account if signal_account is not None else os.environ.get(SIGNAL_ACCOUNT_ENV, "").strip()

    actions: dict[str, Any] = read_action_stats(actions_db)

    return {
        "status": "ok",
        "context_inbox": context,
        "whatsapp": bridge(whatsapp_url, {"route": "loopback_http_bridge", "local_writes": True}),
        "signal": bridge(
            signal_url,
            {
                "route": "loopback_http_jsonrpc",
                "local_writes": True,
                "account_configured": bool(account),
                # Deliberately partial: a full account number is a credential-grade identifier.
                "account_hint": _mask_account(account),
            },
        ),
        "slack": {
            "route": "composio",
            "local_writes": False,
            "read": "local_context_inbox",
            "note": "Slack writes require the authenticated Hermes Composio skill",
            "supported_external_actions": sorted(SLACK_TOOL_ROUTES),
            "unsupported_external_actions": sorted(SLACK_UNSUPPORTED_REASON),
        },
        "actions_db": actions,
        "approval_boundary": {
            "enforced_by": "Hermes native tool approval layer",
            "required": "allow-once approval before every execute-action external mutation",
            "execute_action_registered_as_model_tool": False,
            "controller_acknowledgement": ACKNOWLEDGEMENT_TOKEN,
            "acknowledgement_is_authorization": False,
            "same_uid_adversary_protection": False,
        },
    }


def build_connectivity_probe(
    *,
    whatsapp_url: str = DEFAULT_WHATSAPP_URL,
    signal_url: str = DEFAULT_SIGNAL_URL,
    prober: Callable[[str, float], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prober = prober or probe_loopback

    def one(url: str) -> dict[str, Any]:
        normalized = assert_loopback(url)
        return {"base_url": normalized, **prober(normalized, PROBE_TIMEOUT_SECONDS)}

    return {
        "status": "ok",
        "kind": "explicit_connectivity_probe",
        "whatsapp": one(whatsapp_url),
        "signal": one(signal_url),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_json(data: Any) -> None:
    print(json.dumps(data, sort_keys=True, ensure_ascii=False))


def _render_human(command: str, data: dict[str, Any]) -> str:
    lines: list[str] = []
    if command == "status":
        context = data["context_inbox"]
        lines.append(
            f"context inbox: {'ok' if context['available'] else 'unavailable'} "
            f"({context.get('events', 0)} events)"
        )
        for platform, info in sorted(context.get("platforms", {}).items()):
            lines.append(f"  {platform}: {info['events']} events, last {info['last_message_ts'] or '-'}")
        for name in ("whatsapp", "signal"):
            info = data[name]
            reachable = info.get("reachable")
            state = "online" if reachable else ("offline" if reachable is False else "not probed")
            suffix = ""
            if name == "signal":
                suffix = (
                    f", account {info['account_hint']}"
                    if info["account_configured"]
                    else ", account NOT configured"
                )
            lines.append(f"{name}: {state} at {info['base_url']}{suffix}")
        lines.append("slack: read-only locally; writes route to Composio via the Hermes skill")
        actions = data["actions_db"]
        lines.append(f"intents: {actions.get('total', 0)} total {actions.get('by_status', {})}")
    elif command == "search":
        lines.append(f"{data['count']} result(s){' (truncated)' if data['truncated'] else ''}")
        for item in data["results"]:
            lines.append(
                f"  [{item['message_ts']}] {item['platform']} {item['conversation_name'] or item['conversation_id']} "
                f"<{item['sender_display_name'] or item['sender_id']}> {item['snippet']}"
            )
            lines.append(f"      event_id={item['event_id']}")
    elif command == "conversations":
        lines.append(f"{data['count']} conversation(s){' (truncated)' if data['truncated'] else ''}")
        for item in data["conversations"]:
            lines.append(
                f"  {item['platform']} {item['conversation_name'] or '(unnamed)'} "
                f"[{item['conversation_id']}] {item['message_count']} msgs, last {item['last_message_ts'] or '-'}"
            )
            if item["latest"]:
                lines.append(f"      {item['latest']['snippet']}")
    elif command == "show":
        event = data["event"]
        for key in EVENT_COLUMNS:
            lines.append(f"{key}: {event[key]}")
        lines.append(f"attachments: {event['attachment_count']}")
        lines.append("body:")
        lines.append(event["body"])
    elif command == "list-intents":
        for item in data["intents"]:
            lines.append(
                f"  {item['intent_id']} {item['platform']}/{item['action']} {item['status']} "
                f"expires {item['expires_ts']}"
            )
        if not data["intents"]:
            lines.append("no intents")
    else:
        lines.append(json.dumps(data, sort_keys=True, ensure_ascii=False))
    return "\n".join(lines)


def _add_common_db_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=None, help="Context Inbox SQLite path")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of human output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="personal-message",
        description=(
            "Local personal messaging controller. Hermes must enforce native allow-once "
            "approval before every execute-action call; the CLI acknowledgement is non-secret "
            "and is not authorization."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status", help="local health of context DB, bridges and Slack routing")
    _add_common_db_args(status)
    status.add_argument("--actions-db", type=Path, default=None)
    status.add_argument("--whatsapp-url", default=DEFAULT_WHATSAPP_URL)
    status.add_argument("--signal-url", default=DEFAULT_SIGNAL_URL)

    probe = sub.add_parser("probe", help="explicitly probe loopback bridge connectivity")
    probe.add_argument("--whatsapp-url", default=DEFAULT_WHATSAPP_URL)
    probe.add_argument("--signal-url", default=DEFAULT_SIGNAL_URL)
    probe.add_argument("--json", action="store_true")

    search = sub.add_parser("search", help="search the local Context Inbox")
    _add_common_db_args(search)
    search.add_argument("--query", default="")
    search.add_argument("--platform", choices=PLATFORMS)
    search.add_argument("--conversation")
    search.add_argument("--sender")
    search.add_argument(
        "--direction",
        choices=("inbound", "outbound", "unknown", "incoming", "outgoing"),
        help="filter by direction (incoming/outgoing accepted as aliases)",
    )
    search.add_argument("--since")
    search.add_argument("--limit", type=int, default=SEARCH_LIMIT_DEFAULT)

    conversations = sub.add_parser("conversations", help="list conversations with latest metadata")
    _add_common_db_args(conversations)
    conversations.add_argument("--platform", choices=PLATFORMS)
    conversations.add_argument("--query", default="")
    conversations.add_argument("--limit", type=int, default=CONVERSATION_LIMIT_DEFAULT)

    show = sub.add_parser("show", help="show one event by event_id")
    _add_common_db_args(show)
    show.add_argument("--event-id", required=True)
    show.add_argument("--include-raw", action="store_true", help="include provider raw_json")

    prepare = sub.add_parser(
        "prepare-action", help="create a pending mutation intent (offline, contacts nothing)"
    )
    _add_common_db_args(prepare)
    prepare.add_argument("--actions-db", type=Path, default=None)
    prepare.add_argument("--platform", choices=PLATFORMS, required=True)
    prepare.add_argument("--action", choices=ACTIONS, required=True)
    prepare.add_argument("--target")
    prepare.add_argument("--conversation", help="conversation id or name to resolve to a target")
    prepare.add_argument("--event-id", help="resolve target/quote/message id from a Context Inbox event")
    prepare.add_argument("--account", help="canonical Context Inbox account identity")
    prepare.add_argument("--workspace", help="canonical Context Inbox workspace identity")
    prepare.add_argument("--connector-id", help="connector identity to bind into the intent")
    prepare.add_argument("--whatsapp-url", default=DEFAULT_WHATSAPP_URL)
    prepare.add_argument("--signal-url", default=DEFAULT_SIGNAL_URL)
    prepare.add_argument("--signal-account", default=None)
    prepare.add_argument("--slack-connection", default="hermes_slack_personal")
    message_input = prepare.add_mutually_exclusive_group()
    message_input.add_argument("--message")
    message_input.add_argument(
        "--message-stdin",
        action="store_true",
        help="read the message body from stdin (preferred over command-line text)",
    )
    message_input.add_argument(
        "--message-file",
        type=Path,
        help="read the UTF-8 message body from a no-follow regular file",
    )
    prepare.add_argument("--file", action="append", default=[])
    prepare.add_argument("--emoji")
    prepare.add_argument("--target-message-id")
    prepare.add_argument("--target-timestamp")
    prepare.add_argument("--target-author")
    prepare.add_argument(
        "--target-direction",
        choices=("inbound", "outbound", "incoming", "outgoing"),
    )
    prepare.add_argument("--thread-ts")
    prepare.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)

    execute = sub.add_parser(
        "execute-action",
        help="execute a pending intent after native Hermes allow-once approval",
    )
    execute.add_argument("--intent-id", required=True)
    execute.add_argument("--acknowledgement", required=True)
    execute.add_argument("--actions-db", type=Path, default=None)
    execute.add_argument("--whatsapp-url", default=DEFAULT_WHATSAPP_URL)
    execute.add_argument("--signal-url", default=DEFAULT_SIGNAL_URL)
    execute.add_argument("--signal-account", default=None)
    execute.add_argument("--timeout", type=float, default=HTTP_TIMEOUT_SECONDS)
    execute.add_argument("--dry-run", action="store_true")
    execute.add_argument(
        "--reveal-local-plan",
        action="store_true",
        help="reveal a Slack plan locally inside the native approved tool boundary",
    )
    execute.add_argument("--json", action="store_true")

    record = sub.add_parser(
        "record-external-result", help="close the audit trail after an authenticated Slack call"
    )
    record.add_argument("--intent-id", required=True)
    record.add_argument("--result", choices=("success", "failed", "uncertain"), required=True)
    record.add_argument("--acknowledgement", required=True)
    record.add_argument("--plan-hash", required=True)
    record.add_argument("--remote-ref", default="")
    record.add_argument("--actions-db", type=Path, default=None)
    record.add_argument("--json", action="store_true")

    intents = sub.add_parser("list-intents", help="list recent intents (metadata only)")
    intents.add_argument("--actions-db", type=Path, default=None)
    intents.add_argument("--status")
    intents.add_argument("--limit", type=int, default=25)
    intents.add_argument("--json", action="store_true")

    reconcile = sub.add_parser(
        "reconcile", help="mark stale in-flight/external-pending intents uncertain; never replay"
    )
    reconcile.add_argument("--actions-db", type=Path, default=None)
    reconcile.add_argument("--age-seconds", type=int, default=900)
    reconcile.add_argument("--json", action="store_true")

    purge = sub.add_parser("purge", help="purge old terminal intent payloads and staged artifacts")
    purge.add_argument("--actions-db", type=Path, default=None)
    purge.add_argument("--retention-hours", type=float, default=24.0)
    purge.add_argument("--json", action="store_true")

    return parser


def _context_db(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "db", None) or DEFAULT_CONTEXT_DB).expanduser()


def _actions_db(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "actions_db", None) or DEFAULT_ACTIONS_DB).expanduser()


def _run_prepare(args: argparse.Namespace) -> dict[str, Any]:
    event: dict[str, Any] | None = None
    target = args.target
    account = args.account
    workspace = args.workspace
    canonical_conversation_id: str | None = None
    execution_account = args.signal_account or os.environ.get(SIGNAL_ACCOUNT_ENV, "").strip() or None
    if args.event_id or args.conversation:
        conn = open_context_db(_context_db(args))
        try:
            if args.event_id:
                event = get_event(conn, args.event_id, for_action=True)["event"]
            if args.conversation:
                if event is not None:
                    if args.conversation not in {
                        event["conversation_id"],
                        event.get("conversation_name", ""),
                    }:
                        raise ValidationError(
                            "explicit conversation conflicts with --event-id provenance",
                            code="event_target_conflict",
                        )
                else:
                    resolved = resolve_conversation(
                        conn,
                        args.platform,
                        args.conversation,
                        account=account,
                        workspace=workspace,
                    )
                    resolved_target = resolved.get("_action_target_id") or resolved["conversation_id"]
                    if target is not None and normalize_target(
                        args.platform, target
                    ) != normalize_target(args.platform, resolved_target):
                        raise ValidationError(
                            "explicit target conflicts with --conversation",
                            code="conversation_target_conflict",
                        )
                    target = resolved_target
                    canonical_conversation_id = resolved["conversation_id"]
                    account = resolved["account"]
                    workspace = resolved["workspace"]
                    execution_account = resolved.get("_action_account_id") or execution_account
        finally:
            conn.close()

    endpoint_url = ""
    connector_id = args.connector_id
    if args.platform == "whatsapp":
        endpoint_url = args.whatsapp_url
    elif args.platform == "signal":
        endpoint_url = args.signal_url
    else:
        connector_id = connector_id or args.slack_connection

    prepared = build_payload(
        platform=args.platform,
        action=args.action,
        target=target,
        conversation_id=canonical_conversation_id,
        event=event,
        account=account,
        workspace=workspace,
        connector_id=connector_id,
        endpoint_url=endpoint_url or None,
        execution_account=execution_account,
        message=read_message_argument(args),
        files=args.file,
        emoji=args.emoji,
        target_message_id=args.target_message_id,
        target_timestamp=args.target_timestamp,
        target_author=args.target_author,
        target_direction=args.target_direction,
        thread_ts=args.thread_ts,
    )
    store = ActionStore(_actions_db(args))
    intent = store.create_intent(prepared, ttl_seconds=args.ttl_seconds)
    return {
        "status": "prepared",
        "intent": summarize_intent({**intent, "attempts": 0, "result_code": ""}),
        "context": {
            key: prepared.context[key]
            for key in ("conversation_name", "conversation_type", "direction")
            if prepared.context.get(key)
        },
        "next_step": (
            "Hermes must obtain native allow-once approval for this exact mutation before "
            f"invoking execute-action for {intent['intent_id']}. The CLI acknowledgement is "
            "not authorization."
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    command = args.cmd
    always_json = command in ("prepare-action", "execute-action", "record-external-result")

    try:
        if command == "status":
            data = build_status(
                context_db=_context_db(args),
                actions_db=_actions_db(args),
                whatsapp_url=args.whatsapp_url,
                signal_url=args.signal_url,
                probe=False,
            )
        elif command == "probe":
            data = build_connectivity_probe(
                whatsapp_url=args.whatsapp_url,
                signal_url=args.signal_url,
            )
        elif command in ("search", "conversations", "show"):
            conn = open_context_db(_context_db(args))
            try:
                if command == "search":
                    data = search_events(
                        conn,
                        query=args.query,
                        platform=args.platform,
                        conversation=args.conversation,
                        sender=args.sender,
                        direction=args.direction,
                        since=args.since,
                        limit=args.limit,
                    )
                elif command == "conversations":
                    data = list_conversations(
                        conn, platform=args.platform, query=args.query, limit=args.limit
                    )
                else:
                    data = get_event(conn, args.event_id, include_raw=args.include_raw)
            finally:
                conn.close()
        elif command == "prepare-action":
            data = _run_prepare(args)
        elif command == "execute-action":
            config = ExecutionConfig(
                whatsapp_url=args.whatsapp_url,
                signal_url=args.signal_url,
                signal_account=args.signal_account or "",
                timeout=args.timeout,
            )
            data = execute_intent(
                ActionStore(_actions_db(args)),
                args.intent_id,
                acknowledgement=args.acknowledgement,
                config=config,
                dry_run=args.dry_run,
                reveal_local_plan=args.reveal_local_plan,
            )
        elif command == "record-external-result":
            data = record_external_result(
                ActionStore(_actions_db(args)),
                args.intent_id,
                result=args.result,
                acknowledgement=args.acknowledgement,
                plan_hash=args.plan_hash,
                remote_ref=args.remote_ref,
            )
        elif command == "list-intents":
            data = {
                "status": "ok",
                "intents": list_intents_readonly(
                    _actions_db(args), status=args.status, limit=args.limit
                ),
            }
        elif command == "reconcile":
            data = ActionStore(_actions_db(args)).reconcile_stale(age_seconds=args.age_seconds)
        elif command == "purge":
            if args.retention_hours <= 0:
                raise ValidationError("retention-hours must be positive")
            data = ActionStore(_actions_db(args)).purge_terminal(
                retention_seconds=max(1, int(args.retention_hours * 60 * 60))
            )
        else:  # pragma: no cover - argparse rejects unknown commands
            raise AssertionError(command)
    except MessagingError as exc:
        _print_json(exc.as_dict())
        return 2
    except (sqlite3.Error, OSError) as exc:
        _print_json({"status": "error", "code": "local_failure", "error": _sanitize_detail(exc)})
        return 2

    if always_json or getattr(args, "json", False):
        _print_json(data)
    else:
        print(_render_human(command, data))

    failed_statuses = {STATUS_FAILED, STATUS_UNCERTAIN}
    return 1 if data.get("status") in failed_statuses else 0
