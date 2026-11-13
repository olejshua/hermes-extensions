"""Private SQLite state for the Dreaming subsystem.

This store is the only durable memory the sweep keeps. It holds runs, rounds,
per-phase outcomes, source checkpoints, deduplicated candidates with their
evidence, promotion decisions, and report-delivery claims.

Two invariants matter most:

* No unbounded raw content. Evidence rows carry a redacted snippet clipped to
  the configured budget plus a hash of the original; the full source text is
  never copied here.
* Source sessions are only marked complete once the phase that consumed them
  actually succeeded, so a model failure leaves the sweep resumable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at REAL NOT NULL,
  finished_at REAL,
  status TEXT NOT NULL,
  mode TEXT NOT NULL,
  rounds INTEGER NOT NULL DEFAULT 0,
  deadline REAL,
  error TEXT,
  summary_json TEXT
);
CREATE TABLE IF NOT EXISTS rounds (
  round_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  status TEXT NOT NULL,
  sessions INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS phases (
  phase_id TEXT PRIMARY KEY,
  round_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  phase TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  items INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS sources (
  source_key TEXT PRIMARY KEY,
  profile TEXT NOT NULL,
  session_id TEXT NOT NULL,
  session_source TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  day TEXT NOT NULL,
  status TEXT NOT NULL,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  completed_at REAL,
  completed_run_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY,
  claim_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  claim TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0,
  durability TEXT NOT NULL DEFAULT 'ephemeral',
  actionability TEXT NOT NULL DEFAULT 'none',
  reinforcement INTEGER NOT NULL DEFAULT 1,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  first_run_id TEXT NOT NULL,
  last_run_id TEXT NOT NULL,
  score REAL,
  classification TEXT,
  explain_json TEXT,
  tags_json TEXT NOT NULL DEFAULT '[]',
  identity_json TEXT NOT NULL DEFAULT '{}',
  identity_hash TEXT NOT NULL DEFAULT '',
  identity_version INTEGER NOT NULL DEFAULT 0,
  publication_state TEXT NOT NULL DEFAULT 'not_requested',
  synced_at REAL,
  staging_state TEXT NOT NULL DEFAULT 'not_applicable'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_claim_key ON candidates(claim_key);
CREATE TABLE IF NOT EXISTS candidate_claims (
  claim_variant_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL,
  claim TEXT NOT NULL,
  claim_hash TEXT NOT NULL,
  run_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(candidate_id,claim_hash)
);
CREATE INDEX IF NOT EXISTS idx_candidate_claims_candidate
  ON candidate_claims(candidate_id,created_at,claim_variant_id);
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL,
  ref TEXT NOT NULL,
  profile TEXT NOT NULL,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  day TEXT NOT NULL,
  snippet TEXT NOT NULL,
  snippet_hash TEXT NOT NULL,
  created_at REAL NOT NULL,
  run_id TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_unique ON evidence(candidate_id, ref, snippet_hash);
CREATE INDEX IF NOT EXISTS idx_evidence_candidate ON evidence(candidate_id);
CREATE TABLE IF NOT EXISTS insights (
  insight_id TEXT PRIMARY KEY,
  claim_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  claim TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  run_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  candidate_ids_json TEXT NOT NULL DEFAULT '[]',
  contradiction_json TEXT NOT NULL DEFAULT '[]',
  deep_supported INTEGER NOT NULL DEFAULT 0,
  deep_verdict TEXT,
  deep_rationale TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_insights_claim_key ON insights(claim_key);
CREATE TABLE IF NOT EXISTS promotions (
  promotion_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  target TEXT NOT NULL,
  score REAL NOT NULL,
  rationale TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  artifact_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_promotions_run ON promotions(run_id);
CREATE TABLE IF NOT EXISTS retrievals (
  retrieval_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  query TEXT NOT NULL,
  namespace TEXT NOT NULL,
  status TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
  report_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  path TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  created_at REAL NOT NULL,
  claimed_at REAL,
  claim_owner TEXT NOT NULL DEFAULT '',
  delivered_at REAL,
  delivery_snapshot TEXT NOT NULL DEFAULT '',
  delivery_day TEXT NOT NULL DEFAULT '',
  superseded_at REAL,
  superseded_by TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_fingerprint ON reports(fingerprint);
CREATE TABLE IF NOT EXISTS leases (
  name TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  acquired_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  pid INTEGER NOT NULL,
  run_id TEXT,
  generation INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS publications (
  publication_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL,
  staged_import_path TEXT NOT NULL,
  final_import_path TEXT NOT NULL,
  staged_report_path TEXT NOT NULL,
  final_report_path TEXT NOT NULL,
  dreams_path TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  report_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  lease_name TEXT NOT NULL,
  lease_owner TEXT NOT NULL,
  lease_generation INTEGER NOT NULL,
  created_at REAL NOT NULL,
  published_at REAL,
  synced_at REAL,
  sync_evidence_hash TEXT NOT NULL DEFAULT '',
  sync_source_id TEXT NOT NULL DEFAULT '',
  sync_remote_resource_id TEXT NOT NULL DEFAULT '',
  sync_receipt TEXT NOT NULL DEFAULT '',
  error TEXT
);
CREATE TABLE IF NOT EXISTS staging_outbox (
  outbox_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  target TEXT NOT NULL CHECK(target IN ('intent','improvement')),
  payload_json TEXT NOT NULL,
  provenance_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','staged')),
  attempts INTEGER NOT NULL DEFAULT 0,
  error_class TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  staged_at REAL,
  UNIQUE(candidate_id,target)
);
CREATE INDEX IF NOT EXISTS idx_rounds_run ON rounds(run_id);
CREATE INDEX IF NOT EXISTS idx_phases_run ON phases(run_id);
CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
"""

SCHEMA_VERSION = "9"

# Additive-only migrations. Each entry is (table, column, DDL type clause).
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("evidence", "provenance_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("candidates", "tags_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("candidates", "explain_json", "TEXT"),
    ("candidates", "classification", "TEXT"),
    ("candidates", "identity_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("candidates", "identity_hash", "TEXT NOT NULL DEFAULT ''"),
    ("candidates", "identity_version", "INTEGER NOT NULL DEFAULT 0"),
    ("candidates", "publication_state", "TEXT NOT NULL DEFAULT 'not_requested'"),
    ("candidates", "synced_at", "REAL"),
    ("candidates", "staging_state", "TEXT NOT NULL DEFAULT 'not_applicable'"),
    ("promotions", "artifact_path", "TEXT"),
    ("runs", "summary_json", "TEXT"),
    ("runs", "deadline", "REAL"),
    ("sources", "completed_run_id", "TEXT"),
    ("leases", "generation", "INTEGER NOT NULL DEFAULT 1"),
    ("retrievals", "query_hash", "TEXT NOT NULL DEFAULT ''"),
    ("retrievals", "query_length", "INTEGER NOT NULL DEFAULT 0"),
    ("insights", "contradiction_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("insights", "deep_supported", "INTEGER NOT NULL DEFAULT 0"),
    ("insights", "deep_verdict", "TEXT"),
    ("insights", "deep_rationale", "TEXT NOT NULL DEFAULT ''"),
    ("insights", "reflection_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("reports", "reflection_attempt_json", "TEXT NOT NULL DEFAULT ''"),
    ("reports", "delivery_state", "TEXT NOT NULL DEFAULT ''"),
    ("reports", "report_attempt_json", "TEXT NOT NULL DEFAULT ''"),
    ("publications", "indexable", "INTEGER NOT NULL DEFAULT 0"),
    ("publications", "synced_at", "REAL"),
    ("publications", "sync_evidence_hash", "TEXT NOT NULL DEFAULT ''"),
    ("publications", "sync_source_id", "TEXT NOT NULL DEFAULT ''"),
    ("publications", "sync_remote_resource_id", "TEXT NOT NULL DEFAULT ''"),
    ("publications", "sync_receipt", "TEXT NOT NULL DEFAULT ''"),
    ("reports", "delivery_snapshot", "TEXT NOT NULL DEFAULT ''"),
    ("reports", "delivery_day", "TEXT NOT NULL DEFAULT ''"),
    ("reports", "superseded_at", "REAL"),
    ("reports", "superseded_by", "TEXT NOT NULL DEFAULT ''"),
)

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)
_HASH_RE = re.compile(r"[0-9a-f]{64}")

_NEGATION_TOKENS = frozenset(
    {
        "not", "no", "never", "neither", "nor", "without",
        "nicht", "nie", "kein", "keine", "keinen", "keinem", "keiner", "keines", "ohne",
    }
)

_LEADING_ARTICLES = frozenset(
    {"a", "an", "the", "his", "her", "their", "der", "die", "das", "ein", "eine"}
)
_PREDICATE_ALIASES = {
    "use": "use",
    "uses": "use",
    "using": "use",
    "used": "use",
    "utilize": "use",
    "utilizes": "use",
    "utilized": "use",
    "utilizing": "use",
    "prefer": "prefer",
    "prefers": "prefer",
    "preferred": "prefer",
    "manage": "manage",
    "manages": "manage",
    "managed": "manage",
    "own": "own",
    "owns": "own",
    "owned": "own",
}


@dataclass(frozen=True)
class ClaimIdentity:
    """Typed semantic identity supplied with an exact candidate claim.

    The tuple is deliberately relational and polarity/scope aware. It remains
    an untrusted model proposal until the store validates every component
    against the exact ordered claim text.
    """

    subject: str
    predicate: str
    object: str
    polarity: str
    scope: str

    def canonical(self) -> dict[str, str]:
        polarity = str(self.polarity).strip().lower()
        if polarity not in {"positive", "negative"}:
            raise ValueError("claim identity polarity must be positive or negative")
        return {
            "subject": _canonical_component(self.subject, "subject", 120),
            "predicate": _canonical_predicate(self.predicate),
            "object": _canonical_component(self.object, "object", 180),
            "polarity": polarity,
            "scope": _canonical_component(self.scope, "scope", 120),
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "ClaimIdentity":
        if not isinstance(value, dict) or set(value) != {
            "subject", "predicate", "object", "polarity", "scope"
        }:
            raise ValueError("claim_identity must contain exactly subject/predicate/object/polarity/scope")
        return cls(
            subject=value["subject"],
            predicate=value["predicate"],
            object=value["object"],
            polarity=value["polarity"],
            scope=value["scope"],
        )


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str
    claim_key: str
    kind: str
    claim: str
    detail: str
    status: str
    confidence: float
    durability: str
    actionability: str
    reinforcement: int
    first_seen: float
    last_seen: float
    score: float | None
    classification: str | None
    tags: tuple[str, ...]
    identity_hash: str = ""
    identity_version: int = 0
    publication_state: str = "not_requested"
    synced: bool = False
    staging_state: str = "not_applicable"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "CandidateRecord":
        return cls(
            candidate_id=row["candidate_id"],
            claim_key=row["claim_key"],
            kind=row["kind"],
            claim=row["claim"],
            detail=row["detail"],
            status=row["status"],
            confidence=float(row["confidence"]),
            durability=row["durability"],
            actionability=row["actionability"],
            reinforcement=int(row["reinforcement"]),
            first_seen=float(row["first_seen"]),
            last_seen=float(row["last_seen"]),
            score=None if row["score"] is None else float(row["score"]),
            classification=row["classification"],
            tags=tuple(json.loads(row["tags_json"] or "[]")),
            identity_hash=str(row["identity_hash"] or ""),
            identity_version=int(row["identity_version"] or 0),
            publication_state=str(row["publication_state"] or "not_requested"),
            synced=row["synced_at"] is not None,
            staging_state=str(row["staging_state"] or "not_applicable"),
        )


@dataclass(frozen=True)
class EvidenceRecord:
    ref: str
    profile: str
    session_id: str
    role: str
    day: str
    snippet: str
    snippet_hash: str
    provenance: dict[str, Any] = field(default_factory=dict)


class LeaseHeld(RuntimeError):
    """Raised when another live worker already holds the sweep lease."""


class DreamStore:
    """The private Dream state database."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 10_000):
        self.path = Path(path).expanduser()
        self.busy_timeout_ms = busy_timeout_ms
        _reject_symlink_sqlite_paths(self.path)
        _reject_unsafe_ancestors(self.path.parent)
        parent = self.path.parent
        existed = parent.exists()
        parent.mkdir(parents=True, exist_ok=True)
        if not existed:
            _chmod_if_owned(parent, 0o700)
        self._init()
        _chmod_if_owned(self.path, 0o600)
        _chmod_sidecars(self.path)

    # ------------------------------------------------------------------ core

    def connect(self) -> sqlite3.Connection:
        """Return the raw configured SQLite connection (public compatibility API)."""

        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        _chmod_sidecars(self.path)
        return conn

    @contextmanager
    def closing_connection(self) -> Iterator[sqlite3.Connection]:
        """Commit/rollback and always close an internally owned connection."""

        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init(self) -> None:
        with self.closing_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES('version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        for table, column, ddl in _MIGRATIONS:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not columns:
                continue
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        # Delivery truth: rows marked delivered before receipts existed (stdout hand-off) are
        # legacy_unverified, never confirmed. delivered_at stays so legacy consumers and dedupe keep working.
        conn.execute("UPDATE reports SET delivery_state='legacy_unverified' WHERE delivered_at IS NOT NULL "
                     "AND delivery_state='' AND reflection_attempt_json='' AND report_attempt_json=''")
        conn.execute("UPDATE reports SET delivery_state='confirmed' WHERE delivered_at IS NOT NULL AND delivery_state='' "
                     "AND reflection_attempt_json LIKE '%\"state\": \"delivered\"%'")
        # Version 5 names the local filesystem outcome honestly.  It is not an
        # acknowledgement from OpenViking and therefore cannot mean synced.
        conn.execute(
            "UPDATE publications SET status='locally_published' WHERE status='published'"
        )
        conn.execute(
            "UPDATE candidates SET status='classified' WHERE status='promoted'"
        )
        # Pre-receipt acknowledgements were derived only from Dream's own hash
        # and are not evidence of a real OpenViking resource. Requeue them.
        invalid_sync_runs = [
            str(row["run_id"])
            for row in conn.execute(
                "SELECT run_id FROM publications WHERE synced_at IS NOT NULL AND "
                "(sync_source_id='' OR sync_remote_resource_id='' OR sync_receipt='')"
            ).fetchall()
        ]
        if invalid_sync_runs:
            placeholders = ",".join("?" for _ in invalid_sync_runs)
            conn.execute(
                f"UPDATE candidates SET publication_state='locally_published',synced_at=NULL "
                f"WHERE candidate_id IN (SELECT candidate_id FROM promotions WHERE "
                f"target='openviking_dream' AND run_id IN ({placeholders}))",
                invalid_sync_runs,
            )
            conn.execute(
                f"UPDATE publications SET synced_at=NULL,sync_evidence_hash='',sync_source_id='',"
                f"sync_remote_resource_id='',sync_receipt='' WHERE run_id IN ({placeholders})",
                invalid_sync_runs,
            )
        # Preserve the exact claim already held by pre-v5 candidates as the
        # first provenance variant. The typed identity columns remain empty for
        # legacy rows, so this migration never guesses or merges identities.
        for row in conn.execute(
            "SELECT candidate_id,claim,first_run_id,first_seen FROM candidates "
            "WHERE claim<>'' AND NOT EXISTS (SELECT 1 FROM candidate_claims "
            "WHERE candidate_claims.candidate_id=candidates.candidate_id)"
        ).fetchall():
            exact_claim = str(row["claim"])
            exact_hash = hashlib.sha256(exact_claim.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO candidate_claims("
                "claim_variant_id,candidate_id,claim,claim_hash,run_id,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    deterministic_id("clm", str(row["candidate_id"]), exact_hash),
                    row["candidate_id"],
                    exact_claim,
                    exact_hash,
                    str(row["first_run_id"] or "legacy"),
                    float(row["first_seen"] or 0),
                ),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.closing_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    # ----------------------------------------------------------------- leases

    def acquire_lease(
        self,
        name: str,
        *,
        lease_seconds: float,
        owner: str | None = None,
        run_id: str | None = None,
        now: float | None = None,
    ) -> str:
        """Take a lease, never stealing it from a live PID on this host."""

        stamp = time.time() if now is None else now
        lease_owner = owner or f"dream-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        with self.transaction() as conn:
            row = conn.execute("SELECT owner,expires_at,pid,generation FROM leases WHERE name=?", (name,)).fetchone()
            generation = 1
            if row is not None:
                alive = _process_alive(int(row["pid"]))
                if alive:
                    raise LeaseHeld(f"dreaming lease {name!r} held by {row['owner']}")
                generation = int(row["generation"] or 0) + 1
            conn.execute(
                "INSERT INTO leases(name,owner,acquired_at,expires_at,pid,run_id,generation) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, acquired_at=excluded.acquired_at, "
                "expires_at=excluded.expires_at, pid=excluded.pid, run_id=excluded.run_id, generation=excluded.generation",
                (name, lease_owner, stamp, stamp + lease_seconds, os.getpid(), run_id, generation),
            )
        return lease_owner

    def renew_lease(self, name: str, owner: str, *, lease_seconds: float, now: float | None = None) -> bool:
        stamp = time.time() if now is None else now
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE leases SET expires_at=? WHERE name=? AND owner=?",
                (stamp + lease_seconds, name, owner),
            )
            return cursor.rowcount == 1

    def release_lease(self, name: str, owner: str) -> bool:
        with self.transaction() as conn:
            # Retain the row so the generation is monotonic and an old owner
            # can never pass an ABA-style fence after release/reacquire.
            cursor = conn.execute(
                "UPDATE leases SET expires_at=0,pid=0,run_id=NULL WHERE name=? AND owner=?",
                (name, owner),
            )
            return cursor.rowcount == 1

    def lease_info(self, name: str) -> dict[str, Any] | None:
        with self.closing_connection() as conn:
            row = conn.execute("SELECT * FROM leases WHERE name=?", (name,)).fetchone()
            return dict(row) if row else None

    def assert_lease(self, name: str, owner: str, generation: int | None = None) -> bool:
        with self.closing_connection() as conn:
            row = conn.execute("SELECT owner,generation,pid FROM leases WHERE name=?", (name,)).fetchone()
        return bool(
            row
            and row["owner"] == owner
            and (generation is None or int(row["generation"]) == generation)
            and _process_alive(int(row["pid"]))
        )

    # ------------------------------------------------------------------- runs

    def start_run(self, *, mode: str, deadline: float | None, now: float | None = None) -> str:
        stamp = time.time() if now is None else now
        run_id = f"run_{uuid.uuid4().hex[:20]}"
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO runs(run_id,started_at,status,mode,deadline) VALUES(?,?,?,?,?)",
                (run_id, stamp, "running", mode, deadline),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        rounds: int,
        error: str | None = None,
        summary: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        stamp = time.time() if now is None else now
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, rounds=?, error=?, summary_json=? WHERE run_id=?",
                (stamp, status, rounds, error, json.dumps(summary or {}, sort_keys=True), run_id),
            )

    def start_round(self, run_id: str, ordinal: int, *, now: float | None = None) -> str:
        stamp = time.time() if now is None else now
        round_id = f"rnd_{uuid.uuid4().hex[:20]}"
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO rounds(round_id,run_id,ordinal,started_at,status) VALUES(?,?,?,?,?)",
                (round_id, run_id, ordinal, stamp, "running"),
            )
        return round_id

    def finish_round(
        self,
        round_id: str,
        *,
        status: str,
        sessions: int,
        error: str | None = None,
        now: float | None = None,
    ) -> None:
        stamp = time.time() if now is None else now
        with self.transaction() as conn:
            conn.execute(
                "UPDATE rounds SET finished_at=?, status=?, sessions=?, error=? WHERE round_id=?",
                (stamp, status, sessions, error, round_id),
            )

    def record_phase(
        self,
        *,
        run_id: str,
        round_id: str,
        phase: str,
        status: str,
        started_at: float,
        items: int = 0,
        error: str | None = None,
        now: float | None = None,
    ) -> str:
        stamp = time.time() if now is None else now
        phase_id = f"phs_{uuid.uuid4().hex[:20]}"
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO phases(phase_id,round_id,run_id,phase,status,started_at,finished_at,items,error) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (phase_id, round_id, run_id, phase, status, started_at, stamp, items, error),
            )
        return phase_id

    # ---------------------------------------------------------------- sources

    def seen_source_keys(self, fingerprints: dict[str, str]) -> set[str]:
        """Return keys already completed with an identical fingerprint.

        A changed fingerprint means the session grew since we last read it, so
        it becomes eligible again.
        """

        if not fingerprints:
            return set()
        with self.closing_connection() as conn:
            rows = conn.execute("SELECT source_key, fingerprint, status FROM sources").fetchall()
        done: set[str] = set()
        for row in rows:
            key = row["source_key"]
            if key not in fingerprints:
                continue
            if row["status"] == "complete" and row["fingerprint"] == fingerprints[key]:
                done.add(key)
        return done

    def register_sources(self, sources: Sequence[dict[str, Any]], *, now: float | None = None) -> int:
        stamp = time.time() if now is None else now
        if not sources:
            return 0
        with self.transaction() as conn:
            for source in sources:
                conn.execute(
                    """
                    INSERT INTO sources(source_key,profile,session_id,session_source,fingerprint,day,status,first_seen,last_seen)
                    VALUES(?,?,?,?,?,?,'pending',?,?)
                    ON CONFLICT(source_key) DO UPDATE SET
                      last_seen=excluded.last_seen,
                      session_source=excluded.session_source,
                      day=excluded.day,
                      status=CASE WHEN sources.fingerprint=excluded.fingerprint THEN sources.status ELSE 'pending' END,
                      fingerprint=excluded.fingerprint
                    """,
                    (
                        source["source_key"],
                        source["profile"],
                        source["session_id"],
                        source.get("session_source", "unknown"),
                        source["fingerprint"],
                        source.get("day", "unknown"),
                        stamp,
                        stamp,
                    ),
                )
        return len(sources)

    def complete_sources(self, keys: Iterable[str], run_id: str, *, now: float | None = None) -> int:
        stamp = time.time() if now is None else now
        keys = list(keys)
        if not keys:
            return 0
        with self.transaction() as conn:
            conn.executemany(
                "UPDATE sources SET status='complete', completed_at=?, completed_run_id=?, error=NULL WHERE source_key=?",
                [(stamp, run_id, key) for key in keys],
            )
        return len(keys)

    def fail_sources(self, keys: Iterable[str], error: str) -> int:
        keys = list(keys)
        if not keys:
            return 0
        with self.transaction() as conn:
            conn.executemany(
                "UPDATE sources SET status='pending', attempts=attempts+1, error=? WHERE source_key=?",
                [(error[:500], key) for key in keys],
            )
        return len(keys)

    def pending_source_count(self) -> int:
        with self.closing_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM sources WHERE status!='complete'").fetchone()
            return int(row["n"])

    # ------------------------------------------------------------- candidates

    def upsert_candidate(
        self,
        *,
        kind: str,
        claim: str,
        claim_identity: ClaimIdentity | dict[str, Any] | None = None,
        detail: str,
        confidence: float,
        durability: str,
        actionability: str,
        tags: Sequence[str],
        run_id: str,
        evidence: Sequence[EvidenceRecord],
        now: float | None = None,
    ) -> tuple[str, bool]:
        """Stage a candidate and its evidence for ``run_id``.

        Existing candidate metadata and reinforcement are deliberately left
        untouched until the fenced success transaction.  A failed run can
        therefore be rolled back without partially reinforcing durable state.
        """

        stamp = time.time() if now is None else now
        exact_claim = str(claim).strip()
        if not exact_claim or len(exact_claim) > 400:
            raise ValueError("candidate claim must contain 1..400 characters")
        proposed_identity = None
        if claim_identity is not None:
            proposed_identity = (
                claim_identity
                if isinstance(claim_identity, ClaimIdentity)
                else ClaimIdentity.from_mapping(claim_identity)
            )
        identity = validated_claim_identity(exact_claim, proposed_identity)
        if identity is None:
            claim_key = exact_claim_key(kind, exact_claim)
            identity_json = "{}"
            identity_hash = hashlib.sha256(claim_key.encode("utf-8")).hexdigest()
            identity_version = 0
        else:
            claim_key = canonical_claim_key(kind, identity)
            canonical_identity = identity.canonical()
            identity_json = json.dumps(
                canonical_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            identity_hash = hashlib.sha256(claim_key.encode("utf-8")).hexdigest()
            identity_version = 1
        candidate_id = deterministic_id("cand", claim_key)
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE claim_key=?", (claim_key,)).fetchone()
            created = row is None
            if row is None:
                legacy_rows = conn.execute(
                    "SELECT DISTINCT c.* FROM candidates c LEFT JOIN candidate_claims cc "
                    "ON cc.candidate_id=c.candidate_id "
                    "WHERE c.kind=? AND COALESCE(c.identity_version,0)=0 "
                    "AND (c.claim=? OR cc.claim=?) ORDER BY c.candidate_id",
                    (kind, exact_claim, exact_claim),
                ).fetchall()
                if len(legacy_rows) == 1:
                    row = legacy_rows[0]
                    candidate_id = str(row["candidate_id"])
                    publication_state, synced_at = self._legacy_publication_state(
                        conn, candidate_id, str(row["publication_state"] or "not_requested")
                    )
                    conn.execute(
                        "UPDATE candidates SET claim_key=?,identity_json=?,identity_hash=?,identity_version=?,"
                        "publication_state=?,synced_at=? WHERE candidate_id=? AND identity_version=0",
                        (
                            claim_key,
                            identity_json,
                            identity_hash,
                            identity_version,
                            publication_state,
                            synced_at,
                            candidate_id,
                        ),
                    )
                    row = conn.execute(
                        "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
                    ).fetchone()
                    created = False
            if created:
                conn.execute(
                    """
                    INSERT INTO candidates(candidate_id,claim_key,kind,claim,detail,status,confidence,durability,
                                           actionability,reinforcement,first_seen,last_seen,first_run_id,last_run_id,
                                           tags_json,identity_json,identity_hash,identity_version)
                    VALUES(?,?,?,?,?,'open',?,?,?,1,?,?,?,?,?,?,?,?)
                    """,
                    (
                        candidate_id,
                        claim_key,
                        kind,
                        claim,
                        detail,
                        float(confidence),
                        durability,
                        actionability,
                        stamp,
                        stamp,
                        run_id,
                        run_id,
                        json.dumps(sorted(set(tags)), sort_keys=True),
                        identity_json,
                        identity_hash,
                        identity_version,
                    ),
                )
            else:
                assert row is not None
                candidate_id = row["candidate_id"]
                if (
                    str(row["identity_hash"] or "") != identity_hash
                    or int(row["identity_version"] or 0) != identity_version
                ):
                    raise ValueError("candidate identity collision")

            exact_hash = hashlib.sha256(exact_claim.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO candidate_claims("
                "claim_variant_id,candidate_id,claim,claim_hash,run_id,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    deterministic_id("clm", candidate_id, exact_hash),
                    candidate_id,
                    exact_claim,
                    exact_hash,
                    run_id,
                    stamp,
                ),
            )

            for item in evidence:
                evidence_id = deterministic_id("ev", candidate_id, item.ref, item.snippet_hash)
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO evidence(evidence_id,candidate_id,ref,profile,session_id,role,day,
                                                   snippet,snippet_hash,created_at,run_id,provenance_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        evidence_id,
                        candidate_id,
                        item.ref,
                        item.profile,
                        item.session_id,
                        item.role,
                        item.day,
                        item.snippet,
                        item.snippet_hash,
                        stamp,
                        run_id,
                        json.dumps(item.provenance, sort_keys=True),
                    ),
                )
        return candidate_id, created

    @staticmethod
    def _legacy_publication_state(
        conn: sqlite3.Connection, candidate_id: str, current: str
    ) -> tuple[str, float | None]:
        row = conn.execute(
            "SELECT p.status,p.synced_at FROM publications p JOIN promotions pr ON pr.run_id=p.run_id "
            "WHERE pr.candidate_id=? AND pr.target='openviking_dream' "
            "ORDER BY COALESCE(p.synced_at,p.published_at,p.created_at) DESC,p.publication_id LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return current, None
        if row["synced_at"] is not None:
            return "synced", float(row["synced_at"])
        if row["status"] == "locally_published":
            return "locally_published", None
        if row["status"] == "pending":
            return "outbox_pending", None
        return current, None

    def claim_variants(self, candidate_id: str) -> list[str]:
        """Return exact local claim variants; never used by metadata status."""

        with self.closing_connection() as conn:
            return [
                str(row["claim"])
                for row in conn.execute(
                    "SELECT claim FROM candidate_claims WHERE candidate_id=? "
                    "ORDER BY created_at,claim_variant_id",
                    (candidate_id,),
                )
            ]

    def candidate_merge_metadata(self, candidate_id: str) -> dict[str, Any]:
        with self.closing_connection() as conn:
            row = conn.execute(
                "SELECT identity_hash,identity_version FROM candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown candidate")
            variants = int(
                conn.execute(
                    "SELECT COUNT(*) FROM candidate_claims WHERE candidate_id=?", (candidate_id,)
                ).fetchone()[0]
            )
        version = int(row["identity_version"] or 0)
        return {
            "identity_version": version,
            "identity_hash": str(row["identity_hash"] or ""),
            "claim_variants": variants,
            "merge_strategy": (
                "typed_subject_predicate_object_polarity_scope"
                if version == 1
                else "legacy_exact_ordered_tokens"
            ),
            "embedding_merge": False,
        }

    def candidate(self, candidate_id: str) -> CandidateRecord | None:
        with self.closing_connection() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            return CandidateRecord.from_row(row) if row else None

    def candidate_by_claim(self, claim: str) -> CandidateRecord | None:
        with self.closing_connection() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE claim_key=?", (normalize_claim(claim),)).fetchone()
            return CandidateRecord.from_row(row) if row else None

    def candidates(self, *, statuses: Sequence[str] | None = None, limit: int | None = None) -> list[CandidateRecord]:
        sql = "SELECT * FROM candidates"
        params: list[Any] = []
        if statuses:
            sql += f" WHERE status IN ({','.join('?' for _ in statuses)})"
            params.extend(statuses)
        sql += " ORDER BY last_seen DESC, candidate_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.closing_connection() as conn:
            return [CandidateRecord.from_row(row) for row in conn.execute(sql, params)]

    def evidence_for(self, candidate_id: str) -> list[EvidenceRecord]:
        with self.closing_connection() as conn:
            rows = conn.execute(
                "SELECT ref,profile,session_id,role,day,snippet,snippet_hash,provenance_json FROM evidence "
                "WHERE candidate_id=? ORDER BY day, ref",
                (candidate_id,),
            ).fetchall()
        return [
            EvidenceRecord(
                ref=row["ref"],
                profile=row["profile"],
                session_id=row["session_id"],
                role=row["role"],
                day=row["day"],
                snippet=row["snippet"],
                snippet_hash=row["snippet_hash"],
                provenance=json.loads(row["provenance_json"] or "{}"),
            )
            for row in rows
        ]

    def set_candidate_outcome(
        self,
        candidate_id: str,
        *,
        status: str,
        score: float,
        classification: str,
        explain: dict[str, Any],
    ) -> None:
        with self.transaction() as conn:
            merged = self._explain_with_merge(conn, candidate_id, explain)
            conn.execute(
                "UPDATE candidates SET status=?, score=?, classification=?, explain_json=? WHERE candidate_id=?",
                (status, float(score), classification, json.dumps(merged, sort_keys=True), candidate_id),
            )

    def candidate_explain(self, candidate_id: str) -> dict[str, Any] | None:
        with self.closing_connection() as conn:
            row = conn.execute(
                "SELECT explain_json FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
        if not row or not row["explain_json"]:
            return None
        return json.loads(row["explain_json"])

    def candidate_status_counts(self) -> dict[str, int]:
        with self.closing_connection() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM candidates GROUP BY status").fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    @staticmethod
    def _explain_with_merge(
        conn: sqlite3.Connection, candidate_id: str, explain: dict[str, Any]
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT identity_hash,identity_version FROM candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        variants = int(
            conn.execute(
                "SELECT COUNT(*) FROM candidate_claims WHERE candidate_id=?", (candidate_id,)
            ).fetchone()[0]
        )
        output = dict(explain)
        version = int(row["identity_version"] or 0) if row else 0
        output["merge"] = {
            "identity_version": version,
            "identity_hash": str(row["identity_hash"] or "") if row else "",
            "claim_variants": variants,
            "merge_strategy": (
                "typed_subject_predicate_object_polarity_scope"
                if version == 1
                else "legacy_exact_ordered_tokens"
            ),
            "embedding_merge": False,
        }
        return output

    # ---------------------------------------------------------------- insights

    def upsert_insight(
        self,
        *,
        kind: str,
        claim: str,
        detail: str,
        confidence: float,
        status: str,
        run_id: str,
        evidence: Sequence[dict[str, Any]],
        candidate_ids: Sequence[str],
        now: float | None = None,
    ) -> str:
        stamp = time.time() if now is None else now
        claim_key = normalize_claim(f"{kind}|{claim}")
        insight_id = deterministic_id("ins", claim_key)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO insights(insight_id,claim_key,kind,claim,detail,confidence,status,run_id,created_at,
                                     evidence_json,candidate_ids_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(claim_key) DO NOTHING
                """,
                (
                    insight_id,
                    claim_key,
                    kind,
                    claim,
                    detail,
                    float(confidence),
                    status,
                    run_id,
                    stamp,
                    json.dumps(list(evidence), sort_keys=True),
                    json.dumps(sorted(set(candidate_ids)), sort_keys=True),
                ),
            )
        return insight_id

    def insights(self, *, run_id: str | None = None, kinds: Sequence[str] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM insights"
        clauses: list[str] = []
        params: list[Any] = []
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        if kinds:
            clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY confidence DESC, insight_id"
        with self.closing_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
            item["candidate_ids"] = json.loads(item.pop("candidate_ids_json") or "[]")
            item["contradiction_sides"] = json.loads(item.pop("contradiction_json") or "[]")
            item["deep_supported"] = bool(item["deep_supported"])
            item["reflection"] = json.loads(item.pop("reflection_json") or "{}")
            result.append(item)
        return result

    def set_insight_outcome(self, insight_id: str, *, status: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE insights SET status=? WHERE insight_id=?", (status, insight_id))

    # ------------------------------------------------------------- promotions

    def record_promotion(
        self,
        *,
        candidate_id: str,
        run_id: str,
        target: str,
        score: float,
        rationale: str,
        artifact_path: str | None = None,
        now: float | None = None,
    ) -> str:
        stamp = time.time() if now is None else now
        promotion_id = deterministic_id("prm", run_id, candidate_id, target)
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO promotions(promotion_id,candidate_id,run_id,target,score,rationale,created_at,artifact_path) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (promotion_id, candidate_id, run_id, target, float(score), rationale[:1000], stamp, artifact_path),
            )
        return promotion_id

    def promotions(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM promotions"
        params: list[Any] = []
        if run_id:
            sql += " WHERE run_id=?"
            params.append(run_id)
        sql += " ORDER BY score DESC, promotion_id"
        with self.closing_connection() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def record_retrieval(
        self,
        *,
        run_id: str,
        query_hash: str,
        query_length: int,
        namespace: str,
        status: str,
        hits: int,
        error: str | None = None,
        now: float | None = None,
    ) -> None:
        stamp = time.time() if now is None else now
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO retrievals(retrieval_id,run_id,query,namespace,status,hits,error,created_at,query_hash,query_length) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    f"ret_{uuid.uuid4().hex[:20]}",
                    run_id,
                    "",
                    namespace,
                    status,
                    hits,
                    (error or "")[:500] or None,
                    stamp,
                    query_hash[:64],
                    max(0, int(query_length)),
                ),
            )

    # ---------------------------------------------------------------- reports

    def record_report(self, *, run_id: str, path: Path, fingerprint: str, now: float | None = None) -> str:
        stamp = time.time() if now is None else now
        report_id = deterministic_id("rpt", fingerprint)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO reports(report_id,run_id,path,fingerprint,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET path=excluded.path, run_id=excluded.run_id",
                (report_id, run_id, str(path), fingerprint, stamp),
            )
        return report_id

    def claim_report(
        self,
        *,
        owner: str | None = None,
        now: float | None = None,
        claim_seconds: float = 300.0,
        day_start: float | None = None,
        day_end: float | None = None,
        delivery_day: str = "",
        once_per_day: bool = False,
        eligible_run_ids: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        """Lease one report; optionally supersede older triggers for its local day."""

        stamp = time.time() if now is None else now
        claim_owner = owner or f"claim-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        if (day_start is None) != (day_end is None):
            raise ValueError("day_start and day_end must be supplied together")
        if day_start is not None and float(day_end) <= float(day_start):
            raise ValueError("report delivery day bounds are invalid")
        day_clause = ""
        day_params: tuple[Any, ...] = ()
        if day_start is not None:
            day_clause = " AND created_at>=? AND created_at<?"
            day_params = (float(day_start), float(day_end))
        if eligible_run_ids is not None:
            day_clause += f" AND run_id IN ({','.join('?' for _ in eligible_run_ids) or 'NULL'})"
            day_params += tuple(eligible_run_ids)
        with self.transaction() as conn:
            if once_per_day:
                if not delivery_day or day_start is None:
                    raise ValueError("once-per-day report claim requires a local day")
                if conn.execute(
                    "SELECT 1 FROM reports WHERE delivery_day=? AND "
                    "(delivered_at IS NOT NULL OR reflection_attempt_json<>'' OR (claimed_at IS NOT NULL AND superseded_at IS NULL)) LIMIT 1",
                    (delivery_day,),
                ).fetchone():
                    return None
                # An uncertain send is never retried after a mere timeout.
                # Only an explicit release after native proof of no send can
                # reacquire this same frozen snapshot, never a newer report.
                pending = conn.execute(
                    "SELECT report_id FROM reports WHERE delivery_day=? AND "
                    "delivered_at IS NULL AND superseded_at IS NULL AND delivery_snapshot<>'' "
                    "ORDER BY created_at,report_id LIMIT 1", (delivery_day,),
                ).fetchone()
                if pending is not None:
                    day_clause = " AND report_id=?"
                    day_params = (pending["report_id"],)
            row = conn.execute(
                "SELECT * FROM reports WHERE delivered_at IS NULL AND "
                "superseded_at IS NULL AND (claimed_at IS NULL OR claimed_at<=?)"
                + day_clause
                + " ORDER BY created_at DESC,report_id DESC LIMIT 1",
                (stamp - max(1.0, claim_seconds), *day_params),
            ).fetchone()
            if row is None:
                return None
            if day_start is not None:
                conn.execute(
                    "UPDATE reports SET superseded_at=?,superseded_by=? "
                    "WHERE report_id<>? AND delivered_at IS NULL AND superseded_at IS NULL "
                    "AND created_at>=? AND created_at<?",
                    (
                        stamp,
                        str(row["report_id"]),
                        str(row["report_id"]),
                        float(day_start),
                        float(day_end),
                    ),
                )
            cursor = conn.execute(
                "UPDATE reports SET claimed_at=?,claim_owner=?,delivery_day=CASE "
                "WHEN delivery_day='' THEN ? ELSE delivery_day END "
                "WHERE report_id=? AND delivered_at IS NULL AND superseded_at IS NULL "
                "AND (claimed_at IS NULL OR claimed_at<=?)",
                (
                    stamp,
                    claim_owner,
                    delivery_day,
                    row["report_id"],
                    stamp - max(1.0, claim_seconds),
                ),
            )
            if cursor.rowcount != 1:
                return None
            claimed = dict(row)
            claimed["claim_owner"] = claim_owner
            claimed["claimed_at"] = stamp
            return claimed

    def reflection_review_history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Bounded prior conclusions/attempts for Deep's novelty comparison."""
        result: list[dict[str, Any]] = []
        outbox_order: list[str] = []
        with self.closing_connection() as conn:
            for row in conn.execute(
                "SELECT i.* FROM insights i JOIN runs r ON i.run_id=r.run_id "
                "WHERE r.status='complete' ORDER BY i.created_at DESC LIMIT ?", (max(1, min(40, limit)),)
            ):
                meta = json.loads(row["reflection_json"] or "{}")
                review = meta.get("quality_review") or {}
                result.append({"item_id": row["insight_id"], "claim": row["claim"][:180],
                               "proposed_result": str(meta.get("proposed_result", ""))[:240],
                               "novelty_key": review.get("novelty_key", ""),
                               "status": row["status"], "decision": review.get("decision", "unknown")})
            for row in conn.execute(
                "SELECT delivery_snapshot,reflection_attempt_json,delivered_at FROM reports "
                "WHERE delivery_snapshot<>'' ORDER BY created_at DESC LIMIT ?", (max(1, min(40, limit)),)
            ):
                try:
                    snapshot = json.loads(row["delivery_snapshot"])
                except (ValueError, TypeError):
                    continue  # Legacy non-reflection reports are not idea history.
                attempt = json.loads(row["reflection_attempt_json"] or "{}")
                delivery_state = str(attempt.get("state") or "reserved")
                if row["delivered_at"] is not None and not attempt:
                    delivery_state = "legacy_ack_without_native_receipt"
                for item in _reflection_snapshot_items(snapshot):
                    outbox_order.append(str(item["item_id"]))
                    prior = next((x for x in result if x["item_id"] == item["item_id"]), None)
                    evidence = {"delivery_state": delivery_state,
                                "attempt_already_consumed": bool(attempt),
                                "evidence_scope": "outbox_status_not_user_rule_or_permission"}
                    if prior is not None:
                        prior.update(evidence)
                    else:
                        result.append({"item_id": item["item_id"], "novelty_key": item.get("novelty_key", ""),
                                       "proposed_result": str(item.get("text", ""))[:240],
                                       "status": "reserved_or_attempted", "decision": "do_not_repeat", **evidence})
        by_id = {item["item_id"]: item for item in result}
        # A long sequence of withheld ideas must not hide the actual prior
        # reservations/attempts from the next bounded semantic comparison.
        order = list(dict.fromkeys([*outbox_order, *by_id]))
        return [by_id[item_id] for item_id in order[:max(1, min(40, limit))]]

    def claim_reflection_bundle(self, *, owner: str, items: Sequence[dict[str, Any]],
                                now: float, delivery_day: str, limit: int) -> dict[str, Any] | None:
        """Reserve/freeze several ideas atomically in the existing report outbox.

        No day-wide exclusion, report supersession, new table or transport queue.
        Any frozen item is reserved; any attempted item is permanently consumed.
        Only an explicitly released, provably-unsent claim can reuse its snapshot.
        """
        if not items:
            return None
        eligible = {str(item["item_id"]): dict(item) for item in items}
        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM reports ORDER BY created_at DESC,report_id DESC").fetchall()
            seen_ids: set[str] = set()
            seen_keys: set[str] = set()
            for row in rows:
                try:
                    snapshot = json.loads(row["delivery_snapshot"] or "{}")
                except (ValueError, TypeError):
                    if row["reflection_attempt_json"]:
                        raise ValueError("unreadable reflection attempt snapshot")
                    continue
                frozen = _reflection_snapshot_items(snapshot)
                seen_ids.update(str(i["item_id"]) for i in frozen)
                seen_keys.update(str(i["novelty_key"]) for i in frozen if i.get("novelty_key"))
                if (frozen and row["claimed_at"] is None and row["delivered_at"] is None
                        and row["superseded_at"] is None and not row["reflection_attempt_json"]
                        and all(i["item_id"] in eligible and
                                i["item_sha256"] == eligible[i["item_id"]]["item_sha256"] for i in frozen)):
                    conn.execute("UPDATE reports SET claimed_at=?,claim_owner=? WHERE report_id=?",
                                 (now, owner, row["report_id"]))
                    return {**snapshot, "claim_owner": owner}
            chosen: list[dict[str, Any]] = []
            for item in items:
                if item["item_id"] in seen_ids or item["novelty_key"] in seen_keys:
                    continue
                chosen.append(dict(item))
                seen_ids.add(item["item_id"])
                seen_keys.add(item["novelty_key"])
                if len(chosen) >= max(1, min(20, limit)):
                    break
            if not chosen:
                return None
            runs = {i["run_id"] for i in chosen}
            anchor = next((r for r in rows if r["run_id"] in runs and r["delivered_at"] is None
                           and r["claimed_at"] is None and r["superseded_at"] is None
                           and not r["delivery_snapshot"] and not r["reflection_attempt_json"]), None)
            if anchor is None:
                return None
            snapshot = {"schema_version": "dream-reflection/v1", "delivery_id": anchor["report_id"],
                        "item_id": chosen[0]["item_id"], "item_sha256": chosen[0]["item_sha256"],
                        "items": chosen, "text": "\n\n".join(i["text"] for i in chosen), "delivered": False}
            encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            if len(encoded) > 10_000:
                raise ValueError("reflection bundle exceeds report snapshot budget")
            conn.execute("UPDATE reports SET claimed_at=?,claim_owner=?,delivery_day=?,delivery_snapshot=? "
                         "WHERE report_id=?", (now, owner, delivery_day, encoded, anchor["report_id"]))
            return {**snapshot, "claim_owner": owner}

    def ack_report(self, report_id: str, owner: str, *, now: float | None = None,
                   native_receipt: dict[str, Any] | None = None) -> bool:
        stamp = time.time() if now is None else now
        with self.transaction() as conn:
            row = conn.execute("SELECT reflection_attempt_json,delivery_snapshot FROM reports WHERE report_id=? AND claim_owner=? AND delivered_at IS NULL AND superseded_at IS NULL", (report_id, owner)).fetchone()
            if row is None:
                return False
            if not row["reflection_attempt_json"] and row["delivery_snapshot"]:
                if _reflection_snapshot_items(json.loads(row["delivery_snapshot"])):
                    return False  # A frozen reflection claim is not a send receipt.
            if row["reflection_attempt_json"]:
                attempt = json.loads(row["reflection_attempt_json"])
                if (not isinstance(native_receipt, dict) or native_receipt.get("success") is not True
                        or not str(native_receipt.get("message_id") or "").isdigit()
                        or int(native_receipt["message_id"]) <= 0
                        or native_receipt.get("attempt_id") != attempt.get("attempt_id")
                        or native_receipt.get("arguments_sha256") != attempt.get("arguments_sha256")):
                    return False
                attempt.update(state="delivered", receipt=native_receipt)
                conn.execute("UPDATE reports SET reflection_attempt_json=? WHERE report_id=?", (json.dumps(attempt,sort_keys=True),report_id))
            if not row["reflection_attempt_json"]:
                row_attempt = conn.execute("SELECT report_attempt_json FROM reports WHERE report_id=?", (report_id,)).fetchone()[0]
                if row_attempt:
                    return False  # An attempted native send must be completed with its receipt, never plain-acked.
            state = "confirmed" if row["reflection_attempt_json"] else "legacy_unverified"
            cursor = conn.execute(
                "UPDATE reports SET delivered_at=?, delivery_state=? WHERE report_id=? AND claim_owner=? "
                "AND claimed_at IS NOT NULL AND delivered_at IS NULL AND superseded_at IS NULL",
                (stamp, state, report_id, owner),
            )
            return cursor.rowcount == 1

    def begin_report_attempt(self, report_id: str, owner: str, attempt: dict[str, Any]) -> bool:
        """Fence one native send of a plain stored report. Never replayed once recorded."""
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM reports WHERE report_id=? AND claim_owner=? AND claimed_at IS NOT NULL "
                               "AND delivered_at IS NULL AND superseded_at IS NULL", (report_id, owner)).fetchone()
            if (row is None or row["report_attempt_json"] or row["reflection_attempt_json"]
                    or attempt.get("report_id") != report_id or attempt.get("fingerprint") != row["fingerprint"]):
                return False
            conn.execute("UPDATE reports SET report_attempt_json=? WHERE report_id=?", (json.dumps(attempt, sort_keys=True), report_id))
            return True

    def finish_report_attempt(self, report_id: str, attempt_id: str, state: str) -> bool:
        """Record a non-delivered outcome (failed/unknown). The attempt row stays, so no automatic replay."""
        with self.transaction() as conn:
            row = conn.execute("SELECT report_attempt_json FROM reports WHERE report_id=? AND delivered_at IS NULL", (report_id,)).fetchone()
            if row is None or not row[0]:
                return False
            attempt = json.loads(row[0])
            if attempt.get("attempt_id") != attempt_id or attempt.get("state") != "attempted" or state not in ("failed", "unknown"):
                return False
            attempt["state"] = state
            conn.execute("UPDATE reports SET report_attempt_json=? WHERE report_id=?", (json.dumps(attempt, sort_keys=True), report_id))
            return True

    def ack_report_receipt(self, report_id: str, owner: str, receipt: dict[str, Any], *, now: float | None = None) -> bool:
        """Confirm delivery only against the recorded attempt: the receipt must carry that attempt's id,
        argument hash, target hash, platform and report id, plus success and a positive numeric message id."""
        stamp = time.time() if now is None else now
        with self.transaction() as conn:
            row = conn.execute("SELECT report_attempt_json FROM reports WHERE report_id=? AND claim_owner=? AND claimed_at IS NOT NULL "
                               "AND delivered_at IS NULL AND superseded_at IS NULL", (report_id, owner)).fetchone()
            if row is None or not row[0]:
                return False
            attempt = json.loads(row[0])
            if (attempt.get("state") != "attempted" or not isinstance(receipt, dict) or receipt.get("success") is not True
                    or not str(receipt.get("message_id") or "").isdigit() or int(receipt["message_id"]) <= 0
                    or receipt.get("error") or receipt.get("skipped") is True or receipt.get("partial_success") is True
                    or any(receipt.get(k) != attempt.get(k) for k in
                           ("attempt_id", "arguments_sha256", "target_sha256", "platform", "report_id"))):
                return False
            attempt.update(state="delivered", receipt={k: receipt[k] for k in
                           ("success", "message_id", "attempt_id", "arguments_sha256", "target_sha256", "platform", "report_id")})
            cursor = conn.execute("UPDATE reports SET delivered_at=?, delivery_state='confirmed', report_attempt_json=? "
                                  "WHERE report_id=? AND claim_owner=? AND delivered_at IS NULL",
                                  (stamp, json.dumps(attempt, sort_keys=True), report_id, owner))
            return cursor.rowcount == 1

    def report_delivery_counts(self) -> dict[str, int]:
        """Metadata-only: confirmed (bound native receipt), legacy_unverified (stdout/plain ack), attempt_unknown."""
        with self.transaction() as conn:
            rows = conn.execute("SELECT delivery_state, report_attempt_json FROM reports WHERE superseded_at IS NULL "
                                "AND (delivered_at IS NOT NULL OR report_attempt_json<>'')").fetchall()
        out = {"confirmed": 0, "legacy_unverified": 0, "attempt_unknown": 0}
        for r in rows:
            if r["delivery_state"] in ("confirmed", "legacy_unverified"):
                out[r["delivery_state"]] += 1
            elif r["report_attempt_json"]:
                out["attempt_unknown"] += 1
        return out

    def begin_reflection_attempt(self, report_id: str, owner: str, attempt: dict[str, Any]) -> bool:
        """Fence an irreversible single transport attempt in the existing report."""
        with self.transaction() as conn:
            row=conn.execute("SELECT * FROM reports WHERE report_id=? AND claim_owner=? AND delivered_at IS NULL AND superseded_at IS NULL AND claimed_at IS NOT NULL",(report_id,owner)).fetchone()
            if row is None or row["reflection_attempt_json"]:
                return False
            snapshot=json.loads(row["delivery_snapshot"])
            if snapshot.get("item_id")!=attempt.get("item_id") or snapshot.get("item_sha256")!=attempt.get("item_sha256"):
                return False
            if snapshot.get("items") != attempt.get("items"):
                return False  # Bind every bundled item, not only its first item.
            conn.execute("UPDATE reports SET reflection_attempt_json=? WHERE report_id=?",(json.dumps(attempt,sort_keys=True),report_id))
            return True

    def finish_reflection_attempt(self, report_id: str, attempt_id: str, state: str) -> bool:
        if state not in {"failed","unknown"}:
            raise ValueError("invalid reflection attempt state")
        with self.transaction() as conn:
            row=conn.execute("SELECT reflection_attempt_json FROM reports WHERE report_id=?",(report_id,)).fetchone()
            if not row or not row["reflection_attempt_json"]:
                return False
            attempt=json.loads(row["reflection_attempt_json"])
            if attempt.get("attempt_id")!=attempt_id or attempt.get("state")!="attempted":
                return False
            attempt["state"]=state
            conn.execute("UPDATE reports SET reflection_attempt_json=? WHERE report_id=?",(json.dumps(attempt,sort_keys=True),report_id))
            return True

    def release_report_claim(self, report_id: str, owner: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE reports SET claimed_at=NULL, claim_owner='' WHERE report_id=? AND claim_owner=? "
                "AND delivered_at IS NULL AND superseded_at IS NULL",
                (report_id, owner),
            )
            return cursor.rowcount == 1

    def save_report_delivery_snapshot(
        self,
        report_id: str,
        owner: str,
        snapshot: str,
    ) -> str:
        """Persist the first bounded rendering and return that immutable value."""

        if not isinstance(snapshot, str) or not snapshot or len(snapshot) > 10_000:
            raise ValueError("report delivery snapshot is invalid")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT delivery_snapshot FROM reports WHERE report_id=? AND claim_owner=? "
                "AND claimed_at IS NOT NULL AND delivered_at IS NULL AND superseded_at IS NULL",
                (report_id, owner),
            ).fetchone()
            if row is None:
                raise ValueError("report delivery claim is no longer active")
            if row["delivery_snapshot"]:
                return str(row["delivery_snapshot"])
            conn.execute(
                "UPDATE reports SET delivery_snapshot=? WHERE report_id=? AND claim_owner=? "
                "AND delivery_snapshot='' AND delivered_at IS NULL AND superseded_at IS NULL",
                (snapshot, report_id, owner),
            )
            return snapshot

    # ----------------------------------------------------------- publication

    def commit_publication(
        self,
        *,
        run_id: str,
        fingerprint: str,
        staged_import_path: Path,
        final_import_path: Path,
        staged_report_path: Path,
        final_report_path: Path,
        dreams_path: Path,
        content_hash: str,
        report_hash: str,
        source_keys: Sequence[str],
        decisions: Sequence[dict[str, Any]],
        insights: Sequence[dict[str, Any]],
        lease_name: str,
        lease_owner: str,
        lease_generation: int,
        indexable: bool = True,
        now: float | None = None,
    ) -> str:
        """Atomically fence and record checkpoints, decisions, and outbox intent."""

        stamp = time.time() if now is None else now
        publication_id = deterministic_id("pub", run_id, fingerprint)
        with self.transaction() as conn:
            lease = conn.execute(
                "SELECT owner,generation,pid FROM leases WHERE name=?", (lease_name,)
            ).fetchone()
            if not lease or lease["owner"] != lease_owner or int(lease["generation"]) != lease_generation:
                raise LeaseHeld("dream sweep lease fence was lost before publication commit")
            if not _process_alive(int(lease["pid"])):
                raise LeaseHeld("dream sweep lease owner is no longer live")
            self._finalize_run_mutations(conn, run_id, stamp)
            conn.execute(
                "INSERT INTO publications(publication_id,run_id,fingerprint,staged_import_path,final_import_path,"
                "staged_report_path,final_report_path,dreams_path,content_hash,report_hash,status,lease_name,"
                "lease_owner,lease_generation,created_at,indexable) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?)",
                (
                    publication_id, run_id, fingerprint, str(staged_import_path), str(final_import_path),
                    str(staged_report_path), str(final_report_path), str(dreams_path), content_hash, report_hash,
                    lease_name, lease_owner, lease_generation, stamp, int(indexable),
                ),
            )
            report_id = deterministic_id("rpt", fingerprint)
            conn.execute(
                "INSERT INTO reports(report_id,run_id,path,fingerprint,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET path=excluded.path,run_id=excluded.run_id",
                (report_id, run_id, str(final_report_path), fingerprint, stamp),
            )
            for decision in decisions:
                candidate_status = {
                    "openviking_dream": "classified", "context_inbox": "classified",
                    "hypothesis": "hypothesis", "defer": "deferred", "reject": "rejected",
                }[decision["classification"]]
                publication_state = (
                    "outbox_pending"
                    if decision["classification"] == "openviking_dream"
                    else "not_requested"
                )
                staging_state = (
                    "pending_approval"
                    if decision["classification"] == "context_inbox"
                    and decision.get("actionability") == "act"
                    else "not_applicable"
                )
                merged_explain = self._explain_with_merge(
                    conn, str(decision["candidate_id"]), decision.get("explain", {})
                )
                conn.execute(
                    "UPDATE candidates SET status=?,score=?,classification=?,explain_json=?,"
                    "publication_state=?,staging_state=? WHERE candidate_id=?",
                    (candidate_status, float(decision["score"]), decision["classification"],
                     json.dumps(merged_explain, sort_keys=True), publication_state,
                     staging_state, decision["candidate_id"]),
                )
                promotion_id = deterministic_id("prm", run_id, decision["candidate_id"], decision["classification"])
                conn.execute(
                    "INSERT OR REPLACE INTO promotions(promotion_id,candidate_id,run_id,target,score,rationale,created_at,artifact_path) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        promotion_id, decision["candidate_id"], run_id, decision["classification"],
                        float(decision["score"]), str(decision["rationale"])[:1000], stamp,
                        str(final_import_path) if decision["classification"] == "openviking_dream" else None,
                    ),
                )
                self._queue_decision_staging(conn, run_id, decision, stamp)
            for insight in insights:
                conn.execute(
                    "UPDATE insights SET kind=?,claim=?,detail=?,confidence=?,status=?,run_id=?,"
                    "evidence_json=?,candidate_ids_json=?,contradiction_json=?,deep_supported=?,"
                    "deep_verdict=?,deep_rationale=?,reflection_json=? WHERE insight_id=?",
                    (str(insight["kind"]), str(insight["claim"]), str(insight.get("detail") or ""),
                     float(insight.get("confidence") or 0), str(insight.get("status") or "hypothesis"),
                     run_id, json.dumps(insight.get("evidence", []), sort_keys=True),
                     json.dumps(sorted(set(insight.get("candidate_ids", []))), sort_keys=True),
                     json.dumps(insight.get("contradiction_sides", []), sort_keys=True),
                     int(insight.get("deep_supported") is True), insight.get("deep_verdict"),
                     str(insight.get("deep_rationale") or "")[:900], json.dumps(insight.get("reflection", {}), sort_keys=True), str(insight["insight_id"])),
                )
            conn.executemany(
                "UPDATE sources SET status='complete',completed_at=?,completed_run_id=?,error=NULL WHERE source_key=?",
                [(stamp, run_id, key) for key in source_keys],
            )
        return publication_id

    def commit_without_publication(
        self,
        *,
        run_id: str,
        source_keys: Sequence[str],
        decisions: Sequence[dict[str, Any]],
        insights: Sequence[dict[str, Any]],
        lease_name: str,
        lease_owner: str,
        lease_generation: int,
        now: float | None = None,
    ) -> None:
        """Fence and checkpoint a semantic no-op in one transaction."""

        stamp = time.time() if now is None else now
        with self.transaction() as conn:
            lease = conn.execute(
                "SELECT owner,generation,pid FROM leases WHERE name=?", (lease_name,)
            ).fetchone()
            if (
                not lease or lease["owner"] != lease_owner
                or int(lease["generation"]) != lease_generation
                or not _process_alive(int(lease["pid"]))
            ):
                raise LeaseHeld("dream sweep lease fence was lost before no-op commit")
            self._finalize_run_mutations(conn, run_id, stamp)
            for decision in decisions:
                candidate_status = {
                    "openviking_dream": "classified", "context_inbox": "classified",
                    "hypothesis": "hypothesis", "defer": "deferred", "reject": "rejected",
                }[decision["classification"]]
                staging_state = (
                    "pending_approval"
                    if decision["classification"] == "context_inbox"
                    and decision.get("actionability") == "act"
                    else "not_applicable"
                )
                merged_explain = self._explain_with_merge(
                    conn, str(decision["candidate_id"]), decision.get("explain", {})
                )
                conn.execute(
                    "UPDATE candidates SET status=?,score=?,classification=?,explain_json=?,staging_state=? "
                    "WHERE candidate_id=?",
                    (candidate_status, float(decision["score"]), decision["classification"],
                     json.dumps(merged_explain, sort_keys=True), staging_state,
                     decision["candidate_id"]),
                )
                self._queue_decision_staging(conn, run_id, decision, stamp)
            for insight in insights:
                conn.execute(
                    "UPDATE insights SET kind=?,claim=?,detail=?,confidence=?,status=?,run_id=?,"
                    "evidence_json=?,candidate_ids_json=?,contradiction_json=?,deep_supported=?,"
                    "deep_verdict=?,deep_rationale=?,reflection_json=? WHERE insight_id=?",
                    (str(insight["kind"]), str(insight["claim"]), str(insight.get("detail") or ""),
                     float(insight.get("confidence") or 0), str(insight.get("status") or "hypothesis"),
                     run_id, json.dumps(insight.get("evidence", []), sort_keys=True),
                     json.dumps(sorted(set(insight.get("candidate_ids", []))), sort_keys=True),
                     json.dumps(insight.get("contradiction_sides", []), sort_keys=True),
                     int(insight.get("deep_supported") is True), insight.get("deep_verdict"),
                     str(insight.get("deep_rationale") or "")[:900], json.dumps(insight.get("reflection", {}), sort_keys=True), str(insight["insight_id"])),
                )
            conn.executemany(
                "UPDATE sources SET status='complete',completed_at=?,completed_run_id=?,error=NULL WHERE source_key=?",
                [(stamp, run_id, key) for key in source_keys],
            )

    @staticmethod
    def _finalize_run_mutations(conn: sqlite3.Connection, run_id: str, stamp: float) -> None:
        """Make staged reinforcement visible exactly once inside success commit."""

        conn.execute(
            "UPDATE candidates SET reinforcement=reinforcement+1,last_seen=?,last_run_id=? "
            "WHERE first_run_id<>? AND candidate_id IN "
            "(SELECT candidate_id FROM evidence WHERE run_id=? "
            " UNION SELECT candidate_id FROM candidate_claims WHERE run_id=?)",
            (stamp, run_id, run_id, run_id, run_id),
        )

    def rollback_run_mutations(self, run_id: str) -> None:
        """Remove only uncommitted candidate/evidence/insight rows from a run."""

        with self.transaction() as conn:
            conn.execute("DELETE FROM candidate_claims WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM evidence WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM candidates WHERE first_run_id=?", (run_id,))
            conn.execute("DELETE FROM insights WHERE run_id=?", (run_id,))

    @staticmethod
    def _queue_decision_staging(
        conn: sqlite3.Connection,
        run_id: str,
        decision: dict[str, Any],
        stamp: float,
    ) -> None:
        if (
            decision.get("classification") != "context_inbox"
            or decision.get("actionability") != "act"
        ):
            return
        candidate_id = str(decision["candidate_id"])
        refs = sorted(str(ref) for ref in decision.get("evidence_refs", []))[:12]
        provenance_hash = hashlib.sha256(
            (candidate_id + "\0" + run_id + "\0" + "\0".join(refs)).encode("utf-8")
        ).hexdigest()
        payload = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "kind": str(decision.get("kind") or "open_loop")[:48],
            "summary": str(decision.get("claim") or "")[:400],
            "requires_approval": True,
            "evidence_count": len(refs),
        }
        outbox_id = deterministic_id("stg", candidate_id, "intent")
        conn.execute(
            "INSERT OR IGNORE INTO staging_outbox("
            "outbox_id,candidate_id,run_id,target,payload_json,provenance_hash,status,created_at) "
            "VALUES(?,?,?,'intent',?,?,'pending',?)",
            (
                outbox_id,
                candidate_id,
                run_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                provenance_hash,
                stamp,
            ),
        )

    def rollback_abandoned_mutations(self) -> int:
        """Clean crashed running runs that have no committed publication intent."""

        with self.closing_connection() as conn:
            rows = conn.execute(
                "SELECT run_id FROM runs WHERE status='running' AND run_id NOT IN "
                "(SELECT run_id FROM publications WHERE status='pending')"
            ).fetchall()
        for row in rows:
            self.rollback_run_mutations(str(row["run_id"]))
            self.finish_run(str(row["run_id"]), status="failed", rounds=0,
                            error="abandoned run recovered")
        return len(rows)

    def pending_publications(self) -> list[dict[str, Any]]:
        with self.closing_connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM publications WHERE status='pending' ORDER BY created_at,publication_id"
            )]

    def publication_counts(self) -> dict[str, int]:
        with self.closing_connection() as conn:
            rows = conn.execute(
                "SELECT CASE WHEN synced_at IS NOT NULL AND sync_source_id<>'' "
                "AND sync_remote_resource_id<>'' AND sync_receipt<>'' THEN 'synced' ELSE status END AS state,"
                "COUNT(*) AS n FROM publications GROUP BY state"
            ).fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    def locally_published_publications(self, *, limit: int = 1000) -> list[dict[str, str]]:
        """Return bounded metadata for the external-sync acknowledgement step."""

        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("publication acknowledgement limit must be from 1 to 1000")
        with self.closing_connection() as conn:
            rows = conn.execute(
                "SELECT publication_id,run_id,content_hash,final_import_path FROM publications "
                "WHERE status='locally_published' AND synced_at IS NULL "
                "ORDER BY created_at,publication_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "publication_id": str(row["publication_id"]),
                "run_id": str(row["run_id"]),
                "content_hash": str(row["content_hash"]),
                "final_import_path": str(row["final_import_path"]),
            }
            for row in rows
        ]

    def committed_staging_paths(self) -> set[str]:
        with self.closing_connection() as conn:
            rows = conn.execute(
                "SELECT staged_import_path,staged_report_path FROM publications WHERE status='pending'"
            )
            return {str(value) for row in rows for value in row if value}

    def adopt_publication(
        self, publication_id: str, *, lease_name: str, owner: str, generation: int
    ) -> dict[str, Any]:
        """Fence a crashed owner's pending intent to the current live lease."""

        with self.transaction() as conn:
            lease = conn.execute(
                "SELECT owner,generation,pid FROM leases WHERE name=?", (lease_name,)
            ).fetchone()
            if (
                not lease
                or lease["owner"] != owner
                or int(lease["generation"]) != generation
                or not _process_alive(int(lease["pid"]))
            ):
                raise LeaseHeld("cannot adopt publication without current live lease")
            cursor = conn.execute(
                "UPDATE publications SET lease_name=?,lease_owner=?,lease_generation=? "
                "WHERE publication_id=? AND status='pending'",
                (lease_name, owner, generation, publication_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("publication intent is no longer pending")
            row = conn.execute(
                "SELECT * FROM publications WHERE publication_id=?", (publication_id,)
            ).fetchone()
            assert row is not None
            return dict(row)

    def mark_publication_published(
        self, publication_id: str, *, owner: str, generation: int, now: float | None = None
    ) -> bool:
        stamp = time.time() if now is None else now
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT lease_name,run_id,final_report_path FROM publications "
                "WHERE publication_id=? AND status='pending'",
                (publication_id,),
            ).fetchone()
            if row is None:
                return False
            # Enforce the same owner + generation + live-process fence the sibling
            # publication mutations (commit_publication, commit_without_publication,
            # adopt_publication) apply: a stale owner, a bumped generation, or a
            # dead PID must never be able to finalize a pending publication.
            lease = conn.execute(
                "SELECT owner,generation,pid FROM leases WHERE name=?", (row["lease_name"],)
            ).fetchone()
            if (
                not lease
                or lease["owner"] != owner
                or int(lease["generation"]) != generation
                or not _process_alive(int(lease["pid"]))
            ):
                raise LeaseHeld("dream sweep lease fence was lost before publication acknowledgement")
            cursor = conn.execute(
                "UPDATE publications SET status='locally_published',published_at=?,error=NULL "
                "WHERE publication_id=? AND status='pending'",
                (stamp, publication_id),
            )
            conn.execute(
                "UPDATE candidates SET publication_state='locally_published' "
                "WHERE candidate_id IN (SELECT candidate_id FROM promotions "
                "WHERE run_id=? AND target='openviking_dream')",
                (row["run_id"],),
            )
            run = conn.execute("SELECT summary_json FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
            summary = json.loads(run["summary_json"] or "{}") if run else {}
            summary["report"] = row["final_report_path"]
            conn.execute(
                "UPDATE runs SET status='complete',finished_at=?,error=NULL,summary_json=? WHERE run_id=?",
                (stamp, json.dumps(summary, sort_keys=True), row["run_id"]),
            )
            return cursor.rowcount == 1

    def mark_publication_synced(
        self,
        publication_id: str,
        *,
        state_db: Path,
        source_id: str,
        now: float | None = None,
    ) -> bool:
        """Acknowledge only an exact receipt from the normal sync State DB."""

        if not isinstance(source_id, str) or not source_id or len(source_id) > 200:
            raise ValueError("source_id is invalid")
        normal_state = Path(state_db).expanduser().absolute()
        if not normal_state.is_file() or normal_state.is_symlink():
            raise ValueError("normal sync State DB is unavailable")
        stamp = time.time() if now is None else now
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT run_id,status,synced_at,content_hash FROM publications WHERE publication_id=?",
                (publication_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown publication")
            if row["synced_at"] is not None:
                return False
            if row["status"] != "locally_published":
                raise ValueError("publication must be locally published before sync acknowledgement")
            from ..state import State

            receipt = State(normal_state).verified_sync_receipt(
                source_id, str(row["content_hash"])
            )
            if receipt is None:
                raise ValueError("exact Dream resource has no verified sync receipt")
            conn.execute(
                "UPDATE publications SET synced_at=?,sync_evidence_hash=?,sync_source_id=?,"
                "sync_remote_resource_id=?,sync_receipt=? WHERE publication_id=?",
                (
                    stamp,
                    receipt["receipt"],
                    receipt["source_id"],
                    receipt["ov_resource_id"],
                    receipt["receipt"],
                    publication_id,
                ),
            )
            conn.execute(
                "UPDATE candidates SET publication_state='synced',synced_at=? "
                "WHERE candidate_id IN (SELECT candidate_id FROM promotions "
                "WHERE run_id=? AND target='openviking_dream')",
                (stamp, row["run_id"]),
            )
        return True

    def prune_publications(self, *, retain_publications: int) -> list[dict[str, Any]]:
        """Prune only old imports carrying a complete exact external receipt."""

        keep = max(1, int(retain_publications))
        with self.transaction() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT publication_id,run_id,final_import_path,sync_source_id,"
                    "sync_remote_resource_id,sync_receipt FROM publications "
                    "WHERE synced_at IS NOT NULL ORDER BY synced_at DESC,publication_id DESC"
                ).fetchall()
            ]
            verified = [
                row
                for row in rows
                if row["sync_source_id"]
                and row["sync_remote_resource_id"]
                and _HASH_RE.fullmatch(str(row["sync_receipt"] or ""))
            ]
            stale = verified[keep:]
            if stale:
                conn.executemany(
                    "DELETE FROM publications WHERE publication_id=? AND synced_at IS NOT NULL "
                    "AND sync_source_id<>'' AND sync_remote_resource_id<>'' AND sync_receipt<>''",
                    [(row["publication_id"],) for row in stale],
                )
            return stale

    # ---------------------------------------------------------- typed staging

    def pending_staging(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("staging limit must be from 1 to 1000")
        with self.closing_connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM staging_outbox WHERE status='pending' "
                    "ORDER BY created_at,outbox_id LIMIT ?",
                    (limit,),
                )
            ]

    def mark_staging_staged(self, outbox_id: str, *, now: float | None = None) -> bool:
        stamp = time.time() if now is None else now
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT candidate_id FROM staging_outbox WHERE outbox_id=? AND status='pending'",
                (outbox_id,),
            ).fetchone()
            if row is None:
                return False
            cursor = conn.execute(
                "UPDATE staging_outbox SET status='staged',staged_at=?,error_class='' "
                "WHERE outbox_id=? AND status='pending'",
                (stamp, outbox_id),
            )
            conn.execute(
                "UPDATE candidates SET staging_state='staged_pending_approval' WHERE candidate_id=?",
                (row["candidate_id"],),
            )
            return cursor.rowcount == 1

    def record_staging_failure(self, outbox_id: str, error_class: str) -> None:
        allowed = {"validation", "storage", "unavailable", "other"}
        classification = error_class if error_class in allowed else "other"
        with self.transaction() as conn:
            conn.execute(
                "UPDATE staging_outbox SET attempts=attempts+1,error_class=? "
                "WHERE outbox_id=? AND status='pending'",
                (classification, outbox_id),
            )

    def staging_counts(self) -> dict[str, int]:
        with self.closing_connection() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) AS n FROM staging_outbox GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def latest_report(self) -> dict[str, Any] | None:
        with self.closing_connection() as conn:
            row = conn.execute("SELECT * FROM reports ORDER BY created_at DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def report_fingerprint_exists(self, fingerprint: str) -> bool:
        with self.closing_connection() as conn:
            row = conn.execute("SELECT 1 FROM reports WHERE fingerprint=?", (fingerprint,)).fetchone()
            return row is not None

    # ----------------------------------------------------------------- status

    def latest_run(self) -> dict[str, Any] | None:
        with self.closing_connection() as conn:
            row = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
            if row is None:
                return None
            run = dict(row)
        run["summary"] = json.loads(run.pop("summary_json") or "{}")
        return run

    def run_by_id(self, run_id: str) -> dict[str, Any] | None:
        with self.closing_connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            run = dict(row)
        run["summary"] = json.loads(run.pop("summary_json") or "{}")
        return run

    def phases_for(self, run_id: str) -> list[dict[str, Any]]:
        with self.closing_connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM phases WHERE run_id=? ORDER BY started_at, phase_id", (run_id,)
                )
            ]

    def recent_failures(self, limit: int = 5) -> list[dict[str, Any]]:
        with self.closing_connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT phase, error, finished_at FROM phases WHERE status='failed' "
                    "ORDER BY finished_at DESC LIMIT ?",
                    (limit,),
                )
            ]

    def prune(self, *, retain_runs: int) -> int:
        """Drop the oldest runs beyond the retention window."""

        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT -1 OFFSET ?", (retain_runs,)
            ).fetchall()
            stale = [row["run_id"] for row in rows]
            if not stale:
                return 0
            placeholders = ",".join("?" for _ in stale)
            stale_candidates = [
                row["candidate_id"]
                for row in conn.execute(
                    f"SELECT candidate_id FROM candidates WHERE last_run_id IN ({placeholders}) "
                    "AND status IN ('rejected','deferred')",
                    stale,
                )
            ]
            if stale_candidates:
                candidate_placeholders = ",".join("?" for _ in stale_candidates)
                conn.execute(f"DELETE FROM evidence WHERE candidate_id IN ({candidate_placeholders})", stale_candidates)
                conn.execute(f"DELETE FROM candidates WHERE candidate_id IN ({candidate_placeholders})", stale_candidates)
            for table in ("phases", "rounds", "retrievals", "promotions", "insights"):
                conn.execute(f"DELETE FROM {table} WHERE run_id IN ({placeholders})", stale)
            conn.execute(f"DELETE FROM runs WHERE run_id IN ({placeholders})", stale)
        return len(stale)

    def prune_reports(self, *, retain_reports: int) -> list[dict[str, Any]]:
        """Remove old report-delivery rows and return exact paths for safe file cleanup."""

        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT report_id,run_id,path FROM reports WHERE delivery_snapshot='' "
                "AND reflection_attempt_json='' ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                (max(1, retain_reports),),
            ).fetchall()
            stale = [dict(row) for row in rows]
            if stale:
                conn.executemany("DELETE FROM reports WHERE report_id=?", [(row["report_id"],) for row in stale])
            return stale


# --------------------------------------------------------------------- helpers


def _reflection_snapshot_items(snapshot: Any) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return []
    items = snapshot.get("items")
    if items is None and snapshot.get("item_id") and snapshot.get("item_sha256"):
        items = [snapshot]  # Existing single-item v1 receipt/reservation.
    if not isinstance(items, list):
        return []
    if any(not isinstance(i, dict) or not i.get("item_id") or not i.get("item_sha256") for i in items):
        raise ValueError("invalid frozen reflection identities")
    return items


def normalize_claim(claim: str) -> str:
    """Conservative ordered-token key that preserves relations and negation."""

    words = [word.lower() for word in _WORD_RE.findall(claim)]
    if not words:
        return hashlib.sha256(claim.strip().lower().encode("utf-8")).hexdigest()[:32]
    # Preserve order and repetition.  Relational opposites such as "Alice
    # manages Bob" and "Bob manages Alice", and "likes" / "does not like",
    # must never reinforce one another merely because they share a word set.
    return " ".join(words)


def exact_claim_key(kind: str, claim: str) -> str:
    """Kind-scoped exact ordered-token identity used whenever validation is unsure."""

    if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", kind):
        raise ValueError("candidate kind is unsafe")
    return f"v0|{kind}|{normalize_claim(claim)}"


def validated_claim_identity(
    claim: str, proposal: ClaimIdentity | None
) -> ClaimIdentity | None:
    """Accept a model identity only when exact claim text proves every field.

    Subject, predicate, and object must have one unambiguous ordered match;
    scope must occur as a separate exact span; and explicit negation must agree
    with the proposed polarity. Any uncertainty returns ``None`` so the caller
    uses the kind-scoped exact ordered-claim key instead of merging.
    """

    if proposal is None:
        return None
    try:
        canonical = proposal.canonical()
    except ValueError:
        return None
    claim_tokens = [
        unicodedata.normalize("NFKC", word).casefold() for word in _WORD_RE.findall(claim)
    ]
    if not claim_tokens:
        return None
    component_tokens = {
        name: value.split()
        for name, value in canonical.items()
        if name in {"subject", "object", "scope"}
    }
    subject_spans = _token_spans(claim_tokens, component_tokens["subject"])
    object_spans = _token_spans(claim_tokens, component_tokens["object"])
    scope_spans = _token_spans(claim_tokens, component_tokens["scope"])
    predicate_spans = _predicate_spans(claim_tokens, canonical["predicate"].split())
    ordered = [
        (subject, predicate, obj)
        for subject in subject_spans
        for predicate in predicate_spans
        for obj in object_spans
        if subject[1] <= predicate[0] and predicate[1] <= obj[0]
    ]
    if len(ordered) != 1 or len(scope_spans) != 1:
        return None
    relational_positions = {
        position
        for start, end in ordered[0]
        for position in range(start, end)
    }
    scope_positions = set(range(*scope_spans[0]))
    if relational_positions & scope_positions:
        return None
    negation_positions = [
        index for index, token in enumerate(claim_tokens) if token in _NEGATION_TOKENS
    ]
    if canonical["polarity"] == "negative":
        # Accept only the unambiguous local form where one negator modifies
        # the relation immediately before its predicate ("does not use",
        # "never uses"). A negator attached to a different subject/object is
        # uncertainty, not proof of negative polarity.
        if negation_positions != [ordered[0][1][0] - 1]:
            return None
    elif negation_positions:
        return None
    return proposal


def _token_spans(tokens: Sequence[str], wanted: Sequence[str]) -> list[tuple[int, int]]:
    if not wanted or len(wanted) > len(tokens):
        return []
    size = len(wanted)
    return [
        (index, index + size)
        for index in range(len(tokens) - size + 1)
        if list(tokens[index : index + size]) == list(wanted)
    ]


def _predicate_spans(tokens: Sequence[str], wanted: Sequence[str]) -> list[tuple[int, int]]:
    if not wanted or len(wanted) > len(tokens):
        return []
    size = len(wanted)
    result: list[tuple[int, int]] = []
    for index in range(len(tokens) - size + 1):
        actual = [_PREDICATE_ALIASES.get(token, token) for token in tokens[index : index + size]]
        if actual == list(wanted):
            result.append((index, index + size))
    return result


def canonical_claim_key(kind: str, identity: ClaimIdentity | dict[str, Any]) -> str:
    """Return a deterministic typed key without semantic/embedding guessing."""

    if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", kind):
        raise ValueError("candidate kind is unsafe")
    item = identity if isinstance(identity, ClaimIdentity) else ClaimIdentity.from_mapping(identity)
    canonical = item.canonical()
    return "v1|" + kind + "|" + json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _canonical_component(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"claim identity {label} must be a string")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"claim identity {label} must contain 1..{maximum} characters")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError(f"claim identity {label} contains control characters")
    words = _WORD_RE.findall(normalized)
    while len(words) > 1 and words[0] in _LEADING_ARTICLES:
        words.pop(0)
    if not words:
        raise ValueError(f"claim identity {label} has no canonical tokens")
    return " ".join(words)


def _canonical_predicate(value: Any) -> str:
    predicate = _canonical_component(value, "predicate", 80)
    # Only explicit, small lexical aliases are folded.  Unknown relations stay
    # distinct, which is safer than stemming or similarity-based merging.
    return _PREDICATE_ALIASES.get(predicate, predicate)


def deterministic_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def snippet_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Owned by another user: assume alive rather than stealing the lease.
        return True
    except OSError:
        return True
    return True


def _reject_symlink_sqlite_paths(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.is_symlink():
            raise ValueError(f"refusing symlinked SQLite path: {candidate}")


def _reject_unsafe_ancestors(path: Path) -> None:
    """lstat every existing ancestor; links and non-directories are unsafe."""

    absolute = path.absolute()
    chain = list(reversed((absolute, *absolute.parents)))
    missing_seen = False
    for candidate in chain:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            missing_seen = True
            continue
        if missing_seen:
            # An existing descendant beneath a missing ancestor is impossible
            # without a race; fail closed.
            raise ValueError(f"unsafe path ancestry changed while checking: {candidate}")
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"path ancestor is not a real directory: {candidate}")


def _chmod_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        _chmod_if_owned(Path(f"{path}{suffix}"), 0o600)


def _chmod_if_owned(path: Path, mode: int) -> None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        return
    try:
        os.chmod(path, mode)
    except OSError:
        return
