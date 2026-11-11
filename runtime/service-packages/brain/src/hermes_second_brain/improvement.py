"""Typed, review-gated process-improvement proposals.

The queue deliberately stores proposals, not executable work.  No method in
this module edits a skill, configuration file, test, or workflow.  Every state
change is validated and committed in a private SQLite transaction; review and
approval are distinct states so a model review can never authorize applying a
change.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .context_inbox import (
    _chmod_private_file,
    _chmod_sqlite_sidecars,
    _prepare_private_file_parent,
    _reject_symlink_sqlite_paths,
    redact_sensitive_text,
)
from .temporary_memory import canonical_utc, coerce_utc_clock, validate_idempotency_key


DEFAULT_IMPROVEMENT_DB = Path("~/.hermes/second-brain/improvements.sqlite3").expanduser()

PROPOSAL_STATES = (
    "proposed",
    "reviewed",
    "approved",
    "applied",
    "canary",
    "kept",
    "rolled_back",
    "rejected",
    "expired",
)
TERMINAL_STATES = frozenset({"kept", "rolled_back", "rejected", "expired"})
RISK_CLASSES = ("low", "medium", "high")
TARGET_KINDS = ("skill", "config", "test", "workflow")

TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"reviewed", "rejected", "expired"}),
    "reviewed": frozenset({"approved", "rejected", "expired"}),
    "approved": frozenset({"applied", "rejected", "expired"}),
    "applied": frozenset({"canary", "rolled_back"}),
    "canary": frozenset({"kept", "rolled_back"}),
    "kept": frozenset(),
    "rolled_back": frozenset(),
    "rejected": frozenset(),
    "expired": frozenset(),
}

MAX_TITLE = 180
MAX_INTERVENTION = 1200
MAX_METRIC = 96
MAX_ROLLBACK = 600
MAX_COUNTER = 1_000_000
MAX_BATCH = 20


class ImprovementError(ValueError):
    """The proposal or requested mutation is invalid."""


class InvalidTransition(ImprovementError):
    """The proposal state machine rejected a transition."""


class ImprovementStore:
    """Private durable queue of typed, non-executing proposals."""

    def __init__(self, db_path: Path | str = DEFAULT_IMPROVEMENT_DB):
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
                CREATE TABLE IF NOT EXISTS improvement_proposals(
                  proposal_id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  content_sha256 TEXT NOT NULL,
                  title TEXT NOT NULL,
                  observation_count INTEGER NOT NULL CHECK(observation_count>=0),
                  evidence_count INTEGER NOT NULL CHECK(evidence_count>=0),
                  counterevidence_count INTEGER NOT NULL CHECK(counterevidence_count>=0),
                  risk_class TEXT NOT NULL CHECK(risk_class IN ('low','medium','high')),
                  target_kind TEXT NOT NULL CHECK(target_kind IN ('skill','config','test','workflow')),
                  proposed_intervention TEXT NOT NULL,
                  expected_metric TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN
                    ('proposed','reviewed','approved','applied','canary','kept',
                     'rolled_back','rejected','expired')),
                  canary_state TEXT NOT NULL DEFAULT 'not_started' CHECK(canary_state IN
                    ('not_started','running','passed','failed')),
                  rollback_note TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  terminal_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS improvement_transitions(
                  transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  proposal_id TEXT NOT NULL,
                  from_state TEXT NOT NULL,
                  to_state TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(proposal_id) REFERENCES improvement_proposals(proposal_id)
                );
                CREATE INDEX IF NOT EXISTS idx_improvement_state
                  ON improvement_proposals(state, created_at);
                """
            )

    def propose(
        self,
        *,
        idempotency_key: str,
        title: str,
        risk_class: str,
        target_kind: str,
        proposed_intervention: str,
        expected_metric: str,
        observation_count: int = 0,
        evidence_count: int = 0,
        counterevidence_count: int = 0,
        rollback_note: str = "",
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        proposal = _validate_proposal(
            {
                "title": title,
                "risk_class": risk_class,
                "target_kind": target_kind,
                "proposed_intervention": proposed_intervention,
                "expected_metric": expected_metric,
                "observation_count": observation_count,
                "evidence_count": evidence_count,
                "counterevidence_count": counterevidence_count,
                "rollback_note": rollback_note,
            }
        )
        key = validate_idempotency_key(idempotency_key)
        stamp = canonical_utc(coerce_utc_clock(now))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row, created = self._insert_proposed(conn, key, proposal, stamp)
        return _result(dict(row), created=created)

    def persist_reviewed_batch(
        self,
        proposals: Iterable[Mapping[str, Any]],
        *,
        review_fingerprint: str,
        now: str | dt.datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically persist already cross-reviewed proposals as ``reviewed``.

        This is the only model-runner entry point.  It validates every item
        before opening a transaction, then creates and reviews the whole batch
        together.  A failure therefore leaves all prior queue state intact.
        """

        fingerprint = validate_idempotency_key(review_fingerprint)
        validated = [_validate_proposal(dict(item)) for item in proposals]
        if len(validated) > MAX_BATCH:
            raise ImprovementError(f"review batch exceeds {MAX_BATCH} proposals")
        stamp = canonical_utc(coerce_utc_clock(now))
        output: list[dict[str, Any]] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for index, proposal in enumerate(validated):
                digest = hashlib.sha256(
                    _canonical_proposal_json(proposal).encode("utf-8")
                ).hexdigest()[:32]
                key = validate_idempotency_key(f"review:{fingerprint}:{index}:{digest}")
                row, created = self._insert_proposed(conn, key, proposal, stamp)
                if row["state"] == "proposed":
                    conn.execute(
                        "UPDATE improvement_proposals SET state='reviewed',updated_at=? WHERE proposal_id=?",
                        (stamp, row["proposal_id"]),
                    )
                    conn.execute(
                        "INSERT INTO improvement_transitions(proposal_id,from_state,to_state,created_at) "
                        "VALUES(?,?,?,?)",
                        (row["proposal_id"], "proposed", "reviewed", stamp),
                    )
                row = self._require(conn, row["proposal_id"])
                output.append(_result(dict(row), created=created))
        return output

    def review(self, proposal_id: str, *, now: str | dt.datetime | None = None) -> dict[str, Any]:
        return self._transition(proposal_id, "reviewed", now=now)

    def approve(self, proposal_id: str, *, now: str | dt.datetime | None = None) -> dict[str, Any]:
        return self._transition(proposal_id, "approved", now=now)

    def apply(self, proposal_id: str, *, now: str | dt.datetime | None = None) -> dict[str, Any]:
        # This only records an operator-confirmed external application.  It
        # intentionally performs no application itself.
        return self._transition(proposal_id, "applied", now=now)

    def start_canary(self, proposal_id: str, *, now: str | dt.datetime | None = None) -> dict[str, Any]:
        return self._transition(
            proposal_id, "canary", now=now, updates={"canary_state": "running"}
        )

    def keep(self, proposal_id: str, *, now: str | dt.datetime | None = None) -> dict[str, Any]:
        return self._transition(
            proposal_id, "kept", now=now, updates={"canary_state": "passed"}
        )

    def roll_back(
        self,
        proposal_id: str,
        *,
        rollback_note: str,
        now: str | dt.datetime | None = None,
    ) -> dict[str, Any]:
        note = _bounded_text(rollback_note, "rollback_note", MAX_ROLLBACK, allow_empty=False)
        return self._transition(
            proposal_id,
            "rolled_back",
            now=now,
            updates={"canary_state": "failed", "rollback_note": note},
        )

    def reject(self, proposal_id: str, *, now: str | dt.datetime | None = None) -> dict[str, Any]:
        return self._transition(proposal_id, "rejected", now=now)

    def expire_stale(
        self,
        *,
        now: str | dt.datetime | None = None,
        ttl_days: int = 30,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if type(ttl_days) is not int or not 1 <= ttl_days <= 3650:
            raise ImprovementError("ttl_days must be an integer from 1 to 3650")
        if not isinstance(dry_run, bool):
            raise ImprovementError("dry_run must be a boolean")
        clock = coerce_utc_clock(now)
        cutoff = canonical_utc(clock - dt.timedelta(days=ttl_days))
        stamp = canonical_utc(clock)
        expirable = ("proposed", "reviewed", "approved")
        with self.connect() as conn:
            if not dry_run:
                conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT proposal_id,state FROM improvement_proposals "
                "WHERE state IN ('proposed','reviewed','approved') AND updated_at<=? "
                "ORDER BY proposal_id",
                (cutoff,),
            ).fetchall()
            expired = [row["proposal_id"] for row in rows]
            if not dry_run:
                for row in rows:
                    if row["state"] not in expirable:
                        continue
                    conn.execute(
                        "UPDATE improvement_proposals SET state='expired',updated_at=?,terminal_at=? "
                        "WHERE proposal_id=? AND state=?",
                        (stamp, stamp, row["proposal_id"], row["state"]),
                    )
                    conn.execute(
                        "INSERT INTO improvement_transitions(proposal_id,from_state,to_state,created_at) "
                        "VALUES(?,?,?,?)",
                        (row["proposal_id"], row["state"], "expired", stamp),
                    )
        return {"operation": "expire_stale", "dry_run": dry_run, "expired": expired}

    def get(self, proposal_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = self._require(conn, proposal_id)
        return _result(dict(row), created=False)

    def list(self, *, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if state is not None and state not in PROPOSAL_STATES:
            raise ImprovementError("unknown proposal state")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ImprovementError("limit must be an integer from 1 to 1000")
        with self.connect() as conn:
            if state is None:
                rows = conn.execute(
                    "SELECT * FROM improvement_proposals ORDER BY created_at,proposal_id LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM improvement_proposals WHERE state=? "
                    "ORDER BY created_at,proposal_id LIMIT ?",
                    (state, limit),
                ).fetchall()
        return [_result(dict(row), created=False) for row in rows]

    def count(self, *, state: str | None = None) -> int:
        if state is not None and state not in PROPOSAL_STATES:
            raise ImprovementError("unknown proposal state")
        with self.connect() as conn:
            if state is None:
                value = conn.execute("SELECT COUNT(*) FROM improvement_proposals").fetchone()[0]
            else:
                value = conn.execute(
                    "SELECT COUNT(*) FROM improvement_proposals WHERE state=?", (state,)
                ).fetchone()[0]
        return int(value)

    def snapshot(self) -> dict[str, Any]:
        with self.connect() as conn:
            counts = {
                row[0]: int(row[1])
                for row in conn.execute(
                    "SELECT state,COUNT(*) FROM improvement_proposals GROUP BY state"
                )
            }
        return {
            "states": {state: counts.get(state, 0) for state in PROPOSAL_STATES},
            "pending_approval": counts.get("reviewed", 0),
            "active_canaries": counts.get("canary", 0),
        }

    def _insert_proposed(
        self,
        conn: sqlite3.Connection,
        key: str,
        proposal: Mapping[str, Any],
        stamp: str,
    ) -> tuple[sqlite3.Row, bool]:
        content_hash = hashlib.sha256(
            _canonical_proposal_json(proposal).encode("utf-8")
        ).hexdigest()
        existing = conn.execute(
            "SELECT * FROM improvement_proposals WHERE idempotency_key=?", (key,)
        ).fetchone()
        if existing is not None:
            if existing["content_sha256"] != content_hash:
                raise ImprovementError(
                    "idempotency_key already exists with different proposal content"
                )
            return existing, False
        proposal_id = "imp_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        conn.execute(
            """
            INSERT INTO improvement_proposals(
              proposal_id,idempotency_key,content_sha256,title,observation_count,
              evidence_count,counterevidence_count,risk_class,target_kind,
              proposed_intervention,expected_metric,state,canary_state,
              rollback_note,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                proposal_id,
                key,
                content_hash,
                proposal["title"],
                proposal["observation_count"],
                proposal["evidence_count"],
                proposal["counterevidence_count"],
                proposal["risk_class"],
                proposal["target_kind"],
                proposal["proposed_intervention"],
                proposal["expected_metric"],
                "proposed",
                "not_started",
                proposal["rollback_note"],
                stamp,
                stamp,
            ),
        )
        return self._require(conn, proposal_id), True

    def _transition(
        self,
        proposal_id: str,
        to_state: str,
        *,
        now: str | dt.datetime | None,
        updates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if to_state not in PROPOSAL_STATES:
            raise ImprovementError("unknown proposal state")
        stamp = canonical_utc(coerce_utc_clock(now))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._require(conn, proposal_id)
            current = row["state"]
            if current == to_state:
                return _result(dict(row), created=False)
            if to_state not in TRANSITIONS[current]:
                raise InvalidTransition(f"proposal cannot move {current} -> {to_state}")
            changed: dict[str, Any] = dict(updates or {})
            if set(changed) - {"canary_state", "rollback_note"}:
                raise ImprovementError("unsafe proposal update")
            changed["state"] = to_state
            changed["updated_at"] = stamp
            if to_state in TERMINAL_STATES:
                changed["terminal_at"] = stamp
            assignments = ",".join(f"{name}=?" for name in changed)
            conn.execute(
                f"UPDATE improvement_proposals SET {assignments} WHERE proposal_id=?",
                (*changed.values(), proposal_id),
            )
            conn.execute(
                "INSERT INTO improvement_transitions(proposal_id,from_state,to_state,created_at) "
                "VALUES(?,?,?,?)",
                (proposal_id, current, to_state, stamp),
            )
            row = self._require(conn, proposal_id)
        return _result(dict(row), created=False)

    @staticmethod
    def _require(conn: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
        if not isinstance(proposal_id, str) or not proposal_id.startswith("imp_"):
            raise ImprovementError("unknown proposal")
        row = conn.execute(
            "SELECT * FROM improvement_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise ImprovementError("unknown proposal")
        return row


def _validate_proposal(raw: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "title",
        "observation_count",
        "evidence_count",
        "counterevidence_count",
        "risk_class",
        "target_kind",
        "proposed_intervention",
        "expected_metric",
        "rollback_note",
    }
    required = allowed - {"rollback_note"}
    if set(raw) - allowed or not required.issubset(raw):
        raise ImprovementError("proposal schema is invalid")
    risk = raw.get("risk_class")
    target = raw.get("target_kind")
    if risk not in RISK_CLASSES:
        raise ImprovementError("risk_class is not allowlisted")
    if target not in TARGET_KINDS:
        raise ImprovementError("target_kind is not allowlisted")
    return {
        "title": _bounded_text(raw.get("title"), "title", MAX_TITLE, allow_empty=False),
        "observation_count": _counter(raw.get("observation_count"), "observation_count"),
        "evidence_count": _counter(raw.get("evidence_count"), "evidence_count"),
        "counterevidence_count": _counter(
            raw.get("counterevidence_count"), "counterevidence_count"
        ),
        "risk_class": str(risk),
        "target_kind": str(target),
        "proposed_intervention": _bounded_text(
            raw.get("proposed_intervention"),
            "proposed_intervention",
            MAX_INTERVENTION,
            allow_empty=False,
        ),
        "expected_metric": _bounded_token(raw.get("expected_metric"), "expected_metric", MAX_METRIC),
        "rollback_note": _bounded_text(
            raw.get("rollback_note", ""), "rollback_note", MAX_ROLLBACK, allow_empty=True
        ),
    }


def _canonical_proposal_json(proposal: Mapping[str, Any]) -> str:
    return json.dumps(dict(proposal), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _bounded_text(value: Any, label: str, maximum: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise ImprovementError(f"{label} must be a string")
    clean = " ".join(redact_sensitive_text(value).split())
    if not allow_empty and not clean:
        raise ImprovementError(f"{label} must not be empty")
    if len(clean) > maximum:
        raise ImprovementError(f"{label} exceeds {maximum} characters")
    return clean


def _bounded_token(value: Any, label: str, maximum: int) -> str:
    clean = _bounded_text(value, label, maximum, allow_empty=False)
    if any(not (character.isalnum() or character in "._:-") for character in clean):
        raise ImprovementError(f"{label} contains unsafe characters")
    return clean


def _counter(value: Any, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise ImprovementError(f"{label} must be an integer from 0 to {MAX_COUNTER}")
    return value


def _result(row: Mapping[str, Any], *, created: bool) -> dict[str, Any]:
    return {
        "proposal_id": row["proposal_id"],
        "title": row["title"],
        "observation_count": int(row["observation_count"]),
        "evidence_count": int(row["evidence_count"]),
        "counterevidence_count": int(row["counterevidence_count"]),
        "risk_class": row["risk_class"],
        "target_kind": row["target_kind"],
        "proposed_intervention": row["proposed_intervention"],
        "expected_metric": row["expected_metric"],
        "state": row["state"],
        "canary_state": row["canary_state"],
        "rollback_note": row["rollback_note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "terminal_at": row["terminal_at"],
        "created": created,
    }
