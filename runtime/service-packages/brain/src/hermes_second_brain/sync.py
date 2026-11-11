from __future__ import annotations

import concurrent.futures
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from .manifest import Manifest
from .ov_adapter import OpenVikingAdapter
from .scanner import scan_source
from .state import ResourceRow, State

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncSummary:
    scanned: int
    enqueued: int
    deleted_detected: int
    synced: int
    failed: int


def sync_manifest(manifest: Manifest, *, dry_run: bool = False) -> SyncSummary:
    state = State(manifest.state_db)
    scanned = enqueued = deleted = 0
    active_source_root_ids = {source.id for source in manifest.sources}
    for source in manifest.sources:
        try:
            resources = scan_source(source)
        except (FileNotFoundError, InterruptedError):
            # Unavailable root is not a deletion event. Never revoke from an incomplete scan.
            raise
        changed, deleted_rows = state.upsert_scan(resources, source.id, dry_run=dry_run)
        scanned += len(resources)
        enqueued += len(changed)
        deleted += len(deleted_rows)
        LOG.info("source scanned", extra={"fields": {"source": source.id, "scanned": len(resources), "enqueued": len(changed), "deleted_detected": len(deleted_rows)}})
    if dry_run:
        return SyncSummary(scanned, enqueued, deleted, 0, 0)
    adapter = OpenVikingAdapter(
        binary=manifest.ov_binary,
        timeout_seconds=manifest.ov_timeout_seconds,
        attempts=manifest.retry_attempts,
        backoff_seconds=manifest.retry_backoff_seconds,
    )
    lease_seconds = openviking_lease_seconds(manifest)
    owner = f"sync-{uuid.uuid4().hex}"
    synced = failed = 0
    processed_source_ids: set[str] = set()
    while True:
        batch = state.claim_pending(
            limit=manifest.concurrency,
            lease_seconds=lease_seconds,
            owner=owner,
            include_failed=True,
            exclude_source_ids=processed_source_ids,
            allowed_source_root_ids=active_source_root_ids,
        )
        if not batch:
            break
        processed_source_ids.update(row.source_id for row in batch)
        completed: list[tuple[ResourceRow, str]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=manifest.concurrency) as pool:
            futures = {
                pool.submit(_sync_one, adapter, row, resume_locked=not manifest.wait_for_indexing): row
                for row in batch
            }
            for future in concurrent.futures.as_completed(futures):
                row = futures[future]
                try:
                    ov_id = future.result()
                except Exception as exc:
                    if state.mark_failed(row.source_id, row.sha256, str(exc), owner=owner):
                        failed += 1
                    LOG.error("resource sync failed", extra={"fields": {"source_id": row.source_id, "error": str(exc)}})
                else:
                    completed.append((row, ov_id))
        if not completed:
            continue
        if not manifest.wait_for_indexing:
            for row, ov_id in completed:
                if state.mark_synced(row.source_id, row.sha256, ov_id, owner=owner):
                    synced += 1
                else:
                    LOG.warning("stale resource sync result ignored", extra={"fields": {"source_id": row.source_id}})
            continue
        try:
            adapter.wait()
        except Exception as exc:
            for row, _ in completed:
                if state.mark_failed(row.source_id, row.sha256, str(exc), owner=owner):
                    failed += 1
            LOG.error("OpenViking batch wait failed", extra={"fields": {"error": str(exc), "resources": len(completed)}})
        else:
            for row, ov_id in completed:
                if state.mark_synced(row.source_id, row.sha256, ov_id, owner=owner):
                    synced += 1
                else:
                    LOG.warning("stale resource sync result ignored", extra={"fields": {"source_id": row.source_id}})
    return SyncSummary(scanned, enqueued, deleted, synced, failed)


def _sync_one(adapter: OpenVikingAdapter, row: ResourceRow, *, resume_locked: bool = False) -> str:
    result = adapter.sync_resource(
        path=Path(row.path),
        source_id=row.source_id,
        namespace=row.namespace,
        sha256=row.sha256,
        relative_path=row.relative_path,
        existing_uri=row.ov_resource_id,
        resume_locked=resume_locked,
    )
    return result.resource_id


def openviking_lease_seconds(manifest: Manifest) -> float:
    run_command = _retry_command_bound(manifest.ov_timeout_seconds, manifest.retry_attempts, manifest.retry_backoff_seconds)
    stat_command = manifest.ov_timeout_seconds
    single_resource_chain = (2 * stat_command) + (4 * run_command)
    global_wait = run_command
    return max(60.0, single_resource_chain + global_wait + 60.0)


def _retry_command_bound(timeout_seconds: float, attempts: int, backoff_seconds: float) -> float:
    bounded_attempts = max(1, attempts)
    retry_backoff = sum(backoff_seconds * attempt for attempt in range(1, bounded_attempts))
    return (timeout_seconds * bounded_attempts) + retry_backoff
