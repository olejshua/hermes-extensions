from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..models import Derivative, RawItem, ScanBatch, ScanRequest, Sensitivity, SourceCursor, SourceHealth, SourceStatus
from ..redaction import REDACTION_VERSION, redact_text, stable_hash

Executor = Callable[[list[str], float], subprocess.CompletedProcess[str]]
DEFAULT_CAL = "~/.hermes/skills/productivity/calendar/bin/cal"


def _execute(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, check=False, timeout=timeout)


class CalendarAdapter:
    """Read-only Apple Calendar adapter; only ``cal --json list`` is permitted."""
    source_name = "calendar"
    sensitivity = Sensitivity.SENSITIVE
    retention = (30, 90)
    READ_COMMAND = ("--json", "list")

    def __init__(self, binary: str = DEFAULT_CAL, *, executor: Executor = _execute):
        self.binary, self.executor = str(Path(binary).expanduser()), executor

    def health(self, request: ScanRequest) -> SourceHealth:
        if self.executor is _execute and not shutil.which(self.binary):
            return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, "calendar_unavailable")
        return SourceHealth(self.source_name, SourceStatus.HEALTHY, "ok", "calendar-json-v1")

    def scan(self, request: ScanRequest, cursor: SourceCursor | None) -> ScanBatch:
        now = (request.now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).date()
        past = max(0, min(int(request.options.get("calendar_past_days", 30)), 365))
        future = max(0, min(int(request.options.get("calendar_upcoming_days", 90)), 365))
        argv = [self.binary, *self.READ_COMMAND, (now - dt.timedelta(days=past)).isoformat(), (now + dt.timedelta(days=future)).isoformat()]
        completed = self.executor(argv, float(request.options.get("timeout", 15)))
        if completed.returncode:
            raise PermissionError("calendar_read_denied")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("calendar_invalid_json") from exc
        records = value.get("events", []) if isinstance(value, dict) else value
        if not isinstance(records, list):
            raise ValueError("calendar_invalid_schema")
        signature = hashlib.sha256(completed.stdout.encode()).hexdigest()
        continuing = bool(cursor and cursor.value.get("reconciling") and cursor.value.get("signature") == signature)
        if cursor and not request.full and not continuing and cursor.value.get("signature") == signature:
            return ScanBatch(cursor=cursor, schema_fingerprint="calendar-json-v1", reason_code="unchanged")
        offset = int(cursor.value.get("record_index", 0)) if continuing and cursor else 0
        page = records[offset: offset + request.limit]
        more = len(records) > offset + len(page)
        observed = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        raw, derivatives, skipped = [], [], 0
        for record in page:
            if not isinstance(record, dict):
                skipped += 1; continue
            event_id = str(record.get("id") or record.get("identifier") or record.get("event_id") or "")
            summary = redact_text(str(record.get("title") or record.get("summary") or ""), limit=300)
            if not event_id or not summary:
                skipped += 1; continue
            calendar = str(record.get("calendar_id") or record.get("calendar") or "")
            location = str(record.get("location_id") or record.get("location") or "")
            payload = {"summary": summary, "start": str(record.get("start") or record.get("start_date") or ""), "end": str(record.get("end") or record.get("end_date") or ""), "all_day": bool(record.get("all_day") or record.get("is_all_day")), "provider": redact_text(str(record.get("provider") or record.get("source") or "apple_calendar"), limit=80), "calendar_hash": stable_hash(calendar, namespace="calendar-id") if calendar else "", "location_hash": stable_hash(location, namespace="calendar-location") if location else ""}
            raw.append(RawItem(event_id, payload, observed))
            derivatives.append(Derivative(event_id, {"platform": "calendar", "conversation_id": payload["calendar_hash"] or "calendar", "conversation_name": "Calendar", "conversation_type": "calendar", "body": summary, "message_ts": payload["start"], "source_message_id": "cal_" + stable_hash(event_id, namespace="calendar-event"), "direction": "local", "raw": {"source": "calendar", "redaction_version": REDACTION_VERSION}}, export_allowed=False))
        return ScanBatch(tuple(raw), tuple(derivatives), SourceCursor({"signature": signature, "record_index": offset + len(page) if more else 0, "reconciling": more}), "calendar-json-v1", skipped, "partial" if more else "ok", full_reconciliation=True, scan_complete=not more)
