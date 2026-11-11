"""Three separate durable lifecycles: intents, tool jobs/work orders, and their
metadata-only linkage to the (independent) memory lifecycles.

Design invariants enforced here:

* State transitions are validated against explicit allow-lists and fail closed.
* Transitions are idempotent where safe (creation by idempotency key, and any
  transition carrying an ``idempotency_key`` replays as a no-op).
* Every mutation runs inside a single ``BEGIN IMMEDIATE`` transaction.
* The SQLite store and its parent are kept private (0700/0600) and symlinked
  paths are rejected, reusing the Context Inbox hardening helpers.
* A job can only be ``completed`` from ``verifying`` and only with explicit
  verification-evidence metadata; a direct ``running -> completed`` fails.
* Jobs carry heartbeats/leases, checkpoints, a bounded retry budget, and an
  optional owning intent. The stale-job detector marks ``stalled`` solely from
  the explicit liveness condition (an expired lease); it never inspects the OS,
  never kills a process, and is idempotent.
* Every surface returns bounded metadata only -- no command strings, prompts,
  payloads, or raw external content are stored or emitted.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Iterable

from .context_inbox import (
    _chmod_private_file,
    _chmod_sqlite_sidecars,
    _prepare_private_file_parent,
    _reject_symlink_sqlite_paths,
    redact_sensitive_text,
)
from .temporary_memory import canonical_utc, coerce_utc_clock, validate_idempotency_key

import sqlite3

DEFAULT_LIFECYCLE_DB = Path("~/.hermes/second-brain/lifecycle.sqlite3").expanduser()

INTENT_STATES = ("captured", "clarified", "approved", "planned", "done", "cancelled")
INTENT_TERMINAL = frozenset({"done", "cancelled"})
INTENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "captured": frozenset({"clarified", "cancelled"}),
    "clarified": frozenset({"approved", "cancelled"}),
    "approved": frozenset({"planned", "cancelled"}),
    "planned": frozenset({"done", "cancelled"}),
    "done": frozenset(),
    "cancelled": frozenset(),
}

JOB_STATES = (
    "queued",
    "running",
    "checkpointed",
    "verifying",
    "completed",
    "failed",
    "stalled",
    "cancelled",
)
JOB_TERMINAL = frozenset({"completed", "failed", "cancelled"})
JOB_ACTIVE_LEASE_STATES = frozenset({"running", "checkpointed", "verifying"})

MAX_SUMMARY_CHARACTERS = 200
MAX_SOURCE_CHARACTERS = 64
MAX_KIND_CHARACTERS = 64
MAX_CHECKPOINT_CHARACTERS = 200
MAX_EVIDENCE_CHARACTERS = 500
MAX_ERROR_TYPE_CHARACTERS = 64
MIN_ATTEMPTS = 1
MAX_ATTEMPTS = 20


class LifecycleError(ValueError):
    """Raised for validation, idempotency, or ownership failures."""


class InvalidTransition(LifecycleError):
    """Raised when a requested state transition is not allowed."""


class LifecycleStore:
    def __init__(self, db_path: Path | str = DEFAULT_LIFECYCLE_DB):
        self.db_path = Path(db_path).expanduser()
        _reject_symlink_sqlite_paths(self.db_path)
        _prepare_private_file_parent(self.db_path.parent)
        _reject_symlink_sqlite_paths(self.db_path)
        _chmod_private_file(self.db_path)
        _chmod_sqlite_sidecars(self.db_path)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        _reject_symlink_sqlite_paths(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _chmod_private_file(self.db_path)
        _chmod_sqlite_sidecars(self.db_path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_intents(
                  intent_id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  content_sha256 TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  source TEXT NOT NULL,
                  provenance_hash TEXT NOT NULL DEFAULT '',
                  requires_approval INTEGER NOT NULL CHECK(requires_approval IN (0,1)),
                  state TEXT NOT NULL CHECK(state IN
                    ('captured','clarified','approved','planned','done','cancelled')),
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  terminal_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS lifecycle_jobs(
                  job_id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  intent_id TEXT,
                  job_kind TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN
                    ('queued','running','checkpointed','verifying','completed','failed','stalled','cancelled')),
                  owner TEXT NOT NULL DEFAULT '',
                  lease_until REAL NOT NULL DEFAULT 0,
                  lease_generation INTEGER NOT NULL DEFAULT 0,
                  heartbeat_at TEXT NOT NULL DEFAULT '',
                  heartbeat_epoch REAL NOT NULL DEFAULT 0,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  max_attempts INTEGER NOT NULL,
                  checkpoint TEXT NOT NULL DEFAULT '',
                  verification_evidence TEXT NOT NULL DEFAULT '',
                  error_type TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  terminal_at TEXT NOT NULL DEFAULT '',
                  FOREIGN KEY(intent_id) REFERENCES lifecycle_intents(intent_id)
                );
                CREATE TABLE IF NOT EXISTS lifecycle_transitions(
                  transition_id TEXT PRIMARY KEY,
                  entity_type TEXT NOT NULL CHECK(entity_type IN ('intent','job')),
                  entity_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  from_state TEXT NOT NULL,
                  to_state TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(entity_type, entity_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_lifecycle_jobs_state ON lifecycle_jobs(state);
                CREATE INDEX IF NOT EXISTS idx_lifecycle_jobs_lease ON lifecycle_jobs(state, lease_until);
                CREATE INDEX IF NOT EXISTS idx_lifecycle_intents_state ON lifecycle_intents(state);
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(lifecycle_intents)")
            }
            if "provenance_hash" not in columns:
                conn.execute(
                    "ALTER TABLE lifecycle_intents ADD COLUMN provenance_hash "
                    "TEXT NOT NULL DEFAULT ''"
                )
            job_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(lifecycle_jobs)")
            }
            if "lease_generation" not in job_columns:
                conn.execute(
                    "ALTER TABLE lifecycle_jobs ADD COLUMN lease_generation "
                    "INTEGER NOT NULL DEFAULT 0"
                )

    # ------------------------------------------------------------------ intents
    def capture_intent(
        self,
        *,
        idempotency_key: str,
        summary: str,
        source: str,
        requires_approval: bool = True,
        provenance_hash: str = "",
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        key = validate_idempotency_key(idempotency_key)
        clean_summary = _bounded_redacted(summary, "summary", MAX_SUMMARY_CHARACTERS)
        clean_source = _bounded_token(source, "source", MAX_SOURCE_CHARACTERS)
        if not isinstance(provenance_hash, str) or (
            provenance_hash and not re.fullmatch(r"[0-9a-f]{64}", provenance_hash)
        ):
            raise LifecycleError("provenance_hash must be empty or a SHA-256 hex digest")
        if not isinstance(requires_approval, bool):
            raise LifecycleError("requires_approval must be a boolean")
        stamp = canonical_utc(coerce_utc_clock(now))
        intent_id = "int_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        content = (
            f"{clean_summary}\0{clean_source}\0{int(requires_approval)}\0{provenance_hash}"
        )
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM lifecycle_intents WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if existing["content_sha256"] != content_hash:
                    raise LifecycleError("idempotency_key already exists with different intent content")
                return _intent_result(dict(existing), created=False)
            conn.execute(
                """
                INSERT INTO lifecycle_intents(
                  intent_id,idempotency_key,content_sha256,summary,source,provenance_hash,requires_approval,
                  state,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    intent_id,
                    key,
                    content_hash,
                    clean_summary,
                    clean_source,
                    provenance_hash,
                    int(requires_approval),
                    "captured",
                    stamp,
                    stamp,
                ),
            )
            row = conn.execute(
                "SELECT * FROM lifecycle_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
        return _intent_result(dict(row), created=True)

    def advance_intent(
        self,
        intent_id: str,
        to_state: str,
        *,
        idempotency_key: str | None = None,
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        if to_state not in INTENT_STATES:
            raise LifecycleError(f"unknown intent state: {to_state!r}")
        return self._transition_intent(intent_id, to_state, idempotency_key=idempotency_key, now=now)

    def cancel_intent(
        self,
        intent_id: str,
        *,
        idempotency_key: str | None = None,
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        return self._transition_intent(intent_id, "cancelled", idempotency_key=idempotency_key, now=now)

    def get_intent(self, intent_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM lifecycle_intents WHERE intent_id=?", (intent_id,)).fetchone()
        if row is None:
            raise LifecycleError("unknown intent")
        return _intent_result(dict(row), created=False)

    def list_intents(
        self, *, state: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if state is not None and state not in INTENT_STATES:
            raise LifecycleError("unknown intent state")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise LifecycleError("limit must be an integer from 1 to 1000")
        with self.connect() as conn:
            if state is None:
                rows = conn.execute(
                    "SELECT * FROM lifecycle_intents ORDER BY created_at,intent_id LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM lifecycle_intents WHERE state=? "
                    "ORDER BY created_at,intent_id LIMIT ?",
                    (state, limit),
                ).fetchall()
        return [_intent_result(dict(row), created=False) for row in rows]

    def _transition_intent(
        self,
        intent_id: str,
        to_state: str,
        *,
        idempotency_key: str | None,
        now: str | dt.datetime | None,
    ) -> dict[str, Any]:
        stamp = canonical_utc(coerce_utc_clock(now))
        key = validate_idempotency_key(idempotency_key) if idempotency_key is not None else None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM lifecycle_intents WHERE intent_id=?", (intent_id,)).fetchone()
            if row is None:
                raise LifecycleError("unknown intent")
            if key is not None and _replayed(conn, "intent", intent_id, key):
                return _intent_result(dict(conn.execute(
                    "SELECT * FROM lifecycle_intents WHERE intent_id=?", (intent_id,)
                ).fetchone()), created=False)
            current = row["state"]
            if current == to_state:
                # A same-state request without a fresh idempotency key is a no-op.
                return _intent_result(dict(row), created=False)
            if to_state not in INTENT_TRANSITIONS[current]:
                raise InvalidTransition(f"intent cannot move {current} -> {to_state}")
            terminal_at = stamp if to_state in INTENT_TERMINAL else row["terminal_at"]
            conn.execute(
                "UPDATE lifecycle_intents SET state=?,updated_at=?,terminal_at=? WHERE intent_id=?",
                (to_state, stamp, terminal_at, intent_id),
            )
            _record_transition(conn, "intent", intent_id, key, current, to_state, stamp)
            updated = conn.execute(
                "SELECT * FROM lifecycle_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
        return _intent_result(dict(updated), created=False)

    # --------------------------------------------------------------------- jobs
    def enqueue_job(
        self,
        *,
        idempotency_key: str,
        job_kind: str,
        intent_id: str | None = None,
        max_attempts: int = 3,
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        key = validate_idempotency_key(idempotency_key)
        clean_kind = _bounded_token(job_kind, "job_kind", MAX_KIND_CHARACTERS)
        if type(max_attempts) is not int or not MIN_ATTEMPTS <= max_attempts <= MAX_ATTEMPTS:
            raise LifecycleError(f"max_attempts must be an integer from {MIN_ATTEMPTS} to {MAX_ATTEMPTS}")
        stamp = canonical_utc(coerce_utc_clock(now))
        job_id = "job_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM lifecycle_jobs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if existing["job_kind"] != clean_kind or (existing["intent_id"] or None) != intent_id:
                    raise LifecycleError("idempotency_key already exists with different job content")
                return _job_result(dict(existing), created=False)
            if intent_id is not None:
                if conn.execute(
                    "SELECT 1 FROM lifecycle_intents WHERE intent_id=?", (intent_id,)
                ).fetchone() is None:
                    raise LifecycleError("unknown intent_id for job linkage")
            conn.execute(
                """
                INSERT INTO lifecycle_jobs(
                  job_id,idempotency_key,intent_id,job_kind,state,max_attempts,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (job_id, key, intent_id, clean_kind, "queued", max_attempts, stamp, stamp),
            )
            row = conn.execute("SELECT * FROM lifecycle_jobs WHERE job_id=?", (job_id,)).fetchone()
        return _job_result(dict(row), created=True)

    def start_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_seconds: float,
        idempotency_key: str | None = None,
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        clean_owner = _bounded_token(owner, "owner", MAX_KIND_CHARACTERS)
        clock = coerce_utc_clock(now)
        lease_until = clock.timestamp() + _validate_lease(lease_seconds)
        stamp = canonical_utc(clock)

        def updates(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
            return {
                "owner": clean_owner,
                "lease_until": lease_until,
                "lease_generation": int(row["lease_generation"]) + 1,
                "heartbeat_at": stamp,
                "heartbeat_epoch": clock.timestamp(),
                "attempts": int(row["attempts"]) + 1,
                "error_type": "",
                "verification_evidence": "",
                "terminal_at": "",
            }

        return self._transition_job(
            job_id, {"queued"}, "running", stamp, updates,
            idempotency_key=idempotency_key,
        )

    def heartbeat(
        self,
        job_id: str,
        *,
        owner: str,
        lease_generation: int,
        lease_seconds: float,
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        clean_owner = _bounded_token(owner, "owner", MAX_KIND_CHARACTERS)
        generation = _validate_generation(lease_generation)
        clock = coerce_utc_clock(now)
        lease_until = clock.timestamp() + _validate_lease(lease_seconds)
        stamp = canonical_utc(clock)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._require_job(conn, job_id)
            if row["state"] not in JOB_ACTIVE_LEASE_STATES:
                raise InvalidTransition(f"cannot heartbeat a job in state {row['state']}")
            _require_lease_fence(row, clean_owner, generation, clock.timestamp())
            conn.execute(
                "UPDATE lifecycle_jobs SET lease_until=?,heartbeat_at=?,heartbeat_epoch=?,updated_at=? "
                "WHERE job_id=? AND owner=? AND lease_generation=? AND lease_until>?",
                (
                    lease_until, stamp, clock.timestamp(), stamp, job_id,
                    clean_owner, generation, clock.timestamp(),
                ),
            )
            updated = conn.execute("SELECT * FROM lifecycle_jobs WHERE job_id=?", (job_id,)).fetchone()
        return _job_result(dict(updated), created=False)

    def checkpoint_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_generation: int,
        checkpoint: str,
        idempotency_key: str | None = None,
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        clean_owner = _bounded_token(owner, "owner", MAX_KIND_CHARACTERS)
        generation = _validate_generation(lease_generation)
        clean_checkpoint = _bounded_redacted(checkpoint, "checkpoint", MAX_CHECKPOINT_CHARACTERS)
        clock = coerce_utc_clock(now)
        stamp = canonical_utc(clock)

        def updates(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
            return {"checkpoint": clean_checkpoint}

        return self._transition_job(
            job_id, {"running"}, "checkpointed", stamp, updates,
            idempotency_key=idempotency_key,
            lease_fence=(clean_owner, generation, clock.timestamp(), True),
        )

    def resume_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_generation: int,
        lease_seconds: float,
        idempotency_key: str | None = None,
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        clean_owner = _bounded_token(owner, "owner", MAX_KIND_CHARACTERS)
        generation = _validate_generation(lease_generation)
        clock = coerce_utc_clock(now)
        lease_until = clock.timestamp() + _validate_lease(lease_seconds)
        stamp = canonical_utc(clock)

        def updates(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
            return {
                "owner": clean_owner,
                "lease_until": lease_until,
                "heartbeat_at": stamp,
                "heartbeat_epoch": clock.timestamp(),
            }

        return self._transition_job(
            job_id, {"checkpointed"}, "running", stamp, updates,
            idempotency_key=idempotency_key,
            lease_fence=(clean_owner, generation, clock.timestamp(), True),
        )

    def begin_verification(
        self,
        job_id: str,
        *,
        owner: str,
        lease_generation: int,
        idempotency_key: str | None = None,
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        clean_owner = _bounded_token(owner, "owner", MAX_KIND_CHARACTERS)
        generation = _validate_generation(lease_generation)
        clock = coerce_utc_clock(now)
        stamp = canonical_utc(clock)

        def updates(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
            return {}

        return self._transition_job(
            job_id, {"running", "checkpointed"}, "verifying", stamp, updates,
            idempotency_key=idempotency_key,
            lease_fence=(clean_owner, generation, clock.timestamp(), True),
        )

    def complete_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_generation: int,
        verification_evidence: str,
        idempotency_key: str | None = None,
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        clean_evidence = _bounded_redacted(
            verification_evidence, "verification_evidence", MAX_EVIDENCE_CHARACTERS
        )
        if not clean_evidence:
            raise LifecycleError("completion requires non-empty verification evidence metadata")
        clean_owner = _bounded_token(owner, "owner", MAX_KIND_CHARACTERS)
        generation = _validate_generation(lease_generation)
        clock = coerce_utc_clock(now)
        stamp = canonical_utc(clock)

        def updates(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
            return {
                "verification_evidence": clean_evidence,
                "owner": "",
                "lease_until": 0,
                "terminal_at": stamp,
            }

        # Only 'verifying' -> 'completed' is legal; a direct running->completed
        # is rejected because 'running' is not in the allowed source set.
        return self._transition_job(
            job_id, {"verifying"}, "completed", stamp, updates,
            idempotency_key=idempotency_key,
            lease_fence=(clean_owner, generation, clock.timestamp(), True),
        )

    def fail_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_generation: int,
        error_type: str,
        idempotency_key: str | None = None,
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        clean_error = _bounded_token(error_type, "error_type", MAX_ERROR_TYPE_CHARACTERS)
        clean_owner = _bounded_token(owner, "owner", MAX_KIND_CHARACTERS)
        generation = _validate_generation(lease_generation)
        clock = coerce_utc_clock(now)
        stamp = canonical_utc(clock)

        def updates(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
            return {"error_type": clean_error, "lease_until": 0, "terminal_at": stamp}

        return self._transition_job(
            job_id, {"running", "checkpointed", "verifying"}, "failed", stamp, updates,
            idempotency_key=idempotency_key,
            lease_fence=(clean_owner, generation, clock.timestamp(), True),
        )

    def retry_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_generation: int,
        idempotency_key: str | None = None,
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        clean_owner = _bounded_token(owner, "owner", MAX_KIND_CHARACTERS)
        generation = _validate_generation(lease_generation)
        clock = coerce_utc_clock(now)
        stamp = canonical_utc(clock)

        def updates(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
            if int(row["attempts"]) >= int(row["max_attempts"]):
                raise LifecycleError("retry budget exhausted")
            return {
                "owner": "",
                "lease_until": 0,
                "heartbeat_at": "",
                "heartbeat_epoch": 0,
                "error_type": "",
                "verification_evidence": "",
                "terminal_at": "",
            }

        return self._transition_job(
            job_id, {"failed", "stalled"}, "queued", stamp, updates,
            idempotency_key=idempotency_key,
            lease_fence=(clean_owner, generation, clock.timestamp(), False),
        )

    def cancel_job(
        self,
        job_id: str,
        *,
        owner: str | None = None,
        lease_generation: int | None = None,
        idempotency_key: str | None = None,
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        clean_owner = (
            _bounded_token(owner, "owner", MAX_KIND_CHARACTERS) if owner is not None else None
        )
        generation = (
            _validate_generation(lease_generation) if lease_generation is not None else None
        )
        clock = coerce_utc_clock(now)
        stamp = canonical_utc(clock)

        def updates(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
            if row["state"] in JOB_ACTIVE_LEASE_STATES:
                if clean_owner is None or generation is None:
                    raise LifecycleError(
                        "cancelling an active job requires owner and lease_generation"
                    )
                _require_lease_fence(row, clean_owner, generation, clock.timestamp())
            return {"owner": "", "lease_until": 0, "terminal_at": stamp}

        return self._transition_job(
            job_id,
            {"queued", "running", "checkpointed", "verifying", "failed", "stalled"},
            "cancelled",
            stamp,
            updates,
            idempotency_key=idempotency_key,
        )

    def detect_stale_jobs(
        self,
        *,
        now: str | dt.datetime | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        clock = coerce_utc_clock(now)
        stamp = canonical_utc(clock)
        with self.connect() as conn:
            if not dry_run:
                conn.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in JOB_ACTIVE_LEASE_STATES)
            rows = conn.execute(
                f"""
                SELECT job_id FROM lifecycle_jobs
                WHERE state IN ({placeholders}) AND lease_until>0 AND lease_until<=?
                ORDER BY job_id
                """,
                (*sorted(JOB_ACTIVE_LEASE_STATES), clock.timestamp()),
            ).fetchall()
            stalled = [row["job_id"] for row in rows]
            if stalled and not dry_run:
                conn.executemany(
                    f"""
                    UPDATE lifecycle_jobs
                    SET state='stalled',lease_until=0,error_type='lease_expired',updated_at=?
                    WHERE job_id=? AND state IN ({placeholders}) AND lease_until>0 AND lease_until<=?
                    """,
                    [(stamp, job_id, *sorted(JOB_ACTIVE_LEASE_STATES), clock.timestamp()) for job_id in stalled],
                )
        return {"operation": "detect_stale_jobs", "dry_run": dry_run, "stalled": stalled}

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM lifecycle_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise LifecycleError("unknown job")
        return _job_result(dict(row), created=False)

    def snapshot(self) -> dict[str, Any]:
        with self.connect() as conn:
            intents = {row[0]: row[1] for row in conn.execute(
                "SELECT state, COUNT(*) FROM lifecycle_intents GROUP BY state"
            )}
            jobs = {row[0]: row[1] for row in conn.execute(
                "SELECT state, COUNT(*) FROM lifecycle_jobs GROUP BY state"
            )}
        return {
            "intents": {state: int(intents.get(state, 0)) for state in INTENT_STATES},
            "jobs": {state: int(jobs.get(state, 0)) for state in JOB_STATES},
        }

    def _transition_job(
        self,
        job_id: str,
        from_states: Iterable[str],
        to_state: str,
        stamp: str,
        build_updates,
        *,
        idempotency_key: str | None,
        lease_fence: tuple[str, int, float, bool] | None = None,
    ) -> dict[str, Any]:
        allowed = frozenset(from_states)
        key = validate_idempotency_key(idempotency_key) if idempotency_key is not None else None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._require_job(conn, job_id)
            if lease_fence is not None:
                owner, generation, now_epoch, require_fresh = lease_fence
                _require_lease_fence(
                    row, owner, generation, now_epoch, require_fresh=require_fresh
                )
            if key is not None and _replayed(conn, "job", job_id, key):
                return _job_result(dict(self._require_job(conn, job_id)), created=False)
            current = row["state"]
            if current not in allowed:
                raise InvalidTransition(f"job cannot move {current} -> {to_state}")
            updates = dict(build_updates(conn, row))
            updates["state"] = to_state
            updates["updated_at"] = stamp
            columns = ",".join(f"{name}=?" for name in updates)
            conn.execute(
                f"UPDATE lifecycle_jobs SET {columns} WHERE job_id=?",
                (*updates.values(), job_id),
            )
            _record_transition(conn, "job", job_id, key, current, to_state, stamp)
            updated = conn.execute("SELECT * FROM lifecycle_jobs WHERE job_id=?", (job_id,)).fetchone()
        return _job_result(dict(updated), created=False)

    @staticmethod
    def _require_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM lifecycle_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise LifecycleError("unknown job")
        return row


def _intent_result(row: dict[str, Any], *, created: bool) -> dict[str, Any]:
    return {
        "intent_id": row["intent_id"],
        "state": row["state"],
        "source": row["source"],
        "provenance_hash": row.get("provenance_hash", ""),
        "summary": row["summary"],
        "requires_approval": bool(row["requires_approval"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "terminal_at": row["terminal_at"],
        "active": row["state"] not in INTENT_TERMINAL,
        "created": created,
    }


def _job_result(row: dict[str, Any], *, created: bool) -> dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "intent_id": row["intent_id"] or "",
        "job_kind": row["job_kind"],
        "state": row["state"],
        "attempts": int(row["attempts"]),
        "max_attempts": int(row["max_attempts"]),
        "lease_until": float(row["lease_until"]),
        "lease_generation": int(row.get("lease_generation", 0)),
        "heartbeat_at": row["heartbeat_at"],
        "checkpoint": row["checkpoint"],
        "verification_evidence": row["verification_evidence"],
        "error_type": row["error_type"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "terminal_at": row["terminal_at"],
        "active": row["state"] not in JOB_TERMINAL,
        "created": created,
    }


def _replayed(conn: sqlite3.Connection, entity_type: str, entity_id: str, key: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM lifecycle_transitions WHERE entity_type=? AND entity_id=? AND idempotency_key=?",
        (entity_type, entity_id, key),
    ).fetchone() is not None


def _record_transition(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    key: str | None,
    from_state: str,
    to_state: str,
    stamp: str,
) -> None:
    if key is None:
        return
    transition_id = "trn_" + hashlib.sha256(
        f"{entity_type}\0{entity_id}\0{key}".encode("utf-8")
    ).hexdigest()[:32]
    conn.execute(
        """
        INSERT INTO lifecycle_transitions(
          transition_id,entity_type,entity_id,idempotency_key,from_state,to_state,created_at
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (transition_id, entity_type, entity_id, key, from_state, to_state, stamp),
    )


def _validate_lease(lease_seconds: float) -> float:
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)):
        raise LifecycleError("lease_seconds must be a number")
    value = float(lease_seconds)
    if not 1.0 <= value <= 86_400.0:
        raise LifecycleError("lease_seconds must be between 1 and 86400")
    return value


def _validate_generation(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise LifecycleError("lease_generation must be a positive integer")
    return value


def _require_lease_fence(
    row: sqlite3.Row,
    owner: str,
    generation: int,
    now_epoch: float,
    *,
    require_fresh: bool = True,
) -> None:
    if row["owner"] != owner:
        raise LifecycleError("job lease owner is stale")
    if int(row["lease_generation"]) != generation:
        raise LifecycleError("job lease generation is stale")
    if require_fresh and float(row["lease_until"]) <= now_epoch:
        raise LifecycleError("job lease has expired")


def _bounded_token(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise LifecycleError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized:
        raise LifecycleError(f"{label} must not be empty")
    if len(normalized) > maximum:
        raise LifecycleError(f"{label} exceeds {maximum} characters")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@+-]*", normalized):
        raise LifecycleError(f"{label} must be a bounded metadata token")
    return normalized


def _bounded_redacted(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise LifecycleError(f"{label} must be a string")
    redacted = " ".join(redact_sensitive_text(value).split())
    if len(redacted) > maximum:
        raise LifecycleError(f"{label} exceeds {maximum} characters")
    return redacted
