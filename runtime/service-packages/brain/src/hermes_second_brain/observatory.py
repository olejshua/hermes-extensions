"""Importer, private store, and deterministic report for the Process Observatory.

The passive plugin (``templates/hermes-process-observatory-plugin``) drops
strictly bounded, allowlisted metadata event files into a private spool. This
module drains that spool into a private SQLite store with idempotency,
quarantine for malformed/disallowed files, and TTL retention, then produces a
deterministic local report: counts, duration median/p90/p95, and
error/retry/stall/unverified-completion rates plus the top anomalous tool
families. Small samples are clearly labeled and percentiles are advisory only --
the report never marks anything dead.

The importer re-validates every event against the same fixed allow-list the
plugin uses, so even a tampered spool file cannot smuggle raw content
(command strings, paths, prompts, message bodies) into the store: anything off
the allow-list is quarantined, never imported.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any

from .context_inbox import (
    _chmod_private_file,
    _chmod_sqlite_sidecars,
    _prepare_private_file_parent,
    _reject_symlink_sqlite_paths,
)
from .temporary_memory import canonical_utc, coerce_utc_clock, parse_utc_timestamp

DEFAULT_OBSERVATORY_DB = Path("~/.hermes/second-brain/process-observatory.sqlite3").expanduser()
DEFAULT_SPOOL = Path("~/.hermes/second-brain/process-observatory-spool.d").expanduser()
SCHEMA_VERSION = 1
SMALL_SAMPLE_THRESHOLD = 20
MAX_EVENT_FILE_BYTES = 16_384

# Absolute retention bound on stored rows, enforced independently of the TTL.
# TTL alone cannot bound the store: a burst that stays inside the retention
# window can still grow it without limit. The cap keeps the private database,
# every report scan, and the reviewer packet deterministically bounded even if
# the spool is flooded. The default is a safe upper bound; operators may lower
# (or, within the ceiling, raise) it via ``b_plus.observatory_max_rows``.
DEFAULT_MAX_ROWS = 100_000
MIN_MAX_ROWS = 1
MAX_ROWS_CEILING = 5_000_000

KINDS = {"tool", "llm", "subagent", "session", "kanban", "job"}
STATUSES = {
    "ok", "error", "blocked", "stalled", "finalized", "claimed",
    "verified_completion", "unverified_completion",
}
ERROR_CLASSES = {
    "tool_error", "plugin_block", "api_error", "timeout", "rate_limit",
    "retryable", "terminal", "other",
}
TOOL_FAMILIES = {
    "filesystem", "shell", "network", "delegate", "memory", "search",
    "editor", "kanban", "other",
}
MODEL_FAMILIES = {"claude", "gpt", "gemini", "llama", "qwen", "mistral", "other"}
LIFECYCLES = {"claimed", "completed", "blocked", "finalized", "stalled", "verified", "unverified"}

_HASH_RE = re.compile(r"^[0-9a-f]{1,64}$")
# An opaque, bounded identifier: no whitespace, path, or content characters.
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

_REQUIRED_KEYS = {"schema_version", "event_id", "kind", "ts", "status"}
_OPTIONAL_KEYS = {
    "session_hash", "task_hash", "turn_hash", "actor_hash", "tool_family",
    "model_family", "duration_ms", "error_class", "retried", "lifecycle",
}
_ALLOWED_KEYS = _REQUIRED_KEYS | _OPTIONAL_KEYS
_TRUSTED_SYSTEM_SYMLINKS = {
    Path("/etc"): Path("/private/etc"),
    Path("/tmp"): Path("/private/tmp"),
    Path("/var"): Path("/private/var"),
}


class ObservatoryError(ValueError):
    """Raised when an event fails validation (caller quarantines the file)."""


class ObservatoryStore:
    def __init__(self, db_path: Path | str = DEFAULT_OBSERVATORY_DB):
        self.db_path = Path(db_path).expanduser()
        _reject_symlink_sqlite_paths(self.db_path)
        _prepare_private_file_parent(self.db_path.parent)
        _reject_symlink_sqlite_paths(self.db_path)
        _chmod_private_file(self.db_path)
        _chmod_sqlite_sidecars(self.db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        _reject_symlink_sqlite_paths(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _chmod_private_file(self.db_path)
        _chmod_sqlite_sidecars(self.db_path)
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS observatory_events(
                  event_id TEXT PRIMARY KEY,
                  kind TEXT NOT NULL,
                  ts TEXT NOT NULL,
                  ts_epoch REAL NOT NULL,
                  session_hash TEXT NOT NULL DEFAULT '',
                  task_hash TEXT NOT NULL DEFAULT '',
                  turn_hash TEXT NOT NULL DEFAULT '',
                  actor_hash TEXT NOT NULL DEFAULT '',
                  tool_family TEXT NOT NULL DEFAULT '',
                  model_family TEXT NOT NULL DEFAULT '',
                  duration_ms INTEGER,
                  status TEXT NOT NULL,
                  error_class TEXT NOT NULL DEFAULT '',
                  retried INTEGER NOT NULL DEFAULT 0,
                  lifecycle TEXT NOT NULL DEFAULT '',
                  imported_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_observatory_ts ON observatory_events(ts_epoch);
                CREATE INDEX IF NOT EXISTS idx_observatory_status ON observatory_events(status);
                """
            )

    def insert_event(self, event: dict[str, Any], *, now: str | dt.datetime | None = None) -> bool:
        record = validate_event(event)
        stamp = canonical_utc(coerce_utc_clock(now))
        ts_epoch = parse_utc_timestamp(record["ts"], "ts").timestamp()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO observatory_events(
                  event_id,kind,ts,ts_epoch,session_hash,task_hash,turn_hash,actor_hash,
                  tool_family,model_family,duration_ms,status,error_class,retried,lifecycle,imported_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record["event_id"], record["kind"], record["ts"], ts_epoch,
                    record["session_hash"], record["task_hash"], record["turn_hash"],
                    record["actor_hash"], record["tool_family"], record["model_family"],
                    record["duration_ms"], record["status"], record["error_class"],
                    int(record["retried"]), record["lifecycle"], stamp,
                ),
            )
            return cursor.rowcount == 1

    def count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM observatory_events").fetchone()[0])

    def prune(self, *, now: str | dt.datetime | None = None, retention_days: int = 90) -> int:
        if type(retention_days) is not int or retention_days < 1:
            raise ValueError("retention_days must be a positive integer")
        clock = coerce_utc_clock(now)
        cutoff = clock.timestamp() - retention_days * 86_400
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("DELETE FROM observatory_events WHERE ts_epoch < ?", (cutoff,))
            return int(cursor.rowcount)

    def enforce_row_cap(self, max_rows: int = DEFAULT_MAX_ROWS) -> int:
        """Delete the oldest rows so at most ``max_rows`` remain.

        Runs after spool import and TTL pruning. The most recent ``max_rows``
        events -- ordered deterministically by ``(ts_epoch, event_id)`` so ties
        never depend on insertion or scan order -- are kept; everything older is
        removed. Deletion is by primary key, so dedupe/idempotency of surviving
        rows is untouched: a re-imported event that is still present is still an
        ``INSERT OR IGNORE`` no-op. Returns the number of rows deleted.
        """

        bound = validate_max_rows(max_rows)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                DELETE FROM observatory_events
                WHERE event_id IN (
                  SELECT event_id FROM observatory_events
                  ORDER BY ts_epoch DESC, event_id DESC
                  LIMIT -1 OFFSET ?
                )
                """,
                (bound,),
            )
            return int(cursor.rowcount)

    def rows(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if limit is None:
                cursor = conn.execute("SELECT * FROM observatory_events")
            else:
                # Bounded, newest-first read so a report can never scan more than
                # a fixed number of rows regardless of table size.
                cursor = conn.execute(
                    "SELECT * FROM observatory_events "
                    "ORDER BY ts_epoch DESC, event_id DESC LIMIT ?",
                    (max(0, int(limit)),),
                )
            return [dict(row) for row in cursor]


def validate_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ObservatoryError("event must be a JSON object")
    if any(not isinstance(key, str) for key in event):
        raise ObservatoryError("event keys must be strings")
    unknown = set(event) - _ALLOWED_KEYS
    if unknown:
        # Never echo the disallowed key name; it could itself carry raw content.
        raise ObservatoryError("event contains disallowed keys")
    missing = _REQUIRED_KEYS - set(event)
    if missing:
        raise ObservatoryError("event missing required keys")
    if event.get("schema_version") != SCHEMA_VERSION:
        raise ObservatoryError("unsupported schema_version")
    if not isinstance(event.get("event_id"), str) or not _EVENT_ID_RE.fullmatch(event["event_id"]):
        raise ObservatoryError("event_id must be an opaque bounded identifier")
    if event.get("kind") not in KINDS:
        raise ObservatoryError("kind is not allowlisted")
    if event.get("status") not in STATUSES:
        raise ObservatoryError("status is not allowlisted")
    ts = event.get("ts")
    if not isinstance(ts, str):
        raise ObservatoryError("ts must be a string")
    try:
        parse_utc_timestamp(ts, "ts")
    except ValueError as exc:
        raise ObservatoryError(str(exc)) from exc

    record: dict[str, Any] = {
        "event_id": event["event_id"],
        "kind": event["kind"],
        "ts": ts,
        "status": event["status"],
        "session_hash": _hash_field(event.get("session_hash", ""), "session_hash"),
        "task_hash": _hash_field(event.get("task_hash", ""), "task_hash"),
        "turn_hash": _hash_field(event.get("turn_hash", ""), "turn_hash"),
        "actor_hash": _hash_field(event.get("actor_hash", ""), "actor_hash"),
        "tool_family": _enum_field(event.get("tool_family", ""), TOOL_FAMILIES, "tool_family"),
        "model_family": _enum_field(event.get("model_family", ""), MODEL_FAMILIES, "model_family"),
        "error_class": _enum_field(event.get("error_class", ""), ERROR_CLASSES, "error_class"),
        "lifecycle": _enum_field(event.get("lifecycle", ""), LIFECYCLES, "lifecycle"),
        "duration_ms": _duration_field(event.get("duration_ms")),
        "retried": _bool_field(event.get("retried", False)),
    }
    return record


def import_spool(
    store: ObservatoryStore,
    spool_dir: Path | str = DEFAULT_SPOOL,
    *,
    quarantine_dir: Path | str | None = None,
    now: str | dt.datetime | None = None,
) -> dict[str, Any]:
    spool = _absolute_without_symlink_resolution(Path(spool_dir).expanduser())
    quarantine = _absolute_without_symlink_resolution(
        Path(quarantine_dir).expanduser()
        if quarantine_dir is not None
        else spool.parent / (spool.name + ".quarantine")
    )
    _reject_unsafe_queue_path(spool)
    _reject_unsafe_queue_path(quarantine)
    imported = skipped = quarantined = quarantine_failed = 0
    if not spool.exists():
        return {
            "imported": 0,
            "skipped": 0,
            "quarantined": 0,
            "quarantine_failed": 0,
        }
    _secure_directory(spool, create=False)
    for path in sorted(spool.glob("*.jsonl")):
        try:
            raw = _read_bounded_private_event(path)
            event = json.loads(raw)
            inserted = store.insert_event(event, now=now)
        except (OSError, UnicodeError, json.JSONDecodeError, ObservatoryError):
            if _quarantine_file(path, quarantine):
                quarantined += 1
            else:
                quarantine_failed += 1
            continue
        if inserted:
            imported += 1
        else:
            skipped += 1
        path.unlink(missing_ok=True)
    return {
        "imported": imported,
        "skipped": skipped,
        "quarantined": quarantined,
        "quarantine_failed": quarantine_failed,
    }


def observatory_report(
    store: ObservatoryStore, *, top: int = 5, max_rows: int = DEFAULT_MAX_ROWS
) -> dict[str, Any]:
    # The scan is bounded to the newest ``max_rows`` rows so the report stays
    # deterministically bounded even if the row cap was never enforced. A soft
    # clamp (rather than a raise) keeps reporting fail-safe for odd callers.
    bound = max(MIN_MAX_ROWS, min(int(max_rows), MAX_ROWS_CEILING))
    rows = store.rows(limit=bound)
    total = len(rows)
    durations = sorted(int(row["duration_ms"]) for row in rows if row["duration_ms"] is not None)
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    anomalies: dict[str, dict[str, int]] = {}
    task_anomalies: dict[str, dict[str, int]] = {}
    errors = retries = stalls = unverified = 0
    for row in rows:
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        anomalous = row["status"] in ("error", "stalled") or bool(row["retried"])
        if row["status"] == "error":
            errors += 1
        if row["retried"]:
            retries += 1
        if row["status"] == "stalled":
            stalls += 1
        if row["status"] == "unverified_completion":
            unverified += 1
        if anomalous:
            family = row["tool_family"] or "other"
            bucket = anomalies.setdefault(family, {"errors": 0, "stalls": 0, "retries": 0})
            task_bucket = task_anomalies.setdefault(
                row["kind"], {"errors": 0, "stalls": 0, "retries": 0}
            )
            if row["status"] == "error":
                bucket["errors"] += 1
                task_bucket["errors"] += 1
            if row["status"] == "stalled":
                bucket["stalls"] += 1
                task_bucket["stalls"] += 1
            if row["retried"]:
                bucket["retries"] += 1
                task_bucket["retries"] += 1
    ranked = sorted(
        (
            {"tool_family": family, **counts, "total": counts["errors"] + counts["stalls"] + counts["retries"]}
            for family, counts in anomalies.items()
        ),
        key=lambda item: (-item["total"], item["tool_family"]),
    )[:top]
    ranked_tasks = sorted(
        (
            {
                "task_class": task_class,
                **counts,
                "total": counts["errors"] + counts["stalls"] + counts["retries"],
            }
            for task_class, counts in task_anomalies.items()
        ),
        key=lambda item: (-item["total"], item["task_class"]),
    )[:top]
    return {
        "total": total,
        "small_sample": total < SMALL_SAMPLE_THRESHOLD,
        "sample_size": total,
        "by_kind": dict(sorted(by_kind.items())),
        "by_status": dict(sorted(by_status.items())),
        "error_rate": _rate(errors, total),
        "retry_rate": _rate(retries, total),
        "stall_rate": _rate(stalls, total),
        "unverified_completion_rate": _rate(unverified, total),
        "duration_ms": {
            "count": len(durations),
            "median": _percentile(durations, 50),
            "p90": _percentile(durations, 90),
            "p95": _percentile(durations, 95),
        },
        "top_anomalous_tool_families": ranked,
        "top_anomalous_task_classes": ranked_tasks,
        "note": (
            "Advisory metadata-only summary. Percentiles are descriptive; a high "
            "p95 alone never marks a job dead. Small samples are labeled."
        ),
    }


# ------------------------------------------------------------------ helpers
def validate_max_rows(value: Any) -> int:
    """Validate an ``observatory_max_rows`` setting as a bounded integer.

    Booleans are rejected explicitly (``bool`` is an ``int`` subclass) and the
    value must fall within ``[MIN_MAX_ROWS, MAX_ROWS_CEILING]``.
    """

    if type(value) is not int or not MIN_MAX_ROWS <= value <= MAX_ROWS_CEILING:
        raise ValueError(
            f"observatory_max_rows must be an integer in "
            f"[{MIN_MAX_ROWS}, {MAX_ROWS_CEILING}]"
        )
    return value


def _hash_field(value: Any, label: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise ObservatoryError(f"{label} must be an opaque lowercase-hex hash")
    return value


def _enum_field(value: Any, allowed: set[str], label: str) -> str:
    if value in (None, ""):
        return ""
    if value not in allowed:
        raise ObservatoryError(f"{label} is not allowlisted")
    return value


def _duration_field(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObservatoryError("duration_ms must be a number")
    ms = int(value)
    if not 0 <= ms <= 30 * 24 * 60 * 60 * 1000:
        raise ObservatoryError("duration_ms is out of bounds")
    return ms


def _bool_field(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise ObservatoryError("retried must be a boolean")


def _rate(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return count / total


def _percentile(values: list[int], percentile: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    rank = (percentile / 100) * (len(values) - 1)
    low = int(rank)
    high = min(low + 1, len(values) - 1)
    fraction = rank - low
    return values[low] + (values[high] - values[low]) * fraction


def _absolute_without_symlink_resolution(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_unsafe_queue_path(path: Path) -> None:
    """Reject links except exact root-owned compatibility aliases."""

    current = _absolute_without_symlink_resolution(path)
    leaf = True
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISLNK(info.st_mode):
            if leaf or not _trusted_system_symlink(current, info):
                raise ValueError("refusing symlinked observatory queue path")
        parent = current.parent
        if parent == current:
            return
        current = parent
        leaf = False


def _trusted_system_symlink(path: Path, info: os.stat_result) -> bool:
    expected = _TRUSTED_SYSTEM_SYMLINKS.get(path)
    if expected is None or info.st_uid != 0:
        return False
    try:
        target = Path(os.path.abspath(os.path.join(path.parent, os.readlink(path))))
    except OSError:
        return False
    return target == expected


def _secure_directory(path: Path, *, create: bool) -> None:
    _reject_unsafe_queue_path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        _reject_unsafe_queue_path(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("observatory queue directory is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("observatory queue path is not a directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("observatory queue directory has a different owner")
    try:
        os.chmod(path, 0o700, follow_symlinks=False)
    except (NotImplementedError, OSError) as exc:
        raise ValueError("cannot enforce private observatory queue permissions") from exc
    verified = path.lstat()
    if stat.S_IMODE(verified.st_mode) != 0o700:
        raise ValueError("observatory queue directory is not private")


def _read_bounded_private_event(path: Path) -> str:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ObservatoryError("unsafe event file type")
    if info.st_nlink != 1:
        raise ObservatoryError("unsafe event file link count")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ObservatoryError("event file has a different owner")
    if info.st_size > MAX_EVENT_FILE_BYTES:
        raise ObservatoryError("event file exceeds the bounded size")
    try:
        os.chmod(path, 0o600, follow_symlinks=False)
    except (NotImplementedError, OSError) as exc:
        raise ObservatoryError("cannot enforce private event permissions") from exc

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != info.st_dev
            or opened.st_ino != info.st_ino
            or opened.st_size > MAX_EVENT_FILE_BYTES
        ):
            raise ObservatoryError("event file changed during safe open")
        chunks: list[bytes] = []
        remaining = MAX_EVENT_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_EVENT_FILE_BYTES:
            raise ObservatoryError("event file exceeds the bounded size")
        return payload.decode("utf-8")
    finally:
        os.close(descriptor)


def _quarantine_file(path: Path, quarantine_dir: Path) -> bool:
    try:
        _secure_directory(quarantine_dir, create=True)
        info = path.lstat()
        opaque_name = hashlib.sha256(
            f"{path.name}\0{info.st_mtime_ns}\0{info.st_size}".encode("utf-8")
        ).hexdigest()
        target = quarantine_dir / f"{opaque_name}.invalid"
        suffix = 0
        while target.exists() and suffix < 100:
            suffix += 1
            target = quarantine_dir / f"{opaque_name}-{suffix}.invalid"
        if target.exists():
            return False
        if stat.S_ISLNK(info.st_mode):
            path.unlink()
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(descriptor, b'{"error":"unsafe_event_file"}\n')
            finally:
                os.close(descriptor)
        else:
            path.replace(target)
            os.chmod(target, 0o600, follow_symlinks=False)
        verified = target.lstat()
        if not stat.S_ISREG(verified.st_mode) or stat.S_IMODE(verified.st_mode) != 0o600:
            return False
        return True
    except (OSError, ValueError):
        # Preserve the input for a later retry if quarantine itself is
        # unavailable. Silently deleting malformed input would defeat audit.
        return False
