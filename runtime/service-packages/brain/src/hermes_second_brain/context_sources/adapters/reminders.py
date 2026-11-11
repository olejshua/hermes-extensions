from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from ..models import Derivative, RawItem, ScanBatch, ScanRequest, Sensitivity, SourceCursor, SourceHealth, SourceStatus
from ..redaction import REDACTION_VERSION, redact_text, stable_hash

Executor = Callable[[list[str], float], subprocess.CompletedProcess[str]]


def _execute(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, check=False, timeout=timeout)


class RemindersAdapter:
    source_name = "reminders"
    sensitivity = Sensitivity.PERSONAL
    retention = (30, 90)
    # `show all` is a read-only query.  `--no-input` prevents a scheduled
    # collector from ever falling into an interactive prompt.
    READ_COMMAND = ("show", "all", "--json", "--no-input")
    FORBIDDEN = frozenset({"add", "edit", "complete", "delete", "remove", "create", "update"})

    def __init__(self, binary: str = "remindctl", *, executor: Executor = _execute):
        self.binary = binary
        self.executor = executor

    def health(self, request: ScanRequest) -> SourceHealth:
        if not shutil.which(self.binary) and self.executor is _execute:
            return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, "remindctl_unavailable")
        return SourceHealth(self.source_name, SourceStatus.HEALTHY, "ok", "remindctl-json-v1")

    def scan(self, request: ScanRequest, cursor: SourceCursor | None) -> ScanBatch:
        argv = [self.binary, *self.READ_COMMAND]
        if any(part.lower() in self.FORBIDDEN for part in argv[1:]):
            raise ValueError("write_command_rejected")
        completed = self.executor(argv, float(request.options.get("timeout", 15)))
        if completed.returncode != 0:
            raise PermissionError("reminders_read_denied")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("reminders_invalid_json") from exc
        records = value.get("reminders", []) if isinstance(value, dict) else value
        if not isinstance(records, list):
            raise ValueError("reminders_invalid_schema")
        signature = hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest()
        continuing = bool(cursor and cursor.value.get("reconciling") and cursor.value.get("signature") == signature)
        if cursor and not request.full and not continuing and cursor.value.get("signature") == signature:
            return ScanBatch(cursor=cursor, schema_fingerprint="remindctl-json-v1", reason_code="unchanged")
        offset = int(cursor.value.get("record_index", 0)) if continuing and cursor else 0
        page = records[offset : offset + request.limit]
        has_more = len(records) > offset + request.limit
        raw: list[RawItem] = []
        derivatives: list[Derivative] = []
        skipped = 0
        now = request.now or dt.datetime.now(dt.timezone.utc)
        observed = now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        for record in page:
            if not isinstance(record, dict):
                skipped += 1
                continue
            external_id = str(record.get("id") or record.get("identifier") or "")
            title = redact_text(str(record.get("title") or record.get("name") or ""), limit=500)
            modified = str(record.get("modified_at") or record.get("updated_at") or record.get("due_date") or "")
            if not external_id or not title:
                skipped += 1
                continue
            completed_flag = bool(record.get("completed") or record.get("is_completed"))
            payload = {
                "title": title,
                "due_at": str(record.get("due_date") or record.get("due_at") or ""),
                "completed": completed_flag,
                "priority": str(record.get("priority") or ""),
                "list_hash": stable_hash(str(record.get("list") or record.get("list_id") or ""), namespace="reminder-list") if record.get("list") or record.get("list_id") else "",
            }
            raw.append(RawItem(external_id, payload, observed))
            derivatives.append(
                Derivative(
                    external_id,
                    {
                        "platform": "reminders",
                        "conversation_id": payload["list_hash"],
                        "conversation_name": "Reminders",
                        "conversation_type": "task_list",
                        "body": title,
                        "message_ts": payload["due_at"] or modified or observed,
                        "source_message_id": "rem_" + stable_hash(external_id, namespace="reminder-id"),
                        "direction": "local",
                        "flags": ["completed"] if completed_flag else ["open"],
                        "raw": {"source": "reminders", "redaction_version": REDACTION_VERSION},
                    },
                    export_allowed=not completed_flag,
                )
            )
        return ScanBatch(
            tuple(raw),
            tuple(derivatives),
            SourceCursor({"signature": signature, "record_index": offset + len(page) if has_more else 0, "reconciling": has_more}),
            "remindctl-json-v1",
            skipped,
            "partial" if has_more else "ok",
            full_reconciliation=True,
            scan_complete=not has_more,
        )
