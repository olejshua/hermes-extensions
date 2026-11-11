"""Read-only ingestion of Hermes profile conversations into redacted capsules.

Every source database is opened with ``mode=ro`` and is never written to, not
even to checkpoint: all dreaming state lives in the private Dream store. Only
``active`` user and assistant messages are read. System prompts, tool calls,
tool results, and every reasoning column are structurally excluded by the
SELECT list, so a schema addition cannot silently leak them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence
from urllib.parse import quote

from .config import DreamingConfig, ProfileConfig
from .redaction import redact_detailed

LOG = logging.getLogger(__name__)

# Only these columns are ever selected from `messages`. Anything carrying
# system, tool, or reasoning content is absent by construction.
_MESSAGE_COLUMNS = ("id", "session_id", "role", "content", "timestamp")
_ALLOWED_ROLES = ("user", "assistant")

# Hermes transports also persist machine deliveries as role=user. These are
# structurally known envelopes, not human preferences or confirmed rules.
_MACHINE_DELIVERY_PREFIXES = (
    "[system:", "[important: background process", "[cron delivery:",
    "[async delegation", "[context compaction", "[native sessionübergabe",
    "[native sessionhandoff", "[native context", "[tool result", 
    "die gespeicherten regeln wurden aktualisiert. übernimm sie",
)


def is_machine_delivery(text: str) -> bool:
    return text.lstrip().casefold().startswith(_MACHINE_DELIVERY_PREFIXES)


def _human_origin_bound(origin_json: str, config: DreamingConfig) -> bool:
    try:
        origin = json.loads(origin_json)
    except (ValueError, TypeError):
        return False
    return isinstance(origin, dict) and (
        origin.get("platform") == "telegram" and origin.get("chat_type") == "dm"
        and str(origin.get("user_id")) in config.allowed_telegram_user_ids
        and str(origin.get("chat_id")) == str(origin.get("user_id")))


def _own_reflection_hashes(config: DreamingConfig) -> set[str]:
    """Exclude native mirrors of our own plain-text impulses, without markers."""
    if not config.dream_state_db.is_file() or config.dream_state_db.is_symlink():
        return set()
    result=set()
    try:
        with sqlite3.connect(readonly_uri(config.dream_state_db),uri=True,timeout=.2) as db:
            rows=db.execute("SELECT delivery_snapshot FROM reports WHERE delivery_snapshot<>'' ORDER BY created_at DESC LIMIT 200").fetchall()
        for (raw,) in rows:
            try:
                snapshot=json.loads(raw)
            except (ValueError,TypeError):
                continue
            if isinstance(snapshot,dict) and isinstance(snapshot.get("text"),str):
                result.add(hashlib.sha256(snapshot["text"].strip().encode()).hexdigest())
    except sqlite3.Error:
        pass
    return result

# Content shapes that mean the row is dreaming output rather than a conversation.
_SELF_CONTENT_MARKERS = (
    "<<hermes-dream-prompt>>",
    "hermes dreaming sweep",
    "openviking_dream",
    "dream-run-id:",
    "## deep sleep",
    "## rem sleep",
    "## light sleep",
    "<!-- hermes:dreaming",
)


class IngestError(RuntimeError):
    """Raised when a configured profile database cannot be read."""


@dataclass(frozen=True)
class MessageCapsule:
    """One redacted conversational turn kept as bounded evidence."""

    ref: str
    role: str
    text: str
    timestamp: float | None
    content_hash: str
    redacted: bool
    human_verified: bool | None = None


@dataclass(frozen=True)
class SessionCapsule:
    """A bounded, redacted view of one source session."""

    profile: str
    session_id: str
    source: str
    title: str
    started_at: float | None
    ended_at: float | None
    messages: tuple[MessageCapsule, ...]
    lcm_summaries: tuple[str, ...] = ()
    fingerprint: str = ""
    has_user_content: bool = False

    @property
    def source_key(self) -> str:
        """Stable identity of this session across runs and profiles."""

        return f"{self.profile}:{self.session_id}"

    @property
    def day(self) -> str:
        stamp = self.started_at or self.ended_at
        if stamp is None:
            return "unknown"
        return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")

    def evidence_refs(self) -> tuple[str, ...]:
        refs = [message.ref for message in self.messages]
        refs.extend(f"{self.source_key}#lcm{index}" for index in range(len(self.lcm_summaries)))
        return tuple(refs)

    def character_count(self) -> int:
        return sum(len(message.text) for message in self.messages) + sum(
            len(summary) for summary in self.lcm_summaries
        )


@dataclass
class ProfileScan:
    profile: str
    sessions: list[SessionCapsule] = field(default_factory=list)
    skipped_excluded_source: int = 0
    skipped_self_ingestion: int = 0
    skipped_empty: int = 0
    skipped_archived: int = 0
    skipped_unbound: int = 0
    error: str | None = None


@dataclass(frozen=True)
class LCMBundle:
    texts: tuple[str, ...] = ()
    fingerprint: str = ""


_SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")


def safe_identifier(value: object, *, prefix: str) -> str:
    """Keep ordinary IDs readable but hash anything carrying PII/control text."""

    raw = str(value)
    detail = redact_detailed(raw)
    if detail.redacted or not _SAFE_ID.fullmatch(raw):
        return f"{prefix}_{hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()[:20]}"
    return raw


def readonly_uri(path: Path) -> str:
    """SQLite URI that opens a database strictly read-only.

    ``immutable`` is deliberately not used: the live databases run in WAL mode
    and are written concurrently, so we need normal read-only semantics that
    respect the WAL rather than a snapshot that could read a torn page.
    """

    return f"file:{quote(Path(path).as_posix())}?mode=ro"


def connect_readonly(path: Path, *, timeout: float = 15.0) -> sqlite3.Connection:
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise IngestError(f"profile database not found: {resolved}")
    try:
        conn = sqlite3.connect(readonly_uri(resolved), uri=True, timeout=timeout)
    except sqlite3.Error as exc:
        raise IngestError(f"cannot open {resolved} read-only: {exc}") from exc
    conn.row_factory = sqlite3.Row
    # Belt and braces: even if a caller tried to write, the connection refuses.
    conn.execute("PRAGMA query_only=ON")
    conn.execute(f"PRAGMA busy_timeout={max(1, min(5000, int(timeout * 1000)))}")
    # Hold one read transaction so sessions and their messages come from the
    # same WAL snapshot even while Hermes continues writing.
    conn.execute("BEGIN")
    return conn


def scan_profile(
    profile: ProfileConfig,
    config: DreamingConfig,
    *,
    since: float | None,
    exclude_keys: Iterable[str] = (),
    limit: int | None = None,
    progress_check: Callable[[], None] | None = None,
    sqlite_timeout_seconds: float = 15.0,
) -> ProfileScan:
    """Read eligible sessions for one profile into redacted capsules."""

    scan = ProfileScan(profile=profile.name)
    excluded = set(exclude_keys)
    try:
        conn = connect_readonly(profile.state_db, timeout=max(0.001, sqlite_timeout_seconds))
    except IngestError as exc:
        scan.error = "configured profile database unavailable"
        LOG.warning("dreaming profile unreadable", extra={"fields": {"profile": safe_identifier(profile.name, prefix="profile"), "error": "unavailable"}})
        return scan

    progress_error: list[BaseException] = []
    if progress_check is not None:
        def sqlite_progress() -> int:
            try:
                progress_check()
                return 0
            except BaseException as exc:
                progress_error.append(exc)
                return 1
        conn.set_progress_handler(sqlite_progress, 1_000)
    try:
        if not _has_expected_tables(conn):
            scan.error = "configured profile database is missing required tables"
            return scan
        message_sql = _message_sql(conn)
        eligible_rows: list[sqlite3.Row] = []
        for row in _iter_sessions(conn, config, since=since):
            if progress_check is not None:
                progress_check()
            session_id = str(row["id"])
            key = (
                f"{safe_identifier(profile.name, prefix='profile')}:"
                f"{safe_identifier(session_id, prefix='session')}"
            )
            if key in excluded:
                continue
            source = str(row["source"] or "unknown").lower()
            title = str(row["title"] or "")
            if _is_excluded_source(source, config):
                scan.skipped_excluded_source += 1
                continue
            if config.require_human_binding and not _human_origin_bound(str(row["origin_json"] or "{}"), config):
                scan.skipped_unbound += 1
                continue
            if _looks_like_dream_artifact(title, source, config) and not (config.require_human_binding and source == "telegram"):
                scan.skipped_self_ingestion += 1
                continue
            if int(row["archived"] or 0):
                scan.skipped_archived += 1
                continue
            eligible_rows.append(row)

        lcm_session_ids = _lcm_safe_session_ids(
            conn, [str(row["id"]) for row in eligible_rows],
            progress_check=progress_check,
        )
        summaries = _load_lcm_summaries(
            profile, eligible_session_ids=lcm_session_ids,
            progress_check=progress_check,
            sqlite_timeout_seconds=sqlite_timeout_seconds,
        )
        for row in eligible_rows:
            session_id = str(row["id"])
            source = str(row["source"] or "unknown").lower()
            title = str(row["title"] or "")
            capsule = _build_capsule(
                conn,
                config,
                message_sql=message_sql,
                profile=profile.name,
                session_id=session_id,
                source=source,
                title=title,
                started_at=_float_or_none(row["started_at"]),
                ended_at=_float_or_none(row["ended_at"]),
                lcm_bundle=summaries.get(session_id, LCMBundle()),
                progress_check=progress_check,
                origin_json=str(row["origin_json"] or "{}"),
            )
            if capsule is None:
                scan.skipped_self_ingestion += 1
                continue
            if not capsule.messages and not capsule.lcm_summaries:
                scan.skipped_empty += 1
                continue
            if config.require_human_binding and not any(m.human_verified is True for m in capsule.messages):
                scan.skipped_unbound += 1
                continue
            scan.sessions.append(capsule)
            if limit is not None and len(scan.sessions) >= limit:
                break
    except sqlite3.Error as exc:
        if progress_error:
            raise progress_error[0]
        scan.error = "configured profile database schema/read failure"
        LOG.warning("dreaming profile read failed", extra={"fields": {"profile": safe_identifier(profile.name, prefix="profile"), "error": "schema_or_read_failure"}})
    finally:
        conn.close()
    return scan


def _has_expected_tables(conn: sqlite3.Connection) -> bool:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {"sessions", "messages"} <= tables


def _iter_sessions(conn: sqlite3.Connection, config: DreamingConfig, *, since: float | None) -> Iterator[sqlite3.Row]:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    selected = ["id", "source", "title", "started_at", "ended_at"]
    missing = [name for name in selected if name not in columns]
    if missing:
        raise sqlite3.DatabaseError(f"sessions missing columns: {', '.join(missing)}")
    archived_expr = "archived" if "archived" in columns else "0 AS archived"
    origin_expr = "s.origin_json" if "origin_json" in columns else "'{}' AS origin_json"
    sql = f"SELECT {', '.join(f's.{name}' for name in selected)}, {archived_expr}, {origin_expr} FROM sessions AS s"
    params: list[object] = []
    if since is not None:
        # A long-running interactive session can be older than the lookback
        # while still receiving new turns.  The correlated session_id lookup
        # keeps the tail test indexable, and every value remains parameterized.
        sql += """
            WHERE COALESCE(s.ended_at, s.started_at, 0) >= ?
               OR EXISTS (
                    SELECT 1
                    FROM messages AS m
                    WHERE m.session_id=s.id
                      AND m.timestamp>=?
                      AND m.role IN (?, ?)
                      AND m.active=1
                      AND m.content IS NOT NULL
                      AND TRIM(m.content) != ''
               )
        """
        params.extend((since, since, *_ALLOWED_ROLES))
    sql += " ORDER BY COALESCE(s.ended_at, s.started_at, 0) DESC, s.id DESC"
    yield from conn.execute(sql, params)


_SQLITE_ID_CHUNK = 400
_LCM_SUMMARIES_PER_SESSION = 3


def _lcm_safe_session_ids(
    conn: sqlite3.Connection,
    session_ids: Sequence[str],
    *,
    progress_check: Callable[[], None] | None,
) -> tuple[str, ...]:
    """Return sessions whose user/assistant provenance is entirely active.

    LCM summary rows do not identify the source message versions they cover.
    One inactive or superseded conversational row therefore makes every LCM
    summary for that session unsafe; raw ingestion still uses active rows only.
    """

    if not session_ids:
        return ()
    unsafe: set[str] = set()
    for start in range(0, len(session_ids), _SQLITE_ID_CHUNK):
        if progress_check is not None:
            progress_check()
        chunk = session_ids[start : start + _SQLITE_ID_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT DISTINCT session_id FROM messages "
            f"WHERE session_id IN ({placeholders}) AND role IN (?, ?) AND active IS NOT 1",
            (*chunk, *_ALLOWED_ROLES),
        )
        unsafe.update(str(row[0]) for row in rows)
    return tuple(session_id for session_id in session_ids if session_id not in unsafe)


def _message_sql(conn: sqlite3.Connection) -> str:
    """Build the message SELECT once, requiring the deletion fence.

    The column list is fixed rather than ``*`` so a future schema addition
    (another reasoning or tool column) can never widen what we read.
    """

    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    missing = [name for name in _MESSAGE_COLUMNS if name not in columns]
    if "active" not in columns:
        missing.append("active")
    if missing:
        raise sqlite3.DatabaseError(f"messages missing columns: {', '.join(missing)}")
    binding = "platform_message_id" if "platform_message_id" in columns else "NULL AS platform_message_id"
    binding += ", " + ("display_metadata" if "display_metadata" in columns else "NULL AS display_metadata")
    binding += ", " + ("display_kind" if "display_kind" in columns else "NULL AS display_kind")
    return f"""
        SELECT {', '.join(_MESSAGE_COLUMNS)}, {binding}
        FROM messages
        WHERE session_id=?
          AND role IN (?, ?)
          AND content IS NOT NULL
          AND TRIM(content) != ''
          AND active=1
        ORDER BY timestamp, id
    """


def _build_capsule(
    conn: sqlite3.Connection,
    config: DreamingConfig,
    *,
    message_sql: str,
    profile: str,
    session_id: str,
    source: str,
    title: str,
    started_at: float | None,
    ended_at: float | None,
    lcm_bundle: LCMBundle,
    progress_check: Callable[[], None] | None,
    origin_json: str = "{}",
) -> SessionCapsule | None:
    budgets = config.budgets
    rows = conn.execute(message_sql, (session_id, *_ALLOWED_ROLES))
    head_count = min(3, max(1, budgets.messages_per_session // 5))
    tail_count = max(0, budgets.messages_per_session - head_count)
    head: list[MessageCapsule] = []
    tail: deque[MessageCapsule] = deque(maxlen=tail_count or 1)
    digest = hashlib.sha256()
    digest.update(profile.encode("utf-8"))
    digest.update(b"\0")
    digest.update(session_id.encode("utf-8"))
    has_user = False
    seen_transport_ids: set[str] = set()
    own_reflections = _own_reflection_hashes(config)
    origin_bound = _human_origin_bound(origin_json, config)
    for row_index, row in enumerate(rows):
        if progress_check is not None and row_index % 128 == 0:
            progress_check()
        raw = str(row["content"] or "")
        transport_id = str(row["platform_message_id"] or "").strip()
        try:
            display = json.loads(row["display_metadata"] or "{}")
        except (ValueError, TypeError):
            display = {}
        gateway_owner = display.get("gateway_input_owner") if isinstance(display, dict) else None
        human_bound = (origin_bound and source == "telegram" and transport_id.isdecimal()
                       and str(row["role"]) == "user" and row["display_kind"] != "internal_notification"
                       and isinstance(gateway_owner, str) and bool(re.fullmatch(r"[0-9a-f-]{36}", gateway_owner)))
        # Binding beats content: a real user may quote an automated envelope.
        # Markers only discard unbound native delivery copies; never establish
        # that a row came from a human.
        if not human_bound and is_machine_delivery(raw):
            continue
        if not human_bound and hashlib.sha256(raw.strip().encode()).hexdigest() in own_reflections:
            continue
        if human_bound and transport_id in seen_transport_ids:
            continue
        if human_bound:
            seen_transport_ids.add(transport_id)
        if not human_bound and _is_self_content(raw, config):
            # A machine-authored Dream artifact is excluded at row scope. A
            # long-lived chat must still admit later bound human messages,
            # including a human's explicit quotation of an old Dream report.
            continue
        clean_full = redact_detailed(raw.strip())
        text = _clip(clean_full.text, budgets.characters_per_message)
        if not text:
            continue
        role = str(row["role"])
        if config.require_human_binding and role == "user" and not human_bound:
            role = "assistant"
        digest.update(b"\0")
        digest.update(str(row["id"]).encode("utf-8", "replace"))
        digest.update(b"\0")
        digest.update(role.encode("ascii", "replace"))
        digest.update(b"\0")
        digest.update(str(row["timestamp"]).encode("ascii", "replace"))
        digest.update(b"\0")
        # The raw value never leaves this local digest, but hashing it before
        # redaction makes PII-only edits visible to incremental ingestion.
        digest.update(_hash(raw).encode("ascii"))
        ref = _message_ref(profile, session_id, str(row["id"]))
        capsule = MessageCapsule(
            ref=ref,
            role=role,
            text=text,
            timestamp=_float_or_none(row["timestamp"]),
            content_hash=hashlib.sha256(clean_full.text.encode("utf-8")).hexdigest(),
            redacted=clean_full.redacted,
            human_verified=human_bound if config.require_human_binding else None,
        )
        if len(head) < head_count:
            head.append(capsule)
        elif tail_count:
            tail.append(capsule)
        if role == "user":
            has_user = True

    # Always favour recent turns. Keep a tiny beginning anchor where it fits,
    # then fill the remaining character budget from the newest tail backwards.
    messages = list(head)
    used_characters = sum(len(item.text) for item in messages)
    chosen_tail: list[MessageCapsule] = []
    for item in reversed(tail):
        if used_characters + len(item.text) > budgets.characters_per_session:
            continue
        chosen_tail.append(item)
        used_characters += len(item.text)
    messages.extend(reversed(chosen_tail))
    if not messages and tail:
        newest = tail[-1]
        clipped = _clip(newest.text, budgets.characters_per_session)
        messages = [MessageCapsule(newest.ref, newest.role, clipped, newest.timestamp, newest.content_hash, newest.redacted, newest.human_verified)]

    summaries_list: list[str] = []
    summary_characters = 0
    for raw_summary in lcm_bundle.texts:
        if not str(raw_summary).strip():
            continue
        remaining = budgets.characters_per_session - summary_characters
        if remaining <= 0:
            break
        # Redact the complete summary before clipping. Clipping first can cut a
        # credential below the detector's minimum length and leak its prefix.
        redacted_summary = redact_detailed(str(raw_summary).strip()).text
        summary = _clip(redacted_summary, remaining)
        if not _is_self_content(summary, config):
            summaries_list.append(summary)
            summary_characters += len(summary)
    # Never resurrect a deleted/superseded session from stale LCM alone.
    summaries = tuple(summaries_list) if (head or tail) else ()

    safe_profile = safe_identifier(profile, prefix="profile")
    safe_session = safe_identifier(session_id, prefix="session")

    return SessionCapsule(
        profile=safe_profile,
        session_id=safe_session,
        source=safe_identifier(source, prefix="source"),
        title=redact_detailed(_clip(title, 200)).text,
        started_at=started_at,
        ended_at=ended_at,
        messages=tuple(messages),
        lcm_summaries=summaries,
        fingerprint=_session_fingerprint(safe_profile, safe_session, digest.hexdigest(), lcm_bundle.fingerprint),
        has_user_content=has_user,
    )


def _load_lcm_summaries(
    profile: ProfileConfig, *, eligible_session_ids: Sequence[str],
    progress_check: Callable[[], None] | None = None,
    sqlite_timeout_seconds: float = 15.0,
) -> dict[str, LCMBundle]:
    """Best-effort LCM summaries keyed by session id.

    LCM is a preference, not a requirement: a missing or malformed database
    degrades to raw conversational capsules rather than failing the sweep.
    """

    if not profile.lcm_db or not eligible_session_ids:
        return {}
    path = Path(profile.lcm_db).expanduser()
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(readonly_uri(path), uri=True,
                               timeout=max(0.001, sqlite_timeout_seconds))
    except sqlite3.Error as exc:
        LOG.info("dreaming lcm unavailable", extra={"fields": {"profile": profile.name, "error": str(exc)}})
        return {}
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute(f"PRAGMA busy_timeout={max(1, min(5000, int(sqlite_timeout_seconds * 1000)))}")
        conn.execute("BEGIN")
        progress_error: list[BaseException] = []
        if progress_check is not None:
            def sqlite_progress() -> int:
                try:
                    progress_check()
                    return 0
                except BaseException as exc:
                    progress_error.append(exc)
                    return 1
            conn.set_progress_handler(sqlite_progress, 1_000)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "summary_nodes" not in tables:
            return {}
        summaries: dict[str, list[str]] = {}
        digests: dict[str, Any] = {}
        for start in range(0, len(eligible_session_ids), _SQLITE_ID_CHUNK):
            if progress_check is not None:
                progress_check()
            chunk = eligible_session_ids[start : start + _SQLITE_ID_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                "WITH ranked AS ("
                "SELECT session_id, node_id, summary, depth, "
                "ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY depth DESC, node_id) AS row_rank "
                "FROM summary_nodes "
                f"WHERE session_id IN ({placeholders}) "
                "AND summary IS NOT NULL AND TRIM(summary) != ''"
                ") SELECT session_id, node_id, summary, depth FROM ranked "
                "WHERE row_rank<=? ORDER BY session_id, depth DESC, node_id",
                (*chunk, _LCM_SUMMARIES_PER_SESSION),
            )
            for row_index, row in enumerate(rows):
                if progress_check is not None and row_index % 128 == 0:
                    progress_check()
                session_id = str(row["session_id"])
                digest = digests.setdefault(session_id, hashlib.sha256())
                digest.update(str(row["node_id"]).encode("utf-8", "replace"))
                digest.update(b"\0")
                digest.update(str(row["depth"]).encode("ascii", "replace"))
                digest.update(b"\0")
                digest.update(_hash(str(row["summary"])).encode("ascii"))
                digest.update(b"\0")
                bucket = summaries.setdefault(session_id, [])
                if len(bucket) < _LCM_SUMMARIES_PER_SESSION:
                    bucket.append(str(row["summary"]))
        return {
            key: LCMBundle(tuple(value), digests[key].hexdigest()[:32])
            for key, value in summaries.items()
        }
    except sqlite3.Error as exc:
        if 'progress_error' in locals() and progress_error:
            raise progress_error[0]
        LOG.info("dreaming lcm read failed", extra={"fields": {"profile": safe_identifier(profile.name, prefix="profile"), "error": "unavailable"}})
        return {}
    finally:
        conn.close()


def _is_excluded_source(source: str, config: DreamingConfig) -> bool:
    if any(
        source == marker or source.startswith(f"{marker}:") or source.startswith(f"{marker}/")
        for marker in config.excluded_session_sources
    ):
        return True
    if config.included_session_sources and source not in config.included_session_sources:
        return True
    return False


def _looks_like_dream_artifact(title: str, source: str, config: DreamingConfig) -> bool:
    haystack = f"{title} {source}".lower()
    return any(marker in haystack for marker in config.self_markers)


def _is_self_content(text: str, config: DreamingConfig) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in _SELF_CONTENT_MARKERS):
        return True
    return any(marker in lowered for marker in config.self_markers)


def batch_sessions(sessions: Sequence[SessionCapsule], config: DreamingConfig) -> list[list[SessionCapsule]]:
    """Split sessions into rounds bounded by both session count and characters."""

    budgets = config.budgets
    batches: list[list[SessionCapsule]] = []
    current: list[SessionCapsule] = []
    current_chars = 0
    for session in sessions:
        size = session.character_count()
        too_many = len(current) >= budgets.sessions_per_round
        too_large = current and current_chars + size > budgets.characters_per_batch
        if too_many or too_large:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(session)
        current_chars += size
    if current:
        batches.append(current)
    return batches


def _message_ref(profile: str, session_id: str, message_id: str) -> str:
    return (
        f"{safe_identifier(profile, prefix='profile')}:"
        f"{safe_identifier(session_id, prefix='session')}#m"
        f"{safe_identifier(message_id, prefix='message')}"
    )


def parse_ref(ref: str) -> tuple[str, str, str] | None:
    """Split ``profile:session#mID`` back into its parts."""

    if "#" not in ref or ":" not in ref:
        return None
    locator, _, suffix = ref.partition("#")
    profile, _, session_id = locator.partition(":")
    if not profile or not session_id or not suffix:
        return None
    return profile, session_id, suffix


def _session_fingerprint(
    profile: str,
    session_id: str,
    full_message_digest: str,
    lcm_fingerprint: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(profile.encode("utf-8"))
    digest.update(b"\0")
    digest.update(session_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(full_message_digest.encode("ascii"))
    digest.update(b"\0")
    digest.update(lcm_fingerprint.encode("ascii", "replace"))
    return digest.hexdigest()[:32]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
