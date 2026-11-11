from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

from ..context_inbox import DEFAULT_CONTEXT_DB, ContextInbox
from .models import ScanRequest, ScanResult, SourceHealth, SourceStatus
from .redaction import sanitize_error
from .registry import SourceRegistry
from .store import DEFAULT_SOURCE_DB, CursorConflictError, SourceStore


class SourceRunner:
    def __init__(
        self,
        registry: SourceRegistry,
        *,
        source_db: Path | str = DEFAULT_SOURCE_DB,
        inbox_db: Path | str = DEFAULT_CONTEXT_DB,
        store: SourceStore | None = None,
    ):
        self.registry = registry
        self.source_db = Path(source_db).expanduser()
        self.inbox_db = Path(inbox_db).expanduser()
        self._store = store

    @property
    def store(self) -> SourceStore:
        if self._store is None:
            self._store = SourceStore(self.source_db)
        return self._store

    def scan(self, names: Iterable[str], *, full: bool = False, dry_run: bool = False, limit: int = 1000, options: dict | None = None) -> list[ScanResult]:
        results: list[ScanResult] = []
        for name in names:
            results.append(self.scan_one(name, full=full, dry_run=dry_run, limit=limit, options=options))
        return results

    def scan_one(self, name: str, *, full: bool = False, dry_run: bool = False, limit: int = 1000, options: dict | None = None) -> ScanResult:
        adapter = self.registry.get(name)
        source = adapter.source_name
        request = ScanRequest(source=source, full=full, dry_run=dry_run, limit=max(1, min(limit, 100_000)), options=options or {})
        started = time.time()
        retention = getattr(adapter, "retention", (30, 90))
        try:
            health = adapter.health(request)
        except PermissionError:
            health = None
            status, reason = SourceStatus.PENDING_PERMISSION, "permission_required"
        except Exception as exc:
            health = None
            status, reason = SourceStatus.ERROR, sanitize_error(exc)
        if health is None:
            if not dry_run:
                self.store.record_failure(source, adapter.sensitivity.value, status, reason, started_at=started)
            return ScanResult(source, status, reason, dry_run=dry_run)
        if health.status is not SourceStatus.HEALTHY:
            if not dry_run:
                self.store.record_health(health, adapter.sensitivity.value, retention=retention)
            return ScanResult(source, health.status, health.reason_code, dry_run=dry_run)
        try:
            cursor = None if dry_run else self.store.cursor(source)
            batch = adapter.scan(request, cursor)
            fetched = len(batch.raw_items)
            # WhatsApp deliberately retains the existing importer and snapshot
            # semantics. Run it before advancing its file signature cursor.
            imported = 0
            if hasattr(adapter, "import_into") and (fetched or full):
                summary = adapter.import_into(self.inbox_db, dry_run=dry_run)  # type: ignore[attr-defined]
                if summary.status not in {"ok", "blocked_decryption_needed"}:
                    raise RuntimeError("wrapped_import_failed")
                imported = summary.imported
            if dry_run:
                return ScanResult(source, SourceStatus.HEALTHY, batch.reason_code, fetched, fetched, len(batch.derivatives) + imported, batch.skipped, True)
            try:
                counts = self.store.commit_batch(
                    source,
                    adapter.sensitivity.value,
                    batch,
                    started_at=started,
                    retention=retention,
                    expected_cursor=cursor,
                )
            except CursorConflictError:
                # A concurrent run won. Re-read once instead of allowing this
                # older batch to regress the persisted high-water cursor.
                cursor = self.store.cursor(source)
                batch = adapter.scan(request, cursor)
                fetched = len(batch.raw_items)
                counts = self.store.commit_batch(
                    source,
                    adapter.sensitivity.value,
                    batch,
                    started_at=started,
                    retention=retention,
                    expected_cursor=cursor,
                )
            published = imported
            pending = self.store.pending_changes(source)
            if pending:
                inbox = ContextInbox(self.inbox_db)
                inbox.apply_derivative_changes((event, tombstone, generation) for _, event, tombstone, generation in pending)
                self.store.mark_published(identifier for identifier, _, _, _ in pending)
                published += len(pending)
            return ScanResult(source, SourceStatus.HEALTHY, batch.reason_code, fetched, counts["stored"], published, batch.skipped, False)
        except PermissionError:
            status, reason = SourceStatus.PENDING_PERMISSION, "permission_required"
        except Exception as exc:
            status, reason = SourceStatus.ERROR, sanitize_error(exc)
        if not dry_run:
            self.store.record_failure(source, adapter.sensitivity.value, status, reason, started_at=started)
        return ScanResult(source, status, reason, dry_run=dry_run)

    def probe(self, names: Iterable[str], *, persist: bool = False) -> list[dict]:
        result = []
        request = ScanRequest(dry_run=not persist)
        for name in names:
            adapter = self.registry.get(name)
            try:
                health = adapter.health(request)
            except PermissionError:
                health = SourceHealth(adapter.source_name, SourceStatus.PENDING_PERMISSION, "permission_required")
            except Exception as exc:
                health = SourceHealth(adapter.source_name, SourceStatus.ERROR, sanitize_error(exc))
            if persist:
                self.store.record_health(health, adapter.sensitivity.value, retention=getattr(adapter, "retention", (30, 90)))
            result.append(health.as_dict())
        return result
