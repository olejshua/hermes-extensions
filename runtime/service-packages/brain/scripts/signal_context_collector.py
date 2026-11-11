#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import random
import re
import signal
import time
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

DEFAULT_HTTP_URL = "http://127.0.0.1:8080"
DEFAULT_SPOOL = "~/.hermes/second-brain/context-inbox-spool.jsonl"
MAX_EVENT_BYTES = 8 * 1024 * 1024
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0

LOGGER = logging.getLogger("signal_context_collector")


class SSEEventTooLarge(ValueError):
    pass


class Shutdown:
    def __init__(self) -> None:
        self.requested = False

    def request(self, signum: int, _frame: object) -> None:
        self.requested = True
        LOGGER.info("shutdown requested by signal %s", signum)


class SSEParser:
    """Small SSE parser that preserves data across transport chunk boundaries."""

    def __init__(self, max_event_bytes: int = MAX_EVENT_BYTES) -> None:
        self.max_event_bytes = max_event_bytes
        self._buffer = ""
        self._data_lines: list[str] = []
        self._event_bytes = 0

    def feed(self, chunk: bytes | str) -> list[str]:
        if isinstance(chunk, bytes):
            text = chunk.decode("utf-8", "replace")
        else:
            text = chunk
        self._buffer += text
        events: list[str] = []
        while True:
            line, found = self._pop_line()
            if not found:
                break
            event = self._process_line(line)
            if event is not None:
                events.append(event)
        return events

    def close(self) -> list[str]:
        events: list[str] = []
        if self._buffer:
            event = self._process_line(self._buffer)
            self._buffer = ""
            if event is not None:
                events.append(event)
        event = self._dispatch()
        if event is not None:
            events.append(event)
        return events

    def _pop_line(self) -> tuple[str, bool]:
        positions = [pos for pos in (self._buffer.find("\n"), self._buffer.find("\r")) if pos >= 0]
        if not positions:
            return "", False
        pos = min(positions)
        line = self._buffer[:pos]
        next_pos = pos + 1
        if self._buffer[pos : pos + 2] == "\r\n":
            next_pos = pos + 2
        self._buffer = self._buffer[next_pos:]
        return line, True

    def _process_line(self, line: str) -> str | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field != "data":
            return None
        self._event_bytes += len(value.encode("utf-8")) + 1
        if self._event_bytes > self.max_event_bytes:
            self._reset_event()
            raise SSEEventTooLarge(f"SSE event exceeded {self.max_event_bytes} bytes")
        self._data_lines.append(value)
        return None

    def _dispatch(self) -> str | None:
        if not self._data_lines:
            self._reset_event()
            return None
        data = "\n".join(self._data_lines)
        self._reset_event()
        return data

    def _reset_event(self) -> None:
        self._data_lines = []
        self._event_bytes = 0


def redact_account(account: str) -> str:
    if not account:
        return ""
    suffix = re.sub(r"\D", "", account)[-2:]
    digest = hashlib.sha256(account.encode("utf-8")).hexdigest()[:10]
    return f"account:{digest}:**{suffix}"


def account_key(account: str) -> str:
    return "signal-account:" + hashlib.sha256(account.encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ms_to_iso(value: Any) -> str:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    try:
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def stable_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_digest(data: Any, length: int = 24) -> str:
    return hashlib.sha256(stable_json(data).encode("utf-8")).hexdigest()[:length]


def best_identifier(data: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)
    return ""


def normalize_signal_event(event: dict[str, Any], account: str) -> dict[str, Any] | None:
    envelope = event.get("envelope", event)
    if not isinstance(envelope, dict):
        return None

    direction = "inbound"
    data_message = envelope.get("dataMessage")
    is_edit = False
    sync_sent = None

    edit_message = envelope.get("editMessage")
    if not isinstance(data_message, dict) and isinstance(edit_message, dict):
        candidate = edit_message.get("dataMessage")
        if isinstance(candidate, dict):
            data_message = candidate
            is_edit = True

    sync_message = envelope.get("syncMessage")
    if not isinstance(data_message, dict) and isinstance(sync_message, dict):
        candidate = sync_message.get("sentMessage")
        if isinstance(candidate, dict):
            sync_sent = candidate
            data_message = candidate
            direction = "outbound"

    if not isinstance(data_message, dict):
        return None

    body = str(data_message.get("message") or "")
    attachments = extract_attachments(data_message.get("attachments") or [])
    if not body.strip() and not attachments:
        return None

    group_info = data_message.get("groupInfo") if isinstance(data_message.get("groupInfo"), dict) else {}
    group_id = str(group_info.get("groupId") or "")
    is_group = bool(group_id)

    if direction == "outbound":
        sender_id = account_key(account)
        sender_name = "Signal account"
        peer = best_identifier(sync_sent or data_message, ("destinationNumber", "destinationUuid", "destination"))
        if not peer:
            peer = account_key(account)
    else:
        sender_id = best_identifier(envelope, ("sourceNumber", "sourceUuid", "source"))
        sender_name = str(envelope.get("sourceName") or "")
        peer = sender_id
        if not is_group and not peer:
            return None

    if is_group:
        conversation_id = f"group:{group_id}"
        conversation_type = "group"
        conversation_name = str(group_info.get("groupName") or "")
    else:
        conversation_id = "dm:" + stable_digest({"peer": peer}, 16)
        conversation_type = "dm"
        conversation_name = sender_name if direction == "inbound" else ""

    timestamp_ms = (
        data_message.get("timestamp")
        or envelope.get("timestamp")
        or (edit_message or {}).get("targetSentTimestamp")
        or (sync_sent or {}).get("timestamp")
    )
    message_ts = ms_to_iso(timestamp_ms)
    if not message_ts:
        message_ts = now_iso()

    source_message_id = build_source_message_id(
        direction=direction,
        sender_or_account=sender_id if direction == "inbound" else account_key(account),
        conversation_id=conversation_id,
        timestamp_ms=timestamp_ms,
        edit=is_edit,
        body=body,
        attachments=attachments,
    )
    target_source_message_id = ""
    target_message_ts = ""
    target_sender_id = ""
    if is_edit and isinstance(edit_message, dict):
        target_timestamp_ms = edit_message.get("targetSentTimestamp") or data_message.get("targetSentTimestamp")
        target_message_ts = ms_to_iso(target_timestamp_ms)
        target_sender_id = best_identifier(
            edit_message,
            ("targetAuthorNumber", "targetAuthorUuid", "targetAuthor"),
        ) or (sender_id if direction == "inbound" else account_key(account))
        target_source_message_id = build_source_message_id(
            direction=direction,
            sender_or_account=target_sender_id,
            conversation_id=conversation_id,
            timestamp_ms=target_timestamp_ms,
            edit=False,
            body="",
            attachments=[],
        )

    quote = data_message.get("quote") if isinstance(data_message.get("quote"), dict) else {}
    flags: list[str] = []
    if is_edit:
        flags.append("edit")

    raw = {
        "envelope": envelope,
        "observed_by": "signal_context_collector",
    }
    if is_edit:
        raw["edit_linkage"] = {
            "target_source_message_id": target_source_message_id,
            "target_sent_timestamp": str((edit_message or {}).get("targetSentTimestamp") or ""),
            "target_message_ts": target_message_ts,
            "target_sender_id": target_sender_id,
        }

    record = {
        "platform": "signal",
        "account": account_key(account),
        "workspace": "",
        # Keep privacy-preserving display identity separate from identifiers
        # required by signal-cli actions. Context Inbox summaries omit these.
        "action_target_id": conversation_id if is_group else peer,
        "action_account_id": account,
        "conversation_id": conversation_id,
        "conversation_name": conversation_name,
        "conversation_type": conversation_type,
        "sender_id": sender_id,
        "sender_display_name": sender_name,
        "direction": direction,
        "body": body,
        "message_ts": message_ts,
        "received_ts": now_iso(),
        "ingested_ts": now_iso(),
        "source_message_id": source_message_id,
        "thread_id": str(quote.get("id") or ""),
        "attachments": attachments,
        "flags": flags,
        "reply_metadata": extract_reply_metadata(quote),
        "raw": raw,
    }
    if target_source_message_id:
        record["target_source_message_id"] = target_source_message_id
        record["target_message_ts"] = target_message_ts
        record["target_sender_id"] = target_sender_id
    return record


def build_source_message_id(
    *,
    direction: str,
    sender_or_account: str,
    conversation_id: str,
    timestamp_ms: Any,
    edit: bool,
    body: str,
    attachments: list[dict[str, Any]],
) -> str:
    payload = {
        "direction": direction,
        "sender_or_account": sender_or_account,
        "conversation_id": conversation_id,
        "timestamp_ms": str(timestamp_ms or ""),
        "edit": edit,
    }
    if edit:
        payload["body_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        payload["attachment_ids"] = [str(att.get("id") or "") for att in attachments]
    marker = "edit" if edit else "msg"
    return f"signal:{marker}:{direction}:{stable_digest(payload, 32)}"


def extract_reply_metadata(quote: dict[str, Any]) -> dict[str, Any]:
    if not quote:
        return {}
    author = quote.get("author") if isinstance(quote.get("author"), dict) else {}
    return {
        "id": str(quote.get("id") or ""),
        "author": quote.get("author") if isinstance(quote.get("author"), str) else "",
        "author_number": str(author.get("number") or quote.get("authorNumber") or ""),
        "author_uuid": str(author.get("uuid") or quote.get("authorUuid") or ""),
        "author_name": str(quote.get("authorName") or quote.get("authorProfileName") or ""),
        "text": str(quote.get("text") or ""),
    }


def extract_attachments(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    attachments: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        meta: dict[str, Any] = {}
        for source, target in (
            ("id", "id"),
            ("contentType", "contentType"),
            ("fileName", "fileName"),
            ("filename", "fileName"),
            ("size", "size"),
            ("path", "local_path"),
            ("localPath", "local_path"),
            ("localFilename", "local_filename"),
            ("filenameOnDisk", "local_filename"),
        ):
            value = item.get(source)
            if value not in (None, "") and target not in meta:
                meta[target] = value
        attachments.append(meta)
    return attachments


def queue_dir_for_spool(spool: Path) -> Path:
    return spool.expanduser().with_suffix(spool.expanduser().suffix + ".d")


def write_spool_record(spool: Path, record: dict[str, Any]) -> Path:
    queue = queue_dir_for_spool(spool)
    queue.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(queue, 0o700)
    except OSError:
        pass

    filename_hint = sanitize_filename(str(record.get("source_message_id") or "signal"))
    final = queue / f"{time.time_ns()}-{os.getpid()}-{filename_hint}-{uuid.uuid4().hex}.jsonl"
    tmp = queue / f".{final.name}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
        fsync_dir(queue)
        return final
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return (cleaned or "signal")[:80]


def health_check(http_url: str, timeout: float = 10.0) -> bool:
    url = urljoin(http_url.rstrip("/") + "/", "api/v1/check")
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def listen_once(http_url: str, account: str, spool: Path, shutdown: Shutdown, once: bool = False) -> int:
    url = f"{http_url.rstrip('/')}/api/v1/events?account={quote(account, safe='')}"
    req = Request(url, headers={"Accept": "text/event-stream"}, method="GET")
    parser = SSEParser()
    with urlopen(req, timeout=30.0) as response:
        while not shutdown.requested:
            chunk = response.read(4096)
            if not chunk:
                break
            for payload in parser.feed(chunk):
                handle_sse_payload(payload, account, spool)
        for payload in parser.close():
            handle_sse_payload(payload, account, spool)
    return 0 if once or shutdown.requested else 0


def handle_sse_payload(payload: str, account: str, spool: Path) -> bool:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.warning("ignored malformed SSE JSON payload")
        return False
    if not isinstance(data, dict):
        LOGGER.warning("ignored non-object SSE JSON payload")
        return False
    try:
        record = normalize_signal_event(data, account)
    except Exception as exc:
        LOGGER.warning("ignored Signal event after normalization error type=%s keys=%s", type(exc).__name__, sorted(str(key) for key in data.keys()))
        return False
    if record is None:
        return False
    written = write_spool_record(spool, record)
    LOGGER.info(
        "queued signal event direction=%s conversation=%s file=%s",
        record.get("direction"),
        record.get("conversation_type"),
        written.name,
    )
    return True


def run_forever(http_url: str, account: str, spool: Path, shutdown: Shutdown, once: bool = False) -> int:
    LOGGER.info("starting passive Signal collector for %s", redact_account(account))
    backoff = INITIAL_BACKOFF_SECONDS
    while not shutdown.requested:
        try:
            listen_once(http_url, account, spool, shutdown, once=once)
            if once:
                return 0
            backoff = INITIAL_BACKOFF_SECONDS
        except SSEEventTooLarge as exc:
            LOGGER.error("%s; reconnecting", exc)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            LOGGER.warning("Signal SSE connection failed: %s", exc)
        except json.JSONDecodeError:
            LOGGER.warning("Signal SSE JSON decode error")

        if once:
            return 1
        sleep_for = min(backoff, MAX_BACKOFF_SECONDS) + random.uniform(0, min(backoff, MAX_BACKOFF_SECONDS) * 0.2)
        end = time.monotonic() + sleep_for
        while not shutdown.requested and time.monotonic() < end:
            time.sleep(min(0.25, end - time.monotonic()))
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Passive signal-cli SSE collector for Hermes Context Inbox.")
    parser.add_argument("--http-url", default=os.environ.get("SIGNAL_HTTP_URL", DEFAULT_HTTP_URL))
    parser.add_argument("--account", default=os.environ.get("SIGNAL_ACCOUNT"))
    parser.add_argument("--spool", default=os.environ.get("HERMES_CONTEXT_INBOX_SPOOL", DEFAULT_SPOOL))
    parser.add_argument("--once", action="store_true", help="connect once and exit after EOF")
    parser.add_argument("--health-check", action="store_true", help="check daemon health and exit")
    parser.add_argument("--log-level", default=os.environ.get("SIGNAL_CONTEXT_COLLECTOR_LOG_LEVEL", "INFO"))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO), format="%(levelname)s %(message)s")

    if not args.account:
        parser.error("--account or SIGNAL_ACCOUNT is required")

    if args.health_check:
        return 0 if health_check(args.http_url) else 1

    shutdown = Shutdown()
    signal.signal(signal.SIGTERM, shutdown.request)
    signal.signal(signal.SIGINT, shutdown.request)
    return run_forever(args.http_url, args.account, Path(args.spool), shutdown, once=bool(args.once))


if __name__ == "__main__":
    raise SystemExit(main())
