from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from ..models import Derivative, RawItem, ScanBatch, ScanRequest, Sensitivity, SourceCursor, SourceHealth, SourceStatus
from ..redaction import REDACTION_VERSION, redact_text, stable_hash
from ..sqlite_snapshot import sqlite_snapshot, validate_regular_source

CORE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)


class MessagesAdapter:
    source_name = "messages"
    sensitivity = Sensitivity.RESTRICTED
    retention = (30, 90)

    def __init__(self, chat_db_path: Path | str | None = None):
        self.chat_db_path = Path(chat_db_path or Path.home() / "Library/Messages/chat.db").expanduser()

    def health(self, request: ScanRequest) -> SourceHealth:
        try:
            self._validate()
        except ValueError:
            return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, "messages_permission_or_data_unavailable")
        return SourceHealth(self.source_name, SourceStatus.HEALTHY, "ok", "messages-sqlite-v1")

    def _validate(self) -> None:
        validate_regular_source(self.chat_db_path, max_bytes=2 * 1024 * 1024 * 1024)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.chat_db_path) + suffix)
            if sidecar.exists():
                validate_regular_source(sidecar, max_bytes=2 * 1024 * 1024 * 1024)

    def scan(self, request: ScanRequest, cursor: SourceCursor | None) -> ScanBatch:
        self._validate()
        observed = request.now or dt.datetime.now(dt.timezone.utc)
        observed = observed.astimezone(dt.timezone.utc)
        now_ts = observed.timestamp()
        continuing = bool(cursor and cursor.value.get("reconciling"))
        previous_reconciled = float(cursor.value.get("reconciled_at", 0) or 0) if cursor else 0
        reconciliation_due = cursor is None or (not continuing and now_ts - previous_reconciled >= 7 * 86400)
        full = bool(request.full or continuing or reconciliation_due)
        restart = bool((request.full or reconciliation_due) and not continuing)
        prior = 0 if restart else int(cursor.value.get("rowid", 0) if cursor else 0)
        with sqlite_snapshot(self.chat_db_path) as snapshot:
            conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True); conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "message" not in tables:
                conn.close(); raise ValueError("messages_schema_unsupported")
            columns = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
            if not {"guid", "text"}.issubset(columns):
                conn.close(); raise ValueError("messages_schema_unsupported")
            def col(name: str, default: str = "''") -> str: return "m." + name if name in columns else default
            join = ""
            chat_expr = "''"
            if {"chat_message_join", "chat"}.issubset(tables):
                cmj_columns = {r[1] for r in conn.execute("PRAGMA table_info(chat_message_join)")}
                chat_cols = {r[1] for r in conn.execute("PRAGMA table_info(chat)")}
                if {"chat_id", "message_id"}.issubset(cmj_columns) and ({"guid", "chat_identifier"} & chat_cols):
                    join = " LEFT JOIN chat_message_join cmj ON cmj.message_id=m.ROWID LEFT JOIN chat c ON c.ROWID=cmj.chat_id"
                    chat_expr = "COALESCE(c.guid,c.chat_identifier,'')" if {"guid", "chat_identifier"}.issubset(chat_cols) else ("c.guid" if "guid" in chat_cols else "c.chat_identifier")
            handle_join = ""
            handle_expr = "''"
            if "handle" in tables and "handle_id" in columns:
                handle_cols = {r[1] for r in conn.execute("PRAGMA table_info(handle)")}
                if "id" in handle_cols:
                    handle_join = " LEFT JOIN handle h ON h.ROWID=m.handle_id"
                    handle_expr = "COALESCE(h.id,'')"
            sql = f"SELECT m.ROWID rowid,m.guid guid,m.text text,{col('date','0')} date,{col('service')} service,{col('is_from_me','0')} outgoing,{col('is_system_message','0')} system,{col('is_empty','0')} empty,{chat_expr} chat_identity,{handle_expr} handle_identity FROM message m{join}{handle_join} WHERE m.ROWID>? ORDER BY m.ROWID LIMIT ?"
            rows = conn.execute(sql, (prior, request.limit + 1)).fetchall(); conn.close()
        more = len(rows) > request.limit; rows = rows[:request.limit]
        raw, derivatives, skipped, highwater = [], [], 0, prior
        for row in rows:
            rowid = int(row["rowid"]); highwater = max(highwater, rowid)
            guid = str(row["guid"] or "")
            text = redact_text(str(row["text"] or ""), limit=1000)
            if not guid or not text or bool(row["system"]) or bool(row["empty"]):
                skipped += 1; continue
            conversation = str(row["chat_identity"] or row["handle_identity"] or guid)
            when = _message_time(row["date"])
            payload = {"text": text, "date": when, "service": redact_text(str(row["service"] or ""), limit=40), "is_from_me": bool(row["outgoing"]), "conversation_hash": stable_hash(conversation, namespace="messages-conversation"), "handle_hash": stable_hash(str(row["handle_identity"]), namespace="messages-handle") if row["handle_identity"] else ""}
            external_id = "message:" + guid
            raw.append(RawItem(external_id, payload, when))
            derivatives.append(Derivative(external_id, {"platform": "messages", "conversation_id": payload["conversation_hash"], "conversation_name": "Messages", "conversation_type": "message", "body": text, "message_ts": when, "source_message_id": "msg_" + stable_hash(guid, namespace="messages-guid"), "direction": "outbound" if payload["is_from_me"] else "inbound", "raw": {"source": "messages", "redaction_version": REDACTION_VERSION}}, export_allowed=False))
        next_reconciled = now_ts if full and not more else previous_reconciled
        return ScanBatch(tuple(raw), tuple(derivatives), SourceCursor({"rowid": highwater, "date": rows[-1]["date"] if rows else "", "reconciling": bool(full and more), "reconciled_at": next_reconciled}), "messages-sqlite-v1", skipped, "partial" if more else "ok", full_reconciliation=full, scan_complete=not more)


def _message_time(value: object) -> str:
    try:
        number = float(value); seconds = number / 1_000_000_000 if abs(number) > 10_000_000_000 else number
        return (CORE_EPOCH + dt.timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return ""
