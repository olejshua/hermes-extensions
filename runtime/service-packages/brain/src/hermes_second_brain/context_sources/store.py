from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from .models import Derivative, RawItem, ScanBatch, SourceCursor, SourceHealth, SourceStatus
from .redaction import validate_bounded_json

DEFAULT_SOURCE_DB = Path("~/.hermes/second-brain/context-sources.sqlite3").expanduser()


def _json(value: Any) -> str:
    validate_bounded_json(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _bounded_json(value: Any, *, limit: int, label: str) -> str:
    encoded = _json(value)
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError(f"{label}_too_large")
    return encoded


def _identity(source: str, scope: str, external_id: str) -> str:
    return hashlib.sha256(f"{source}\0{scope}\0{external_id}".encode("utf-8")).hexdigest()


def _cursor_hash(cursor_json: str) -> str:
    return hashlib.sha256(cursor_json.encode("utf-8")).hexdigest()


def _private_parent(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("vault_parent_symlink_rejected")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("vault_parent_symlink_rejected")
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _reject_db_symlinks(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.is_symlink():
            raise ValueError("vault_symlink_rejected")


class CursorConflictError(RuntimeError):
    pass


_UNSET = object()


class SourceStore:
    def __init__(self, db_path: Path | str = DEFAULT_SOURCE_DB):
        self.db_path = Path(db_path).expanduser()
        _private_parent(self.db_path.parent)
        _reject_db_symlinks(self.db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        _reject_db_symlinks(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            os.chmod(self.db_path, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.db_path) + suffix)
                if sidecar.exists():
                    os.chmod(sidecar, 0o600)
        except OSError:
            pass
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS context_sources(
                  source TEXT PRIMARY KEY,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  sensitivity TEXT NOT NULL,
                  status TEXT NOT NULL,
                  reason_code TEXT NOT NULL,
                  schema_fingerprint TEXT NOT NULL DEFAULT '',
                  cursor_json TEXT NOT NULL DEFAULT '{}',
                  last_attempt_at REAL,
                  last_success_at REAL,
                  consecutive_failures INTEGER NOT NULL DEFAULT 0,
                  raw_retention_days INTEGER NOT NULL DEFAULT 30,
                  derivative_retention_days INTEGER NOT NULL DEFAULT 90,
                  reconciliation_generation INTEGER NOT NULL DEFAULT 0,
                  active_reconciliation_generation INTEGER NOT NULL DEFAULT 0,
                  updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_source_runs(
                  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source TEXT NOT NULL,
                  started_at REAL NOT NULL,
                  finished_at REAL NOT NULL,
                  status TEXT NOT NULL,
                  reason_code TEXT NOT NULL,
                  fetched_count INTEGER NOT NULL,
                  stored_count INTEGER NOT NULL,
                  derivative_count INTEGER NOT NULL,
                  skipped_count INTEGER NOT NULL,
                  cursor_before_hash TEXT NOT NULL,
                  cursor_after_hash TEXT NOT NULL,
                  dry_run INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS context_raw_items(
                  item_id TEXT PRIMARY KEY,
                  source TEXT NOT NULL,
                  account_scope TEXT NOT NULL,
                  external_id_hash TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  observed_at TEXT NOT NULL,
                  first_seen_at REAL NOT NULL,
                  last_seen_at REAL NOT NULL,
                  expires_at REAL,
                  UNIQUE(source,account_scope,external_id_hash)
                );
                CREATE TABLE IF NOT EXISTS context_derivatives(
                  derivative_id TEXT PRIMARY KEY,
                  source TEXT NOT NULL,
                  raw_item_id TEXT,
                  account_scope TEXT NOT NULL,
                  external_id_hash TEXT NOT NULL,
                  event_json TEXT NOT NULL,
                  redaction_version TEXT NOT NULL,
                  export_allowed INTEGER NOT NULL,
                  published_at REAL,
                  first_seen_at REAL NOT NULL,
                  last_seen_at REAL NOT NULL,
                  expires_at REAL,
                  reconciliation_generation INTEGER NOT NULL DEFAULT 0,
                  tombstoned_at REAL,
                  UNIQUE(source,account_scope,external_id_hash),
                  FOREIGN KEY(raw_item_id) REFERENCES context_raw_items(item_id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_context_raw_expiry ON context_raw_items(expires_at);
                CREATE INDEX IF NOT EXISTS idx_context_derivative_expiry ON context_derivatives(expires_at);
                CREATE INDEX IF NOT EXISTS idx_context_derivative_publish ON context_derivatives(source,published_at);
                """
            )
            self._migrate_reconciliation_columns(conn)

    @staticmethod
    def _migrate_reconciliation_columns(conn: sqlite3.Connection) -> None:
        source_columns = {row[1] for row in conn.execute("PRAGMA table_info(context_sources)")}
        if "reconciliation_generation" not in source_columns:
            conn.execute("ALTER TABLE context_sources ADD COLUMN reconciliation_generation INTEGER NOT NULL DEFAULT 0")
        if "active_reconciliation_generation" not in source_columns:
            conn.execute("ALTER TABLE context_sources ADD COLUMN active_reconciliation_generation INTEGER NOT NULL DEFAULT 0")
        derivative_columns = {row[1] for row in conn.execute("PRAGMA table_info(context_derivatives)")}
        if "reconciliation_generation" not in derivative_columns:
            conn.execute("ALTER TABLE context_derivatives ADD COLUMN reconciliation_generation INTEGER NOT NULL DEFAULT 0")
        if "tombstoned_at" not in derivative_columns:
            conn.execute("ALTER TABLE context_derivatives ADD COLUMN tombstoned_at REAL")

    def cursor(self, source: str) -> SourceCursor | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT cursor_json,reconciliation_generation,active_reconciliation_generation FROM context_sources WHERE source=?",
                (source,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["cursor_json"])
        except json.JSONDecodeError:
            return None
        return SourceCursor(value) if isinstance(value, dict) else None

    def record_health(self, health: SourceHealth, sensitivity: str, *, retention: tuple[int, int] = (30, 90)) -> None:
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO context_sources(source,sensitivity,status,reason_code,schema_fingerprint,raw_retention_days,derivative_retention_days,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(source) DO UPDATE SET sensitivity=excluded.sensitivity,status=excluded.status,
                     reason_code=excluded.reason_code,schema_fingerprint=excluded.schema_fingerprint,
                     raw_retention_days=excluded.raw_retention_days,derivative_retention_days=excluded.derivative_retention_days,updated_at=excluded.updated_at""",
                (health.source, sensitivity, health.status.value, health.reason_code, health.schema_fingerprint, retention[0], retention[1], now),
            )

    def commit_batch(
        self,
        source: str,
        sensitivity: str,
        batch: ScanBatch,
        *,
        started_at: float,
        retention: tuple[int, int],
        expected_cursor: SourceCursor | None | object = _UNSET,
    ) -> dict[str, int]:
        now = time.time()
        raw_days, derivative_days = retention
        stored = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT cursor_json,reconciliation_generation,active_reconciliation_generation FROM context_sources WHERE source=?",
                (source,),
            ).fetchone()
            before_json = str(row["cursor_json"]) if row is not None else "{}"
            try:
                before_value = json.loads(before_json)
            except json.JSONDecodeError:
                before_value = {}
                before_json = "{}"
            if not isinstance(before_value, dict):
                before_value = {}
                before_json = "{}"
            if expected_cursor is not _UNSET:
                expected_json = _json(expected_cursor.value if isinstance(expected_cursor, SourceCursor) else {})
                if _json(before_value) != expected_json:
                    raise CursorConflictError("source_cursor_changed")
            after_json = _bounded_json(batch.cursor.value if batch.cursor else before_value, limit=64 * 1024, label="cursor")
            current_generation = int(row["reconciliation_generation"] or 0) if row is not None else 0
            active_generation = int(row["active_reconciliation_generation"] or 0) if row is not None else 0
            item_generation = current_generation
            if batch.full_reconciliation:
                item_generation = active_generation or current_generation + 1
                active_generation = 0 if batch.scan_complete else item_generation
                if batch.scan_complete:
                    current_generation = item_generation
            for item in batch.raw_items:
                if not item.external_id or len(item.external_id) > 1024 or len(item.account_scope) > 512 or not isinstance(item.observed_at, str) or len(item.observed_at) > 128:
                    raise ValueError("invalid_raw_identity")
                identity = _identity(source, item.account_scope, item.external_id)
                item_id = "raw_" + identity
                expires = item.expires_at if item.expires_at is not None else now + raw_days * 86400
                if not isinstance(expires, (int, float)) or isinstance(expires, bool) or not math.isfinite(float(expires)):
                    raise ValueError("invalid_raw_expiry")
                cur = conn.execute(
                    """INSERT INTO context_raw_items(item_id,source,account_scope,external_id_hash,payload_json,observed_at,first_seen_at,last_seen_at,expires_at)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(item_id) DO UPDATE SET payload_json=excluded.payload_json,observed_at=excluded.observed_at,last_seen_at=excluded.last_seen_at,expires_at=excluded.expires_at
                       WHERE context_raw_items.payload_json IS NOT excluded.payload_json OR context_raw_items.observed_at IS NOT excluded.observed_at""",
                    (item_id, source, item.account_scope, identity, _bounded_json(item.payload, limit=256 * 1024, label="raw_payload"), item.observed_at, now, now, expires),
                )
                stored += max(0, cur.rowcount)
            raw_identities = {_identity(source, item.account_scope, item.external_id) for item in batch.raw_items}
            for item in batch.derivatives:
                if not item.external_id or len(item.external_id) > 1024 or len(item.account_scope) > 512 or not item.redaction_version or len(item.redaction_version) > 64:
                    raise ValueError("invalid_derivative_identity")
                identity = _identity(source, item.account_scope, item.external_id)
                raw_id = "raw_" + identity if identity in raw_identities else None
                derivative_id = "der_" + identity
                expires = item.expires_at if item.expires_at is not None else now + derivative_days * 86400
                if not isinstance(expires, (int, float)) or isinstance(expires, bool) or not math.isfinite(float(expires)):
                    raise ValueError("invalid_derivative_expiry")
                conn.execute(
                    """INSERT INTO context_derivatives(derivative_id,source,raw_item_id,account_scope,external_id_hash,event_json,redaction_version,export_allowed,published_at,first_seen_at,last_seen_at,expires_at,reconciliation_generation,tombstoned_at)
                       VALUES(?,?,?,?,?,?,?,?,NULL,?,?,?,?,NULL)
                       ON CONFLICT(derivative_id) DO UPDATE SET raw_item_id=excluded.raw_item_id,event_json=excluded.event_json,
                         redaction_version=excluded.redaction_version,export_allowed=excluded.export_allowed,
                         published_at=CASE WHEN context_derivatives.event_json IS NOT excluded.event_json
                           OR context_derivatives.redaction_version IS NOT excluded.redaction_version
                           OR context_derivatives.export_allowed IS NOT excluded.export_allowed
                           OR context_derivatives.tombstoned_at IS NOT NULL THEN NULL ELSE context_derivatives.published_at END,
                         last_seen_at=excluded.last_seen_at,expires_at=excluded.expires_at,
                         reconciliation_generation=excluded.reconciliation_generation,tombstoned_at=NULL
                       WHERE context_derivatives.event_json IS NOT excluded.event_json
                          OR context_derivatives.redaction_version IS NOT excluded.redaction_version
                          OR context_derivatives.export_allowed IS NOT excluded.export_allowed
                          OR context_derivatives.reconciliation_generation IS NOT excluded.reconciliation_generation
                          OR context_derivatives.tombstoned_at IS NOT NULL""",
                    (derivative_id, source, raw_id, item.account_scope, identity, _bounded_json(item.event, limit=64 * 1024, label="derivative"), item.redaction_version, int(item.export_allowed), now, now, expires, item_generation),
                )
            if batch.full_reconciliation and batch.scan_complete:
                conn.execute(
                    """UPDATE context_derivatives
                       SET tombstoned_at=?,published_at=NULL,reconciliation_generation=?
                       WHERE source=? AND tombstoned_at IS NULL AND reconciliation_generation<>?""",
                    (now, item_generation, source, item_generation),
                )
            conn.execute(
                """INSERT INTO context_sources(source,sensitivity,status,reason_code,schema_fingerprint,cursor_json,last_attempt_at,last_success_at,consecutive_failures,raw_retention_days,derivative_retention_days,reconciliation_generation,active_reconciliation_generation,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?,?)
                   ON CONFLICT(source) DO UPDATE SET sensitivity=excluded.sensitivity,status=excluded.status,reason_code=excluded.reason_code,
                     schema_fingerprint=excluded.schema_fingerprint,cursor_json=excluded.cursor_json,last_attempt_at=excluded.last_attempt_at,
                     last_success_at=excluded.last_success_at,consecutive_failures=0,raw_retention_days=excluded.raw_retention_days,
                     derivative_retention_days=excluded.derivative_retention_days,reconciliation_generation=excluded.reconciliation_generation,
                     active_reconciliation_generation=excluded.active_reconciliation_generation,updated_at=excluded.updated_at""",
                (source, sensitivity, SourceStatus.HEALTHY.value, batch.reason_code, batch.schema_fingerprint, after_json, now, now, raw_days, derivative_days, current_generation, active_generation, now),
            )
            conn.execute(
                """INSERT INTO context_source_runs(source,started_at,finished_at,status,reason_code,fetched_count,stored_count,derivative_count,skipped_count,cursor_before_hash,cursor_after_hash,dry_run)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,0)""",
                (source, started_at, now, SourceStatus.HEALTHY.value, batch.reason_code, len(batch.raw_items), stored, len(batch.derivatives), batch.skipped, _cursor_hash(before_json), _cursor_hash(after_json)),
            )
        return {"stored": stored, "derivatives": len(batch.derivatives)}

    def record_failure(self, source: str, sensitivity: str, status: SourceStatus, reason_code: str, *, started_at: float) -> None:
        now = time.time()
        before = self.cursor(source)
        cursor_json = _json(before.value if before else {})
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO context_sources(source,sensitivity,status,reason_code,cursor_json,last_attempt_at,consecutive_failures,updated_at)
                   VALUES(?,?,?,?,?,?,1,?) ON CONFLICT(source) DO UPDATE SET status=excluded.status,reason_code=excluded.reason_code,
                   last_attempt_at=excluded.last_attempt_at,consecutive_failures=context_sources.consecutive_failures+1,updated_at=excluded.updated_at""",
                (source, sensitivity, status.value, reason_code, cursor_json, now, now),
            )
            conn.execute(
                """INSERT INTO context_source_runs(source,started_at,finished_at,status,reason_code,fetched_count,stored_count,derivative_count,skipped_count,cursor_before_hash,cursor_after_hash,dry_run)
                   VALUES(?,?,?,?,?,0,0,0,0,?,?,0)""",
                (source, started_at, now, status.value, reason_code, _cursor_hash(cursor_json), _cursor_hash(cursor_json)),
            )

    def pending_derivatives(self, source: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        return [(identifier, event) for identifier, event, tombstone, _ in self.pending_changes(source) if not tombstone]

    def pending_changes(self, source: str | None = None) -> list[tuple[str, dict[str, Any], bool, int]]:
        sql = "SELECT derivative_id,event_json,export_allowed,tombstoned_at,reconciliation_generation FROM context_derivatives WHERE published_at IS NULL"
        params: tuple[Any, ...] = ()
        if source:
            sql += " AND source=?"
            params = (source,)
        sql += " ORDER BY first_seen_at,derivative_id"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            try:
                event = json.loads(row["event_json"])
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                tombstone = row["tombstoned_at"] is not None or not bool(row["export_allowed"])
                result.append((str(row["derivative_id"]), event, tombstone, int(row["reconciliation_generation"] or 0)))
        return result

    def mark_published(self, derivative_ids: Iterable[str]) -> int:
        ids = list(derivative_ids)
        if not ids:
            return 0
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = 0
            for derivative_id in ids:
                count += conn.execute("UPDATE context_derivatives SET published_at=? WHERE derivative_id=?", (now, derivative_id)).rowcount
        return count

    def health(self, source: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT source,status,reason_code,schema_fingerprint,last_attempt_at,last_success_at,consecutive_failures FROM context_sources"
        params: tuple[Any, ...] = ()
        if source:
            sql += " WHERE source=?"
            params = (source,)
        sql += " ORDER BY source"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    @classmethod
    def retention_preview(cls, db_path: Path | str, *, now: float | None = None) -> dict[str, int | bool]:
        path = Path(db_path).expanduser()
        if not path.exists():
            return {"raw_items": 0, "derivatives": 0, "runs": 0, "dry_run": True}
        _reject_db_symlinks(path)
        stamp = time.time() if now is None else now
        uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            raw = conn.execute("SELECT COUNT(*) FROM context_raw_items WHERE expires_at IS NOT NULL AND expires_at<=?", (stamp,)).fetchone()[0]
            derivatives = conn.execute("SELECT COUNT(*) FROM context_derivatives WHERE expires_at IS NOT NULL AND expires_at<=?", (stamp,)).fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM context_source_runs WHERE finished_at<=?", (stamp - 180 * 86400,)).fetchone()[0]
        finally:
            conn.close()
        return {"raw_items": int(raw), "derivatives": int(derivatives), "runs": int(runs), "dry_run": True}

    def retention(self, *, dry_run: bool = False, now: float | None = None) -> dict[str, int | bool]:
        if dry_run:
            return self.retention_preview(self.db_path, now=now)
        stamp = time.time() if now is None else now
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            raw = conn.execute("SELECT COUNT(*) FROM context_raw_items WHERE expires_at IS NOT NULL AND expires_at<=?", (stamp,)).fetchone()[0]
            derivatives = conn.execute("SELECT COUNT(*) FROM context_derivatives WHERE expires_at IS NOT NULL AND expires_at<=?", (stamp,)).fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM context_source_runs WHERE finished_at<=?", (stamp - 180 * 86400,)).fetchone()[0]
            conn.execute("DELETE FROM context_derivatives WHERE expires_at IS NOT NULL AND expires_at<=?", (stamp,))
            conn.execute("DELETE FROM context_raw_items WHERE expires_at IS NOT NULL AND expires_at<=?", (stamp,))
            conn.execute("DELETE FROM context_source_runs WHERE finished_at<=?", (stamp - 180 * 86400,))
        return {"raw_items": int(raw), "derivatives": int(derivatives), "runs": int(runs), "dry_run": False}

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "sources": conn.execute("SELECT COUNT(*) FROM context_sources").fetchone()[0],
                "runs": conn.execute("SELECT COUNT(*) FROM context_source_runs").fetchone()[0],
                "raw_items": conn.execute("SELECT COUNT(*) FROM context_raw_items").fetchone()[0],
                "derivatives": conn.execute("SELECT COUNT(*) FROM context_derivatives").fetchone()[0],
            }
