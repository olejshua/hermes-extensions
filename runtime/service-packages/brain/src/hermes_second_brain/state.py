from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .scanner import ScannedResource

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS resources (
  source_id TEXT PRIMARY KEY,
  source_root_id TEXT NOT NULL,
  namespace TEXT NOT NULL,
  path TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  status TEXT NOT NULL,
  ov_resource_id TEXT,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until REAL,
  lease_owner TEXT,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  last_synced REAL,
  sync_receipt TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resources_status ON resources(status);
CREATE INDEX IF NOT EXISTS idx_resources_root_seen ON resources(source_root_id,last_seen);
"""


@dataclass(frozen=True)
class ResourceRow:
    source_id: str
    source_root_id: str
    namespace: str
    path: str
    relative_path: str
    sha256: str
    status: str
    attempts: int
    ov_resource_id: str | None
    lease_owner: str | None = None
    sync_receipt: str = ""


class State:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(resources)").fetchall()}
        if "lease_until" not in columns:
            conn.execute("ALTER TABLE resources ADD COLUMN lease_until REAL")
        if "lease_owner" not in columns:
            conn.execute("ALTER TABLE resources ADD COLUMN lease_owner TEXT")
        if "sync_receipt" not in columns:
            conn.execute(
                "ALTER TABLE resources ADD COLUMN sync_receipt TEXT NOT NULL DEFAULT ''"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upsert_scan(self, resources: list[ScannedResource], source_root_id: str, dry_run: bool = False) -> tuple[list[ResourceRow], list[ResourceRow]]:
        now = time.time()
        changed: list[ResourceRow] = []
        deleted: list[ResourceRow] = []
        seen = {r.source_id for r in resources}
        with self.transaction() as conn:
            for res in resources:
                old = conn.execute("SELECT sha256,status,attempts,ov_resource_id FROM resources WHERE source_id=?", (res.source_id,)).fetchone()
                needs_enqueue = old is None or old["sha256"] != res.sha256 or old["status"] in {"failed", "deleted"}
                status = "pending" if needs_enqueue else old["status"]
                attempts = 0 if needs_enqueue else int(old["attempts"])
                ov_resource_id = None if old is None else old["ov_resource_id"]
                row_values = (
                    res.source_id,
                    res.source_root_id,
                    res.namespace,
                    str(res.path),
                    res.relative_path,
                    res.sha256,
                    res.size_bytes,
                    res.mtime_ns,
                    status,
                    attempts,
                    now,
                    now,
                )
                if needs_enqueue:
                    changed.append(ResourceRow(res.source_id, res.source_root_id, res.namespace, str(res.path), res.relative_path, res.sha256, status, attempts, ov_resource_id))
                if not dry_run:
                    conn.execute(
                        """
                        INSERT INTO resources(source_id,source_root_id,namespace,path,relative_path,sha256,size_bytes,mtime_ns,status,attempts,first_seen,last_seen)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(source_id) DO UPDATE SET
                          path=excluded.path, relative_path=excluded.relative_path, sha256=excluded.sha256,
                          size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns, status=excluded.status,
                          attempts=excluded.attempts, last_seen=excluded.last_seen, error=NULL,
                          lease_until=CASE
                            WHEN resources.sha256=excluded.sha256 AND resources.status='in_progress'
                            THEN resources.lease_until ELSE NULL END,
                          lease_owner=CASE
                            WHEN resources.sha256=excluded.sha256 AND resources.status='in_progress'
                            THEN resources.lease_owner ELSE NULL END
                        """,
                        row_values,
                    )
            rows = conn.execute("SELECT * FROM resources WHERE source_root_id=? AND status!='deleted'", (source_root_id,)).fetchall()
            for row in rows:
                if row["source_id"] not in seen:
                    deleted.append(_row(row))
                    if not dry_run:
                        conn.execute("UPDATE resources SET status='deleted', last_seen=?, error=NULL, lease_until=NULL, lease_owner=NULL WHERE source_id=?", (now, row["source_id"]))
        return changed, deleted

    def mark_source_missing(self, source_root_id: str, dry_run: bool = False) -> list[ResourceRow]:
        now = time.time()
        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM resources WHERE source_root_id=? AND status!='deleted'", (source_root_id,)).fetchall()
            if rows and not dry_run:
                conn.execute(
                    "UPDATE resources SET status='deleted', last_seen=?, error=NULL, lease_until=NULL, lease_owner=NULL WHERE source_root_id=? AND status!='deleted'",
                    (now, source_root_id),
                )
            return [_row(row) for row in rows]

    def pending(self, limit: int | None = None) -> list[ResourceRow]:
        sql = "SELECT * FROM resources WHERE status='pending' ORDER BY last_seen, source_id"
        args: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            args = (limit,)
        with self.connect() as conn:
            return [_row(row) for row in conn.execute(sql, args).fetchall()]

    def claim_pending(
        self,
        *,
        limit: int | None = None,
        lease_seconds: float = 600.0,
        owner: str | None = None,
        include_failed: bool = True,
        exclude_source_ids: set[str] | None = None,
        allowed_source_root_ids: set[str] | None = None,
    ) -> list[ResourceRow]:
        now = time.time()
        lease_owner = owner or f"pid-lease-{uuid.uuid4().hex}"
        statuses = ("pending", "failed") if include_failed else ("pending",)
        excluded = tuple(sorted(exclude_source_ids or set()))
        allowed = None if allowed_source_root_ids is None else tuple(sorted(allowed_source_root_ids))
        with self.transaction() as conn:
            if allowed == ():
                return []
            root_clause = ""
            root_args: tuple[object, ...] = ()
            if allowed is not None:
                root_placeholders = ",".join("?" for _ in allowed)
                root_clause = f" AND source_root_id IN ({root_placeholders})"
                root_args = allowed
            conn.execute(
                "UPDATE resources SET status='pending', lease_until=NULL, lease_owner=NULL "
                f"WHERE status='in_progress' AND (lease_until IS NULL OR lease_until<=?){root_clause}",
                (now, *root_args),
            )
            status_placeholders = ",".join("?" for _ in statuses)
            sql = f"SELECT source_id FROM resources WHERE status IN ({status_placeholders})"
            args: tuple[object, ...] = statuses
            if allowed is not None:
                sql += f" AND source_root_id IN ({root_placeholders})"
                args += allowed
            if excluded:
                excluded_placeholders = ",".join("?" for _ in excluded)
                sql += f" AND source_id NOT IN ({excluded_placeholders})"
                args += excluded
            sql += " ORDER BY last_seen, source_id"
            if limit is not None:
                sql += " LIMIT ?"
                args += (limit,)
            ids = [row["source_id"] for row in conn.execute(sql, args).fetchall()]
            if not ids:
                return []
            lease_until = now + lease_seconds
            status_update_placeholders = ",".join("?" for _ in statuses)
            update_root_clause = ""
            update_root_args: tuple[object, ...] = ()
            if allowed is not None:
                update_root_clause = f" AND source_root_id IN ({root_placeholders})"
                update_root_args = allowed
            conn.executemany(
                f"UPDATE resources SET status='in_progress', lease_until=?, lease_owner=? WHERE source_id=? AND status IN ({status_update_placeholders}){update_root_clause}",
                [(lease_until, lease_owner, source_id, *statuses, *update_root_args) for source_id in ids],
            )
            placeholders = ",".join("?" for _ in ids)
            select_root_clause = ""
            select_root_args: tuple[object, ...] = ()
            if allowed is not None:
                select_root_clause = f" AND source_root_id IN ({root_placeholders})"
                select_root_args = allowed
            rows = conn.execute(f"SELECT * FROM resources WHERE source_id IN ({placeholders}) AND lease_owner=?{select_root_clause} ORDER BY last_seen, source_id", (*ids, lease_owner, *select_root_args)).fetchall()
            return [_row(row) for row in rows]

    def mark_synced(self, source_id: str, sha256: str, ov_resource_id: str, *, owner: str | None = None) -> bool:
        if (
            not isinstance(ov_resource_id, str)
            or not ov_resource_id.startswith("viking://resources/")
            or len(ov_resource_id) > 2048
            or any(ord(character) < 32 for character in ov_resource_id)
        ):
            raise ValueError("OpenViking sync requires a real remote resource ID")
        synced_at = time.time()
        receipt = _receipt(source_id, sha256, ov_resource_id, synced_at)
        with self.transaction() as conn:
            owner_clause = " AND lease_owner=?" if owner is not None else ""
            args: tuple[object, ...] = (
                ov_resource_id, synced_at, receipt, source_id, sha256
            )
            if owner is not None:
                args += (owner,)
            cursor = conn.execute(
                f"UPDATE resources SET status='synced',ov_resource_id=?,error=NULL,last_synced=?,"
                f"sync_receipt=?,lease_until=NULL,lease_owner=NULL WHERE source_id=? AND sha256=? "
                f"AND status='in_progress'{owner_clause}",
                args,
            )
            return cursor.rowcount == 1

    def verified_sync_receipt(self, source_id: str, sha256: str) -> dict[str, object] | None:
        """Return an integrity-checked receipt for one exact synced resource."""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT source_id,sha256,status,ov_resource_id,last_synced,sync_receipt "
                "FROM resources WHERE source_id=? AND sha256=?",
                (source_id, sha256),
            ).fetchone()
        if (
            row is None
            or row["status"] != "synced"
            or not row["ov_resource_id"]
            or row["last_synced"] is None
            or not row["sync_receipt"]
        ):
            return None
        expected = _receipt(
            str(row["source_id"]),
            str(row["sha256"]),
            str(row["ov_resource_id"]),
            float(row["last_synced"]),
        )
        if str(row["sync_receipt"]) != expected:
            return None
        return {
            "source_id": str(row["source_id"]),
            "sha256": str(row["sha256"]),
            "ov_resource_id": str(row["ov_resource_id"]),
            "synced_at": float(row["last_synced"]),
            "receipt": expected,
        }

    def mark_failed(self, source_id: str, sha256: str, error: str, *, owner: str | None = None) -> bool:
        with self.transaction() as conn:
            owner_clause = " AND lease_owner=?" if owner is not None else ""
            args: tuple[object, ...] = (error[:1000], source_id, sha256)
            if owner is not None:
                args += (owner,)
            cursor = conn.execute(
                f"UPDATE resources SET status='failed', attempts=attempts+1, error=?, lease_until=NULL, lease_owner=NULL WHERE source_id=? AND sha256=? AND status='in_progress'{owner_clause}",
                args,
            )
            return cursor.rowcount == 1


def _row(row: sqlite3.Row) -> ResourceRow:
    return ResourceRow(
        source_id=row["source_id"],
        source_root_id=row["source_root_id"],
        namespace=row["namespace"],
        path=row["path"],
        relative_path=row["relative_path"],
        sha256=row["sha256"],
        status=row["status"],
        attempts=int(row["attempts"]),
        ov_resource_id=row["ov_resource_id"],
        lease_owner=row["lease_owner"],
        sync_receipt=str(row["sync_receipt"] or ""),
    )


def _receipt(source_id: str, sha256: str, ov_resource_id: str, synced_at: float) -> str:
    material = (
        f"openviking-sync-v1\0{source_id}\0{sha256}\0{ov_resource_id}\0{synced_at:.6f}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
