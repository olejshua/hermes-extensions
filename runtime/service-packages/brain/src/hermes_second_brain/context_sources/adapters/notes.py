from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from collections.abc import Callable

from ..models import Derivative, RawItem, ScanBatch, ScanRequest, Sensitivity, SourceCursor, SourceHealth, SourceStatus
from ..redaction import REDACTION_VERSION, redact_text, stable_hash

Executor = Callable[[list[str], float], subprocess.CompletedProcess[str]]
_LINE = re.compile(r"^\s*(\d+)\.\s*(.*?)\s+-\s+(.*)$")
_SECRET = re.compile(r"(?i)(?:\b(?:api[ _-]?key|token|secret|password|credential|jwt)\b|\bsk-[a-z0-9_-]{12,}|\beyJ[a-zA-Z0-9_-]{12,})")


def _execute(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, check=False, timeout=timeout)


class NotesMetadataAdapter:
    source_name = "notes"
    sensitivity = Sensitivity.RESTRICTED
    retention = (30, 90)

    # The memo CLI's documented non-mutating list operation is
    # `memo notes --no-cache`.  Do not substitute other subcommands: some are
    # interactive or mutate the Notes store.
    READ_COMMAND = ("notes", "--no-cache")

    def __init__(self, binary: str = "memo", *, executor: Executor = _execute): self.binary, self.executor = binary, executor
    def health(self, request: ScanRequest) -> SourceHealth:
        if self.executor is _execute and not shutil.which(self.binary): return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, "notes_unavailable")
        return SourceHealth(self.source_name, SourceStatus.HEALTHY, "ok", "memo-list-v1")
    def scan(self, request: ScanRequest, cursor: SourceCursor | None) -> ScanBatch:
        argv = [self.binary, *self.READ_COMMAND]
        completed = self.executor(argv, float(request.options.get("timeout", 15)))
        if completed.returncode: raise PermissionError("notes_read_denied")
        signature = hashlib.sha256(completed.stdout.encode()).hexdigest()
        continuing = bool(cursor and cursor.value.get("reconciling") and cursor.value.get("signature") == signature)
        if cursor and not request.full and not continuing and cursor.value.get("signature") == signature: return ScanBatch(cursor=cursor, schema_fingerprint="memo-list-v1", reason_code="unchanged")
        notes = []
        for line in completed.stdout.splitlines():
            match = _LINE.match(line)
            if match and not _SECRET.search(match.group(3)): notes.append((match.group(1), redact_text(match.group(2), limit=120), redact_text(match.group(3), limit=300)))
        offset = int(cursor.value.get("record_index", 0)) if continuing and cursor else 0; page = notes[offset:offset + request.limit]; more = len(notes) > offset + len(page)
        raw, derivatives = [], []
        for number, folder, title in page:
            external_id = "note:" + stable_hash(number + "\0" + folder + "\0" + title, namespace="notes-id")
            payload = {"title": title, "title_hash": stable_hash(title, namespace="notes-title"), "folder_hash": stable_hash(folder, namespace="notes-folder")}
            raw.append(RawItem(external_id, payload, ""))
            derivatives.append(Derivative(external_id, {"platform": "notes", "conversation_id": payload["folder_hash"], "conversation_name": "Notes", "conversation_type": "note_metadata", "body": "Apple Note metadata", "message_ts": "", "source_message_id": external_id, "direction": "local", "raw": {"source": "notes", "redaction_version": REDACTION_VERSION}}, export_allowed=False))
        return ScanBatch(tuple(raw), tuple(derivatives), SourceCursor({"signature": signature, "record_index": offset + len(page) if more else 0, "reconciling": more}), "memo-list-v1", 0, "partial" if more else "ok", full_reconciliation=True, scan_complete=not more)
