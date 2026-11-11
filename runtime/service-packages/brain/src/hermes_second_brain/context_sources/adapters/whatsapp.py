from __future__ import annotations

import hashlib
from pathlib import Path

from ...context_inbox import ImportSummary, import_whatsapp_chatstorage
from ..models import RawItem, ScanBatch, ScanRequest, Sensitivity, SourceCursor, SourceHealth, SourceStatus
from ..redaction import stable_hash
from ..sqlite_snapshot import validate_regular_source


class WhatsAppAdapter:
    """Thin observation-only wrapper around the existing hardened importer."""

    source_name = "whatsapp"
    sensitivity = Sensitivity.RESTRICTED
    retention = (30, 365)

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite").expanduser()

    def health(self, request: ScanRequest) -> SourceHealth:
        try:
            validate_regular_source(self.path)
        except ValueError:
            return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, "whatsapp_permission_or_data_unavailable")
        return SourceHealth(self.source_name, SourceStatus.HEALTHY, "ok", "existing-whatsapp-importer-v1")

    def scan(self, request: ScanRequest, cursor: SourceCursor | None) -> ScanBatch:
        path = validate_regular_source(self.path)
        signature = _sqlite_change_signature(path)
        previous = str(cursor.value.get("signature") or "") if cursor and not request.full else ""
        if signature == previous:
            return ScanBatch(cursor=SourceCursor({"signature": signature}), schema_fingerprint="existing-whatsapp-importer-v1", reason_code="unchanged")
        payload = {"file_signature_hash": stable_hash(signature, namespace="whatsapp-file")}
        return ScanBatch((RawItem(payload["file_signature_hash"], payload, ""),), (), SourceCursor({"signature": signature}), "existing-whatsapp-importer-v1", full_reconciliation=request.full)

    def import_into(self, inbox_path: Path, *, dry_run: bool = False) -> ImportSummary:
        return import_whatsapp_chatstorage(self.path, inbox_path, dry_run=dry_run)


def _sqlite_change_signature(path: Path) -> str:
    """Fingerprint main database and committed WAL bytes without opening either writable."""
    digest = hashlib.sha256()
    for label, candidate in ((b"main", path), (b"wal", Path(str(path) + "-wal"))):
        digest.update(label)
        if candidate.is_symlink():
            raise ValueError("whatsapp_sidecar_symlink_rejected")
        try:
            stat = candidate.stat()
        except FileNotFoundError:
            digest.update(b"missing")
            continue
        if not candidate.is_file():
            raise ValueError("whatsapp_sidecar_invalid")
        digest.update(f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}".encode("ascii"))
    return digest.hexdigest()
