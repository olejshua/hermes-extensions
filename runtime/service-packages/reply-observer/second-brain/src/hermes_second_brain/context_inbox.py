from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from contextlib import contextmanager
from urllib.parse import quote, urlsplit, urlunsplit

DEFAULT_CONTEXT_DB = Path("~/.hermes/second-brain/context-inbox.sqlite3").expanduser()
DEFAULT_CONTEXT_EXPORT = Path("~/.hermes/second-brain/import/context/context-inbox.txt").expanduser()
# Canonical bounds for per-section daily-brief caps. Shared with
# briefing_preferences so effective values from preferences and explicit CLI
# overrides are validated against the same 1..100 window.
MIN_SECTION_LIMIT = 1
MAX_SECTION_LIMIT = 100
CORE_DATA_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)
UNIX_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

# Default SQLite busy timeout for the context DB. Concurrent writers (the
# scheduled sync, context-import, and live collectors) share one file, so a
# generous default lets a blocked writer wait for the lock rather than raising
# "database is locked". Overridable via HERMES_CONTEXT_DB_BUSY_TIMEOUT_MS.
DEFAULT_CONTEXT_DB_BUSY_TIMEOUT_MS = 30000


def _context_db_busy_timeout_ms() -> int:
    raw = os.environ.get("HERMES_CONTEXT_DB_BUSY_TIMEOUT_MS")
    if raw is None:
        return DEFAULT_CONTEXT_DB_BUSY_TIMEOUT_MS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_DB_BUSY_TIMEOUT_MS
    # Clamp to a sane floor; a zero/negative timeout would restore the old
    # fail-fast lock behavior that this setting exists to prevent.
    return max(1000, value)


@dataclass(frozen=True)
class ImportSummary:
    source: str
    imported: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = ()
    status: str = "ok"
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "imported": self.imported,
            "skipped": self.skipped,
            "errors": list(self.errors),
            "status": self.status,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class RankedEvent:
    event_id: str
    score: int
    tier: str
    reasons: tuple[str, ...]


class ContextInbox:
    def __init__(self, db_path: Path | str = DEFAULT_CONTEXT_DB):
        self.db_path = Path(db_path).expanduser()
        _reject_symlink_sqlite_paths(self.db_path)
        _prepare_private_file_parent(self.db_path.parent)
        _reject_symlink_sqlite_paths(self.db_path)
        _chmod_private_file(self.db_path)
        _chmod_sqlite_sidecars(self.db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        """Return the raw configured SQLite connection (public compatibility API)."""

        _reject_symlink_sqlite_paths(self.db_path)
        conn = sqlite3.connect(self.db_path)
        # A PRAGMA (e.g. a WAL failure on a read-only mount) or a chmod raising
        # after connect would otherwise leak this handle. Close it on any setup
        # failure before propagating, keeping the raw-connection contract intact.
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            # Concurrent writers (scheduled sync + context-import + collectors)
            # otherwise race and raise "database is locked". A generous busy
            # timeout makes each writer wait for the lock instead of failing.
            # Override via HERMES_CONTEXT_DB_BUSY_TIMEOUT_MS if needed.
            _busy_ms = _context_db_busy_timeout_ms()
            conn.execute(f"PRAGMA busy_timeout={_busy_ms}")
            conn.execute("PRAGMA foreign_keys=ON")
            _chmod_private_file(self.db_path)
            _chmod_sqlite_sidecars(self.db_path)
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def closing_connection(self) -> Iterable[sqlite3.Connection]:
        """Commit/rollback and always close an internally owned connection."""

        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.closing_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS context_events(
                  event_id TEXT PRIMARY KEY,
                  identity_key TEXT NOT NULL UNIQUE,
                  platform TEXT NOT NULL,
                  account TEXT NOT NULL DEFAULT '',
                  workspace TEXT NOT NULL DEFAULT '',
                  action_target_id TEXT NOT NULL DEFAULT '',
                  action_account_id TEXT NOT NULL DEFAULT '',
                  conversation_id TEXT NOT NULL DEFAULT '',
                  conversation_name TEXT NOT NULL DEFAULT '',
                  conversation_type TEXT NOT NULL DEFAULT '',
                  sender_id TEXT NOT NULL DEFAULT '',
                  sender_display_name TEXT NOT NULL DEFAULT '',
                  direction TEXT NOT NULL DEFAULT 'unknown',
                  body TEXT NOT NULL DEFAULT '',
                  message_ts TEXT NOT NULL DEFAULT '',
                  received_ts TEXT NOT NULL DEFAULT '',
                  ingested_ts TEXT NOT NULL DEFAULT '',
                  source_message_id TEXT NOT NULL DEFAULT '',
                  thread_id TEXT NOT NULL DEFAULT '',
                  permalink TEXT NOT NULL DEFAULT '',
                  attachment_metadata_json TEXT NOT NULL DEFAULT '[]',
                  raw_json TEXT NOT NULL DEFAULT '{}',
                  source_path TEXT NOT NULL DEFAULT '',
                  flags TEXT NOT NULL DEFAULT '[]',
                  relevance_score INTEGER NOT NULL DEFAULT 0,
                  relevance_tier TEXT NOT NULL DEFAULT 'archive',
                  relevance_reasons TEXT NOT NULL DEFAULT '[]',
                  processing_state TEXT NOT NULL DEFAULT 'new',
                  source_generation INTEGER NOT NULL DEFAULT 0,
                  source_tombstoned INTEGER NOT NULL DEFAULT 0,
                  updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_import_status(
                  source TEXT PRIMARY KEY,
                  status TEXT NOT NULL,
                  message TEXT NOT NULL DEFAULT '',
                  updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_reminders(
                  reminder_id TEXT PRIMARY KEY,
                  source_event_id TEXT NOT NULL,
                  platform TEXT NOT NULL,
                  conversation_id TEXT NOT NULL,
                  text TEXT NOT NULL,
                  due_hint TEXT NOT NULL DEFAULT '',
                  confidence REAL NOT NULL,
                  status TEXT NOT NULL DEFAULT 'candidate',
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(source_event_id) REFERENCES context_events(event_id)
                );
                CREATE TABLE IF NOT EXISTS context_habit_hypotheses(
                  habit_id TEXT PRIMARY KEY,
                  hypothesis TEXT NOT NULL,
                  evidence_count INTEGER NOT NULL,
                  day_count INTEGER NOT NULL,
                  confidence REAL NOT NULL,
                  status TEXT NOT NULL,
                  evidence_event_ids TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_notifications(
                  notification_id TEXT PRIMARY KEY,
                  source_type TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  fingerprint TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL,
                  initialized_at TEXT NOT NULL DEFAULT '',
                  delivered_at TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_temporary_memory(
                  record_id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  kind TEXT NOT NULL CHECK(kind IN ('context','episode')),
                  text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 4000),
                  source_ref TEXT NOT NULL DEFAULT '' CHECK(length(source_ref)<=500),
                  created_at TEXT NOT NULL,
                  created_at_epoch REAL NOT NULL,
                  expires_at TEXT NOT NULL,
                  expires_at_epoch REAL NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('active','expired')),
                  expired_at TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_intake_bundles(
                  staging_bundle_id TEXT PRIMARY KEY,
                  identity_key TEXT NOT NULL UNIQUE,
                  schema_version INTEGER NOT NULL CHECK(schema_version=1),
                  payload_sha256 TEXT NOT NULL,
                  source_ref TEXT NOT NULL DEFAULT '' CHECK(length(source_ref)<=500),
                  item_count INTEGER NOT NULL CHECK(item_count BETWEEN 1 AND 50),
                  pending_approval_count INTEGER NOT NULL CHECK(pending_approval_count BETWEEN 0 AND item_count),
                  status TEXT NOT NULL CHECK(status IN ('staged','pending_approval')),
                  confirmation TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_intake_items(
                  staging_item_id TEXT PRIMARY KEY,
                  staging_bundle_id TEXT NOT NULL,
                  source_item_id TEXT NOT NULL,
                  position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 49),
                  summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 180),
                  content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 4000),
                  destination TEXT NOT NULL CHECK(destination IN ('reminder','note','memory_fact','temporary_context','routine','personal_task','hermes_work_order')),
                  action TEXT NOT NULL,
                  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                  requires_approval INTEGER NOT NULL CHECK(requires_approval IN (0,1)),
                  sensitive INTEGER NOT NULL CHECK(sensitive IN (0,1)),
                  external INTEGER NOT NULL CHECK(external IN (0,1)),
                  status TEXT NOT NULL CHECK(status IN ('staged','pending_approval')),
                  execution_status TEXT NOT NULL CHECK(execution_status='not_executed'),
                  due_hint TEXT NOT NULL DEFAULT '' CHECK(length(due_hint)<=200),
                  expires_at TEXT NOT NULL DEFAULT '',
                  source_ref TEXT NOT NULL DEFAULT '' CHECK(length(source_ref)<=500),
                  created_at TEXT NOT NULL,
                  UNIQUE(staging_bundle_id,source_item_id),
                  FOREIGN KEY(staging_bundle_id) REFERENCES context_intake_bundles(staging_bundle_id)
                );
                """
            )
            self._migrate_action_binding_columns(conn)
            self._migrate_source_reconciliation_columns(conn)
            self._migrate_context_notifications_columns(conn)
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_context_events_identity ON context_events(platform, account, source_message_id);
                CREATE INDEX IF NOT EXISTS idx_context_events_message_ts ON context_events(message_ts);
                CREATE INDEX IF NOT EXISTS idx_context_events_conversation ON context_events(platform, account, workspace, conversation_id);
                CREATE INDEX IF NOT EXISTS idx_context_events_relevance ON context_events(relevance_tier, relevance_score);
                CREATE INDEX IF NOT EXISTS idx_context_reminders_status ON context_reminders(status, confidence);
                CREATE INDEX IF NOT EXISTS idx_context_notifications_source ON context_notifications(source_type, source_id, status);
                CREATE INDEX IF NOT EXISTS idx_context_temporary_active ON context_temporary_memory(status, expires_at_epoch);
                CREATE INDEX IF NOT EXISTS idx_context_intake_bundle ON context_intake_items(staging_bundle_id, position);
                CREATE INDEX IF NOT EXISTS idx_context_intake_status ON context_intake_items(status, destination);
                """
            )
            self._migrate_source_identity_keys(conn)
            self._migrate_legacy_possibly_addressed_reminders(conn)

    def _migrate_action_binding_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(context_events)")}
        if "action_target_id" not in columns:
            conn.execute(
                "ALTER TABLE context_events ADD COLUMN action_target_id TEXT NOT NULL DEFAULT ''"
            )
        if "action_account_id" not in columns:
            conn.execute(
                "ALTER TABLE context_events ADD COLUMN action_account_id TEXT NOT NULL DEFAULT ''"
            )

    def _migrate_source_reconciliation_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(context_events)")}
        if "source_generation" not in columns:
            conn.execute("ALTER TABLE context_events ADD COLUMN source_generation INTEGER NOT NULL DEFAULT 0")
        if "source_tombstoned" not in columns:
            conn.execute("ALTER TABLE context_events ADD COLUMN source_tombstoned INTEGER NOT NULL DEFAULT 0")

    def _migrate_context_notifications_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(context_notifications)")}
        if "initialized_at" not in columns:
            conn.execute("ALTER TABLE context_notifications ADD COLUMN initialized_at TEXT NOT NULL DEFAULT ''")
        if "delivered_at" not in columns:
            conn.execute("ALTER TABLE context_notifications ADD COLUMN delivered_at TEXT NOT NULL DEFAULT ''")
        if "claim_owner" not in columns:
            conn.execute("ALTER TABLE context_notifications ADD COLUMN claim_owner TEXT NOT NULL DEFAULT ''")
        if "claim_until" not in columns:
            conn.execute("ALTER TABLE context_notifications ADD COLUMN claim_until REAL NOT NULL DEFAULT 0")
        if "emitted_at" not in columns:
            conn.execute("ALTER TABLE context_notifications ADD COLUMN emitted_at TEXT NOT NULL DEFAULT ''")

    def _migrate_legacy_possibly_addressed_reminders(self, conn: sqlite3.Connection) -> None:
        # Older releases persisted a weak outbound-message heuristic as durable
        # completion state. Keep completion inference display-only and reversible.
        conn.execute("UPDATE context_reminders SET status='candidate' WHERE status='possibly_addressed'")

    def _migrate_source_identity_keys(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT event_id,identity_key,platform,account,workspace,conversation_id,source_message_id,message_ts,body,sender_id
            FROM context_events
            WHERE identity_key LIKE 'source|%' OR identity_key LIKE 'source/%'
            """
        ).fetchall()
        for row in rows:
            new_key = build_identity_key(
                row["platform"],
                row["account"],
                row["conversation_id"],
                row["source_message_id"],
                row["message_ts"],
                row["body"],
                row["sender_id"],
                workspace=row["workspace"],
            )
            if new_key == row["identity_key"]:
                continue
            try:
                conn.execute("UPDATE context_events SET identity_key=? WHERE event_id=?", (new_key, row["event_id"]))
            except sqlite3.IntegrityError:
                # If a newer row already owns the recomputed identity, keep the
                # existing row untouched rather than deleting reminder provenance.
                continue

    def upsert_event(self, event: dict[str, Any]) -> str:
        with self.connect() as conn:
            return self._upsert_event_on_connection(conn, event)

    def upsert_events(self, events: Iterable[dict[str, Any]]) -> list[str]:
        """Upsert canonical events in one transaction.

        Collectors with large local backfills use this API so they retain the
        exact normalization/identity/update behavior of :meth:`upsert_event`
        without opening and committing one SQLite connection per source row.
        """
        event_ids: list[str] = []
        with self.connect() as conn:
            for event in events:
                event_ids.append(self._upsert_event_on_connection(conn, event))
        return event_ids

    def upsert_derivatives(self, events: Iterable[dict[str, Any]]) -> list[str]:
        """Persist already-minimized scanner derivatives in one transaction.

        Scanner adapters must put provenance, not source payloads, in ``raw``.
        This narrow alias documents that trust boundary while preserving the
        canonical event normalization and identity behavior.
        """
        prepared: list[dict[str, Any]] = []
        for event in events:
            self._validate_derivative(event)
            prepared.append(event)
        return self.upsert_events(prepared)

    @staticmethod
    def _validate_derivative(event: dict[str, Any]) -> None:
        provenance_keys = {"source", "redaction_version", "kind", "transport", "import", "schema_fingerprint"}
        if not isinstance(event, dict):
            raise TypeError("context derivative must be an object")
        raw = event.get("raw", {})
        if (
            not isinstance(raw, dict)
            or "source" not in raw
            or set(raw) - provenance_keys
            or any(not isinstance(value, (str, int, float, bool, type(None))) or len(str(value)) > 256 for value in raw.values())
            or len(stable_json(raw)) > 2048
        ):
            raise ValueError("context derivative requires bounded provenance")

    def apply_derivative_changes(self, changes: Iterable[tuple[dict[str, Any], bool, int]]) -> list[str]:
        """Apply scanner upserts/tombstones while retaining reminder provenance."""
        affected: list[str] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for event, tombstone, generation in changes:
                self._validate_derivative(event)
                bounded_generation = max(0, min(int(generation), 2**63 - 1))
                if tombstone:
                    normalized = normalize_event(event)
                    row = conn.execute("SELECT event_id FROM context_events WHERE identity_key=?", (normalized["identity_key"],)).fetchone()
                    if row is None:
                        continue
                    event_id = str(row["event_id"])
                    conn.execute(
                        """UPDATE context_events SET source_generation=?,source_tombstoned=1,
                           processing_state='tombstoned',relevance_score=0,relevance_tier='archive',relevance_reasons='[]',updated_at=?
                           WHERE event_id=?""",
                        (bounded_generation, time.time(), event_id),
                    )
                else:
                    event_id = self._upsert_event_on_connection(conn, event)
                    conn.execute(
                        """UPDATE context_events SET source_generation=?,source_tombstoned=0,
                           processing_state=CASE WHEN source_tombstoned=1 THEN 'new' ELSE processing_state END
                           WHERE event_id=?""",
                        (bounded_generation, event_id),
                    )
                affected.append(event_id)
        return affected

    def _upsert_event_on_connection(self, conn: sqlite3.Connection, event: dict[str, Any]) -> str:
        normalized = normalize_event(event)
        now = time.time()
        normalized["updated_at"] = now
        preserve_existing_ingested = not any(key in event and event.get(key) not in (None, "") for key in ("ingested_ts", "ingested_at"))
        columns = [
            "event_id",
            "identity_key",
            "platform",
            "account",
            "workspace",
            "action_target_id",
            "action_account_id",
            "conversation_id",
            "conversation_name",
            "conversation_type",
            "sender_id",
            "sender_display_name",
            "direction",
            "body",
            "message_ts",
            "received_ts",
            "ingested_ts",
            "source_message_id",
            "thread_id",
            "permalink",
            "attachment_metadata_json",
            "raw_json",
            "source_path",
            "flags",
            "relevance_score",
            "relevance_tier",
            "relevance_reasons",
            "processing_state",
            "updated_at",
        ]
        placeholders = ",".join("?" for _ in columns)
        data_columns = [c for c in columns if c not in {"event_id", "identity_key", "relevance_score", "relevance_tier", "relevance_reasons", "processing_state", "updated_at"}]
        if preserve_existing_ingested:
            data_columns = [c for c in data_columns if c != "ingested_ts"]
        updates = ",".join([f"{c}=excluded.{c}" for c in data_columns] + ["relevance_score=0", "relevance_tier='archive'", "relevance_reasons='[]'", "processing_state='new'", "updated_at=excluded.updated_at"])
        changed_where = " OR ".join(f"context_events.{c} IS NOT excluded.{c}" for c in data_columns)
        conn.execute(
            f"""
            INSERT INTO context_events({','.join(columns)}) VALUES({placeholders})
            ON CONFLICT(identity_key) DO UPDATE SET {updates}
            WHERE {changed_where}
            """,
            [normalized[c] for c in columns],
        )
        row = conn.execute("SELECT event_id FROM context_events WHERE identity_key=?", (normalized["identity_key"],)).fetchone()
        return str(row["event_id"])

    def list_events(self, where: str = "", params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        sql = "SELECT * FROM context_events"
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY message_ts DESC, ingested_ts DESC"
        with self.closing_connection() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(params))]

    def set_import_status(self, source: str, status: str, message: str = "") -> None:
        with self.closing_connection() as conn:
            conn.execute(
                """
                INSERT INTO context_import_status(source,status,message,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET status=excluded.status,message=excluded.message,updated_at=excluded.updated_at
                """,
                (source, status, message, time.time()),
            )

    def import_status(self, source: str) -> dict[str, Any]:
        with self.closing_connection() as conn:
            row = conn.execute("SELECT * FROM context_import_status WHERE source=?", (source,)).fetchone()
        return dict(row) if row else {"source": source, "status": "unknown", "message": ""}

    def rank_all(self) -> list[RankedEvent]:
        ranked: list[RankedEvent] = []
        with self.closing_connection() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM context_events WHERE source_tombstoned=0")]
            for row in rows:
                score, tier, reasons = score_event(row)
                ranked.append(RankedEvent(row["event_id"], score, tier, tuple(reasons)))
                conn.execute(
                    "UPDATE context_events SET relevance_score=?, relevance_tier=?, relevance_reasons=?, processing_state=? WHERE event_id=?",
                    (score, tier, json.dumps(reasons), "ranked", row["event_id"]),
                )
        return ranked

    def extract_reminders(self) -> list[dict[str, Any]]:
        reminders: list[dict[str, Any]] = []
        created = now_iso()
        with self.closing_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = [dict(row) for row in conn.execute("SELECT * FROM context_events WHERE source_tombstoned=0")]
            for row in rows:
                candidate = reminder_candidate(row)
                if not candidate:
                    conn.execute("UPDATE context_reminders SET status='superseded' WHERE source_event_id=? AND status='candidate'", (row["event_id"],))
                    continue
                reminder_id = "rem_" + hashlib.sha256((row["event_id"] + candidate["text"]).encode("utf-8")).hexdigest()[:24]
                record = {
                    "reminder_id": reminder_id,
                    "source_event_id": row["event_id"],
                    "platform": row["platform"],
                    "conversation_id": row["conversation_id"],
                    "text": candidate["text"],
                    "due_hint": candidate["due_hint"],
                    "confidence": candidate["confidence"],
                    "status": "candidate",
                    "created_at": created,
                }
                conn.execute(
                    """
                    INSERT INTO context_reminders(reminder_id,source_event_id,platform,conversation_id,text,due_hint,confidence,status,created_at)
                    VALUES(:reminder_id,:source_event_id,:platform,:conversation_id,:text,:due_hint,:confidence,:status,:created_at)
                    ON CONFLICT(reminder_id) DO UPDATE SET text=excluded.text,due_hint=excluded.due_hint,confidence=excluded.confidence
                    """,
                    record,
                )
                conn.execute(
                    "UPDATE context_reminders SET status='superseded' WHERE source_event_id=? AND status='candidate' AND reminder_id<>?",
                    (row["event_id"], reminder_id),
                )
                reminders.append(record)
        return reminders

    def get_reminder(self, reminder_id: str) -> dict[str, Any]:
        with self.closing_connection() as conn:
            row = conn.execute("SELECT * FROM context_reminders WHERE reminder_id=?", (reminder_id,)).fetchone()
        if row is None:
            raise KeyError(reminder_id)
        return dict(row)

    def set_reminder_status(self, reminder_id: str, status: str) -> dict[str, Any]:
        if status not in {"done", "later", "dismissed", "candidate", "superseded"}:
            raise ValueError(f"unsupported reminder status {status!r}")
        with self.closing_connection() as conn:
            row = conn.execute("SELECT reminder_id FROM context_reminders WHERE reminder_id=?", (reminder_id,)).fetchone()
            if row is None:
                raise KeyError(reminder_id)
            conn.execute("UPDATE context_reminders SET status=? WHERE reminder_id=?", (status, reminder_id))
        return self.get_reminder(reminder_id)

    def promote_habit_hypotheses(self) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in self.list_events("source_tombstoned=0"):
            phrase = habit_phrase(row.get("body", ""))
            if phrase:
                groups.setdefault(phrase, []).append(row)
        promoted: list[dict[str, Any]] = []
        stamp = now_iso()
        with self.closing_connection() as conn:
            for phrase, rows in groups.items():
                days = {r["message_ts"][:10] for r in rows if r.get("message_ts")}
                if len(rows) < 3 or len(days) < 2:
                    continue
                event_ids = sorted({r["event_id"] for r in rows})
                confidence = min(0.95, 0.45 + 0.1 * len(rows) + 0.05 * len(days))
                habit_id = "habit_" + hashlib.sha256(phrase.encode("utf-8")).hexdigest()[:24]
                record = {
                    "habit_id": habit_id,
                    "hypothesis": phrase,
                    "evidence_count": len(event_ids),
                    "day_count": len(days),
                    "confidence": round(confidence, 3),
                    "status": "hypothesis",
                    "evidence_event_ids": json.dumps(event_ids),
                    "created_at": stamp,
                    "updated_at": stamp,
                }
                conn.execute(
                    """
                    INSERT INTO context_habit_hypotheses(habit_id,hypothesis,evidence_count,day_count,confidence,status,evidence_event_ids,created_at,updated_at)
                    VALUES(:habit_id,:hypothesis,:evidence_count,:day_count,:confidence,:status,:evidence_event_ids,:created_at,:updated_at)
                    ON CONFLICT(habit_id) DO UPDATE SET evidence_count=excluded.evidence_count,day_count=excluded.day_count,confidence=excluded.confidence,status=excluded.status,evidence_event_ids=excluded.evidence_event_ids,updated_at=excluded.updated_at
                    """,
                    record,
                )
                promoted.append(record)
        return promoted

    def brief(self) -> dict[str, list[dict[str, Any]]]:
        self.rank_all()
        grouped = {"immediate": [], "briefing": [], "archive": []}
        for row in self.list_events():
            item = {
                "event_id": row["event_id"],
                "platform": row["platform"],
                "conversation": row["conversation_name"] or row["conversation_id"],
                "sender": row["sender_display_name"] or row["sender_id"],
                "message_ts": row["message_ts"],
                "score": row["relevance_score"],
                "reasons": json.loads(row["relevance_reasons"] or "[]"),
                "body": redact_sensitive_text(row["body"])[:500],
            }
            grouped.get(row["relevance_tier"], grouped["archive"]).append(item)
        for key in grouped:
            grouped[key].sort(key=lambda r: (-int(r["score"]), r["message_ts"]), reverse=False)
        return grouped

    def claim_alerts(self, initialize: bool = False, lease_seconds: float = 30.0, owner: str | None = None, *, prepared: bool = False) -> dict[str, Any]:
        """Claim alerts, reusing ranks/reminders only after a successful preparation."""
        if not prepared:
            self.rank_all()
            self.extract_reminders()
        candidates = self._alert_candidates()
        stamp = now_iso()
        owner = owner or f"alerts-{os.getpid()}-{time.time_ns()}"
        lease_until = time.time() + max(1.0, lease_seconds)
        inserted: list[dict[str, Any]] = []
        with self.closing_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if initialize:
                for candidate in candidates:
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO context_notifications(notification_id,source_type,source_id,fingerprint,status,initialized_at,created_at)
                        VALUES(?,?,?,?,?,?,?)
                        """,
                        (candidate["notification_id"], "event", candidate["event_id"], candidate["fingerprint"], "initialized", stamp, stamp),
                    )
                    if cursor.rowcount:
                        inserted.append(candidate)
                return {"initialized": len(inserted), "alerts": []}
            for candidate in candidates:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO context_notifications(notification_id,source_type,source_id,fingerprint,status,claim_owner,claim_until,created_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (candidate["notification_id"], "event", candidate["event_id"], candidate["fingerprint"], "claimed", owner, lease_until, stamp),
                )
                if cursor.rowcount:
                    inserted.append(candidate)
                    continue
                cursor = conn.execute(
                    """
                    UPDATE context_notifications
                    SET status='claimed', claim_owner=?, claim_until=?
                    WHERE fingerprint=? AND status='claimed' AND claim_until<?
                    """,
                    (owner, lease_until, candidate["fingerprint"], time.time()),
                )
                if cursor.rowcount:
                    inserted.append(candidate)
            alerts = [{"event_id": c["event_id"], "fingerprint": c["fingerprint"], "text": c["text"], "reminder_id": c.get("reminder_id", "")} for c in inserted]
        return {"initialized": 0, "owner": owner, "alerts": alerts}

    def mark_alerts_emitted(self, fingerprints: Iterable[str], owner: str) -> int:
        stamp = now_iso()
        count = 0
        with self.closing_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for fingerprint in fingerprints:
                cursor = conn.execute(
                    """
                    UPDATE context_notifications
                    SET status='emitted', emitted_at=?, delivered_at=?
                    WHERE fingerprint=? AND status='claimed' AND claim_owner=?
                    """,
                    (stamp, stamp, fingerprint, owner),
                )
                count += cursor.rowcount
        return count

    def _alert_candidates(self) -> list[dict[str, Any]]:
        with self.closing_connection() as conn:
            rows = [dict(row) for row in conn.execute(
                """
                SELECT e.*, r.reminder_id, r.due_hint AS reminder_due_hint, r.text AS reminder_text, r.status AS reminder_status
                FROM context_events e
                LEFT JOIN context_reminders r ON r.source_event_id=e.event_id AND r.status='candidate'
                WHERE e.relevance_tier='immediate' AND e.direction='inbound'
                ORDER BY e.message_ts ASC, e.event_id ASC
                """
            )]
        by_event: dict[str, dict[str, Any]] = {}
        for row in rows:
            by_event.setdefault(row["event_id"], row)
        candidates = []
        for row in by_event.values():
            body = _safe_public_body(row["body"], 220)
            due = str(row.get("reminder_due_hint") or "")
            conversation = row["conversation_name"] or row["conversation_id"] or row["platform"]
            sender = row["sender_display_name"] or row["sender_id"] or "Unbekannt"
            prefix = f"{row['platform']} / {conversation} / {sender}"
            text = f"{prefix}: {body}"
            if due:
                text += f"\nFaelligkeit: {due}"
            fingerprint = "event:" + row["event_id"]
            candidates.append(
                {
                    "notification_id": "ntf_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24],
                    "fingerprint": fingerprint,
                    "event_id": row["event_id"],
                    "reminder_id": row.get("reminder_id") or "",
                    "text": text,
                }
            )
        return candidates

    def daily_brief(
        self,
        hours: int = 24,
        now: str | None = None,
        event_limit: int = 20,
        reminder_limit: int = 10,
        habit_limit: int = 10,
        included_sections: Iterable[str] | None = None,
        excluded_platforms: Iterable[str] = (),
        quiet_when_empty: bool = True,
    ) -> dict[str, Any]:
        if hours < 0:
            raise ValueError("hours must be nonnegative")
        for name, value in (
            ("event_limit", event_limit),
            ("reminder_limit", reminder_limit),
            ("habit_limit", habit_limit),
        ):
            if type(value) is not int or not MIN_SECTION_LIMIT <= value <= MAX_SECTION_LIMIT:
                raise ValueError(
                    f"{name} must be an integer from {MIN_SECTION_LIMIT} to {MAX_SECTION_LIMIT}"
                )
        sections = tuple(included_sections) if included_sections is not None else ("events", "reminders", "habits")
        if len(sections) != len(set(sections)) or any(section not in {"events", "reminders", "habits"} for section in sections):
            raise ValueError("included_sections supports unique events, reminders, and habits values")
        if type(quiet_when_empty) is not bool:
            raise ValueError("quiet_when_empty must be a boolean")
        excluded = tuple(sorted({str(platform).strip().lower() for platform in excluded_platforms if str(platform).strip()}))
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", platform) for platform in excluded):
            raise ValueError("excluded_platforms contains an unsafe platform identifier")
        self.rank_all()
        self.extract_reminders()
        self.promote_habit_hypotheses()
        now_dt = parse_iso(now) if now else dt.datetime.now(dt.timezone.utc)
        start = (now_dt - dt.timedelta(hours=hours)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        now_bound = _iso_z(now_dt)
        with self.closing_connection() as conn:
            placeholders = ",".join("?" for _ in excluded)
            event_platform_filter = f" AND platform NOT IN ({placeholders})" if excluded else ""
            reminder_platform_filter = f" AND e.platform NOT IN ({placeholders})" if excluded else ""
            habit_platform_filter = (
                " AND NOT EXISTS ("
                "SELECT 1 FROM context_events e "
                f"WHERE e.platform IN ({placeholders}) "
                "AND instr(context_habit_hypotheses.evidence_event_ids, "
                "char(34) || e.event_id || char(34))>0)"
                if excluded
                else ""
            )
            if "events" in sections:
                event_rows = [dict(row) for row in conn.execute(
                    """
                    SELECT event_id,platform,conversation_name,conversation_id,sender_display_name,sender_id,message_ts,relevance_score,relevance_tier,relevance_reasons,body
                    FROM context_events
                    WHERE message_ts>=? AND message_ts<=? AND relevance_tier IN ('immediate','briefing')
                    """ + event_platform_filter + """
                    ORDER BY relevance_score DESC, message_ts DESC
                    LIMIT ?
                    """,
                    (start, now_bound, *excluded, event_limit),
                )]
                event_total = conn.execute(
                    "SELECT COUNT(*) FROM context_events WHERE message_ts>=? AND message_ts<=? AND relevance_tier IN ('immediate','briefing')" + event_platform_filter,
                    (start, now_bound, *excluded),
                ).fetchone()[0]
            else:
                event_rows = []
                event_total = 0
            if "reminders" in sections:
                reminder_rows = [dict(row) for row in conn.execute(
                    """
                    SELECT r.*, e.account, e.message_ts, e.sender_display_name, e.body, e.conversation_name
                    FROM context_reminders r
                    JOIN context_events e ON e.event_id=r.source_event_id
                    WHERE r.status IN ('candidate','later') AND e.message_ts<=?
                    """ + reminder_platform_filter + """
                    ORDER BY r.confidence DESC, e.message_ts DESC
                    LIMIT ?
                    """,
                    (now_bound, *excluded, reminder_limit),
                )]
                reminder_total = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM context_reminders r
                    JOIN context_events e ON e.event_id=r.source_event_id
                    WHERE r.status IN ('candidate','later') AND e.message_ts<=?
                    """ + reminder_platform_filter,
                    (now_bound, *excluded),
                ).fetchone()[0]
            else:
                reminder_rows = []
                reminder_total = 0
            if "habits" in sections:
                habit_rows = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM context_habit_hypotheses WHERE status='hypothesis'"
                        + habit_platform_filter
                        + " ORDER BY confidence DESC LIMIT ?",
                        (*excluded, habit_limit),
                    )
                ]
                habit_total = conn.execute(
                    "SELECT COUNT(*) FROM context_habit_hypotheses WHERE status='hypothesis'"
                    + habit_platform_filter,
                    excluded,
                ).fetchone()[0]
            else:
                habit_rows = []
                habit_total = 0
            for reminder in reminder_rows:
                outbound = conn.execute(
                    """
                    SELECT event_id,message_ts FROM context_events
                    WHERE platform=? AND account=? AND conversation_id=? AND direction='outbound' AND message_ts>? AND message_ts<=?
                    ORDER BY message_ts ASC LIMIT 1
                    """,
                    (reminder["platform"], reminder["account"], reminder["conversation_id"], reminder["message_ts"], now_bound),
                ).fetchone()
                if outbound and reminder["status"] == "candidate":
                    reminder["status"] = "possibly_addressed"
                    reminder["possibly_addressed_by_event_id"] = outbound["event_id"]
        return {
            "hours": hours,
            "local_date": now_dt.astimezone().date().isoformat(),
            "included_sections": list(sections),
            "excluded_platforms": list(excluded),
            "quiet_when_empty": quiet_when_empty,
            "caps": {"events": event_limit, "reminders": reminder_limit, "habits": habit_limit},
            "omitted": {
                "events": max(0, int(event_total) - len(event_rows)),
                "reminders": max(0, int(reminder_total) - len(reminder_rows)),
                "habits": max(0, int(habit_total) - len(habit_rows)),
            },
            "events": [
                {
                    "event_id": row["event_id"],
                    "platform": row["platform"],
                    "conversation": row["conversation_name"] or row["conversation_id"],
                    "sender": row["sender_display_name"] or row["sender_id"],
                    "message_ts": row["message_ts"],
                    "score": row["relevance_score"],
                    "tier": row["relevance_tier"],
                    "reasons": json.loads(row["relevance_reasons"] or "[]"),
                    "body": _safe_public_body(row["body"], 260),
                }
                for row in event_rows
            ],
            "reminders": [
                {
                    "reminder_id": row["reminder_id"],
                    "source_event_id": row["source_event_id"],
                    "platform": row["platform"],
                    "conversation": row.get("conversation_name") or row["conversation_id"],
                    "text": _safe_public_body(row["text"], 260),
                    "due_hint": row["due_hint"],
                    "confidence": row["confidence"],
                    "status": row["status"],
                    "possibly_addressed_by_event_id": row.get("possibly_addressed_by_event_id", ""),
                }
                for row in reminder_rows
            ],
            "habits": [
                {
                    "habit_id": row["habit_id"],
                    "hypothesis": _safe_public_body(row["hypothesis"], 220),
                    "evidence_count": row["evidence_count"],
                    "day_count": row["day_count"],
                    "confidence": row["confidence"],
                }
                for row in habit_rows
            ],
        }

    def format_daily_brief(self, brief: dict[str, Any], max_output_characters: int = 3900) -> str:
        if type(max_output_characters) is not int or not 100 <= max_output_characters <= 20_000:
            raise ValueError("max_output_characters must be an integer from 100 to 20000")
        lines: list[str] = []
        if brief["events"]:
            lines.append("Kontext der letzten Stunden")
            for event in brief["events"][:8]:
                lines.append(f"- [{event['tier']}/{event['score']}] {event['platform']} / {event['conversation']} / {event['sender']}: {event['body']}")
        active_reminders = [r for r in brief["reminders"] if r["status"] == "candidate"]
        addressed = [r for r in brief["reminders"] if r["status"] == "possibly_addressed"]
        deferred = [r for r in brief["reminders"] if r["status"] == "later"]
        if active_reminders:
            lines.append("Offene Erinnerungen")
            for reminder in active_reminders[:8]:
                suffix = f" (Faelligkeit: {reminder['due_hint']})" if reminder["due_hint"] else ""
                lines.append(f"- {reminder['platform']} / {reminder['conversation']}: {reminder['text']}{suffix}")
        if addressed:
            lines.append("Moeglicherweise erledigt")
            for reminder in addressed[:3]:
                lines.append(f"- {reminder['platform']} / {reminder['conversation']}: {reminder['text']}")
        if deferred:
            lines.append("Spaeter")
            for reminder in deferred[:3]:
                lines.append(f"- {reminder['platform']} / {reminder['conversation']}: {reminder['text']}")
        if brief["habits"]:
            lines.append("Gewohnheits-Hypothesen")
            for habit in brief["habits"][:3]:
                lines.append(f"- {habit['hypothesis']} ({habit['evidence_count']} Hinweise)")
        omitted = brief.get("omitted") or {}
        hidden_events = max(0, len(brief.get("events", [])) - 8)
        hidden_reminders = max(0, len(active_reminders) - 8) + max(0, len(addressed) - 3) + max(0, len(deferred) - 3)
        hidden_habits = max(0, len(brief.get("habits", [])) - 3)
        event_omissions = int(omitted.get("events") or 0) + hidden_events
        reminder_omissions = int(omitted.get("reminders") or 0) + hidden_reminders
        habit_omissions = int(omitted.get("habits") or 0) + hidden_habits
        if event_omissions:
            lines.append(f"{event_omissions} weitere Kontext-Ereignisse bleiben durchsuchbar.")
        if reminder_omissions:
            lines.append(f"{reminder_omissions} weitere Erinnerungen bleiben durchsuchbar.")
        if habit_omissions:
            lines.append(f"{habit_omissions} weitere Gewohnheits-Hypothesen bleiben durchsuchbar.")
        if not lines and not brief.get("quiet_when_empty", True):
            lines.append("Keine neuen Ereignisse, Erinnerungen oder Gewohnheits-Hypothesen.")
        return "\n".join(lines)[:max_output_characters]

    def claim_daily_brief(
        self,
        brief: dict[str, Any],
        force: bool = False,
        lease_seconds: float = 30.0,
        owner: str | None = None,
    ) -> str | None:
        if force:
            return "force"
        if (
            not brief.get("events")
            and not brief.get("reminders")
            and not brief.get("habits")
            and brief.get("quiet_when_empty", True)
        ):
            return None
        local_date = str(brief.get("local_date") or "")
        fingerprint = f"daily:{local_date}"
        notification_id = "ntf_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        stamp = now_iso()
        now_epoch = time.time()
        claim_owner = owner or f"daily-{os.getpid()}-{time.time_ns()}"
        claim_until = now_epoch + max(1.0, lease_seconds)
        with self.closing_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT notification_id,status,claim_until FROM context_notifications WHERE source_type='daily_brief' AND source_id=? ORDER BY created_at LIMIT 1",
                (local_date,),
            ).fetchone()
            if existing is not None:
                status = str(existing["status"] or "")
                if status in {"delivered", "emitted", "initialized"}:
                    return None
                if status == "claimed" and float(existing["claim_until"] or 0) > now_epoch:
                    return None
                conn.execute(
                    "UPDATE context_notifications SET status='claimed',claim_owner=?,claim_until=?,emitted_at='' WHERE notification_id=?",
                    (claim_owner, claim_until, existing["notification_id"]),
                )
                return claim_owner
            conn.execute(
                """
                INSERT INTO context_notifications(notification_id,source_type,source_id,fingerprint,status,delivered_at,created_at,claim_owner,claim_until,emitted_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (notification_id, "daily_brief", local_date, fingerprint, "claimed", "", stamp, claim_owner, claim_until, ""),
            )
            return claim_owner

    def mark_daily_brief_emitted(self, owner: str) -> int:
        if not owner or owner == "force":
            return 0
        stamp = now_iso()
        with self.closing_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE context_notifications
                SET status='emitted',emitted_at=?,delivered_at=?,claim_owner='',claim_until=0
                WHERE source_type='daily_brief' AND status='claimed' AND claim_owner=?
                """,
                (stamp, stamp, owner),
            )
            return int(cursor.rowcount)

    def export_openviking(self, output: Path) -> int:
        self.rank_all()
        self.extract_reminders()
        self.promote_habit_hypotheses()
        _prepare_private_file_parent(output.parent)
        count = 0
        with self.closing_connection() as conn, atomic_text_writer(output) as fh:
            events = conn.execute(
                """
                SELECT event_id,platform,conversation_name,sender_display_name,message_ts,body,relevance_score,relevance_tier,relevance_reasons
                FROM context_events WHERE relevance_tier IN ('immediate','briefing') ORDER BY relevance_score DESC, message_ts DESC
                """
            )
            for row in events:
                data = dict(row)
                data["body"] = redact_sensitive_text(data["body"])
                data["conversation_name"] = _safe_public_body(data["conversation_name"], 120)
                data["sender_display_name"] = _safe_public_body(data["sender_display_name"], 120)
                data["relevance_reasons"] = json.loads(data["relevance_reasons"] or "[]")
                data["kind"] = "context_event_summary"
                fh.write(json.dumps(data, sort_keys=True) + "\n")
                count += 1
            for row in conn.execute("SELECT reminder_id,source_event_id,platform,text,due_hint,confidence,status,created_at FROM context_reminders WHERE status='candidate' ORDER BY confidence DESC"):
                data = {
                    "kind": "context_reminder_candidate",
                    "reminder_id": row["reminder_id"],
                    "source_event_id": row["source_event_id"],
                    "platform": row["platform"],
                    "text": redact_sensitive_text(row["text"]),
                    "due_hint": redact_sensitive_text(row["due_hint"]),
                    "confidence": row["confidence"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                }
                fh.write(json.dumps(data, sort_keys=True) + "\n")
                count += 1
            for row in conn.execute("SELECT habit_id,hypothesis,evidence_count,day_count,confidence,status,evidence_event_ids,created_at,updated_at FROM context_habit_hypotheses WHERE status='hypothesis' ORDER BY confidence DESC"):
                data = {
                    "kind": "context_habit_hypothesis",
                    "habit_id": row["habit_id"],
                    "hypothesis": redact_sensitive_text(row["hypothesis"]),
                    "evidence_count": row["evidence_count"],
                    "day_count": row["day_count"],
                    "confidence": row["confidence"],
                    "status": row["status"],
                    "evidence_event_ids": row["evidence_event_ids"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                fh.write(json.dumps(data, sort_keys=True) + "\n")
                count += 1
        return count

    def stats(self) -> dict[str, Any]:
        with self.closing_connection() as conn:
            events = conn.execute("SELECT COUNT(*) FROM context_events").fetchone()[0]
            by_platform = {row[0]: row[1] for row in conn.execute("SELECT platform, COUNT(*) FROM context_events GROUP BY platform")}
            by_tier = {row[0]: row[1] for row in conn.execute("SELECT relevance_tier, COUNT(*) FROM context_events GROUP BY relevance_tier")}
            reminders = conn.execute("SELECT COUNT(*) FROM context_reminders").fetchone()[0]
            habits = conn.execute("SELECT COUNT(*) FROM context_habit_hypotheses").fetchone()[0]
        return {"db": str(self.db_path), "events": events, "by_platform": by_platform, "by_tier": by_tier, "reminders": reminders, "habits": habits}


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    platform = str(event.get("platform") or "generic").lower()
    account = str(event.get("account") or "")
    workspace = str(event.get("workspace") or "")
    conversation_id = str(event.get("conversation_id") or event.get("channel_id") or "")
    message_ts = normalize_timestamp(event.get("message_ts") or event.get("timestamp") or event.get("ts") or "")
    received_ts = normalize_timestamp(event.get("received_ts") or "") or message_ts
    ingested_ts = normalize_timestamp(event.get("ingested_ts") or "") or now_iso()
    source_message_id = str(event.get("source_message_id") or event.get("message_id") or event.get("id") or "")
    body = str(event.get("body") or event.get("text") or "")
    attachment_meta = event.get("attachment_metadata_json", event.get("attachments", event.get("files", [])))
    flags = event.get("flags", [])
    raw = event.get("raw", event)
    identity_key = build_identity_key(
        platform,
        account,
        conversation_id,
        source_message_id,
        message_ts,
        body,
        event.get("sender_id", ""),
        workspace=workspace,
    )
    event_id = "ctx_" + hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:32]
    return {
        "event_id": event_id,
        "identity_key": identity_key,
        "platform": platform,
        "account": account,
        "workspace": workspace,
        # Action-capable identifiers are intentionally separate from the
        # privacy-preserving conversation id and ordinary display metadata.
        "action_target_id": str(event.get("action_target_id") or ""),
        "action_account_id": str(event.get("action_account_id") or ""),
        "conversation_id": conversation_id,
        "conversation_name": str(event.get("conversation_name") or event.get("channel_name") or ""),
        "conversation_type": str(event.get("conversation_type") or ""),
        "sender_id": str(event.get("sender_id") or event.get("user") or ""),
        "sender_display_name": str(event.get("sender_display_name") or event.get("sender_name") or event.get("username") or ""),
        "direction": str(event.get("direction") or "unknown"),
        "body": body,
        "message_ts": message_ts,
        "received_ts": received_ts,
        "ingested_ts": ingested_ts,
        "source_message_id": source_message_id,
        "thread_id": str(event.get("thread_id") or event.get("thread_ts") or ""),
        "permalink": str(event.get("permalink") or ""),
        "attachment_metadata_json": stable_json(attachment_meta if isinstance(attachment_meta, (list, dict)) else []),
        "raw_json": stable_json(raw if isinstance(raw, (list, dict)) else {"raw": str(raw)}),
        "source_path": str(event.get("source_path") or ""),
        "flags": stable_json(flags if isinstance(flags, list) else [str(flags)]),
        "relevance_score": int(event.get("relevance_score") or 0),
        "relevance_tier": str(event.get("relevance_tier") or "archive"),
        "relevance_reasons": stable_json(event.get("relevance_reasons") or []),
        "processing_state": str(event.get("processing_state") or "new"),
    }


def build_identity_key(
    platform: str,
    account: str,
    conversation_id: str,
    source_message_id: str,
    message_ts: str,
    body: str,
    sender_id: Any,
    *,
    workspace: str = "",
) -> str:
    if source_message_id:
        parts = (platform, account, workspace, conversation_id, source_message_id)
        return "source/" + "/".join(quote(str(part), safe="") for part in parts)
    payload = stable_json(
        {
            "platform": platform,
            "account": account,
            "workspace": workspace,
            "conversation_id": conversation_id,
            "sender_id": str(sender_id or ""),
            "message_ts": message_ts,
            "body": body,
        }
    )
    return "content|" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def import_canonical_jsonl(path: Path, db_path: Path, platform: str = "generic", dry_run: bool = False) -> ImportSummary:
    inbox = None if dry_run else ContextInbox(db_path)
    imported = skipped = 0
    errors: list[str] = []
    with _open_text(path) as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                skipped += 1
                errors.append(f"{path}:{line_no}: malformed JSON: {exc.msg}")
                continue
            if not isinstance(data, dict):
                skipped += 1
                errors.append(f"{path}:{line_no}: expected object")
                continue
            timestamp = data.get("message_ts") or data.get("timestamp") or data.get("ts")
            if not normalize_timestamp(timestamp):
                skipped += 1
                errors.append(f"{path}:{line_no}: missing or invalid timestamp")
                continue
            data.setdefault("platform", platform)
            data.setdefault("source_path", str(path))
            imported += 1
            if inbox is not None:
                inbox.upsert_event(data)
    status = "error" if errors else "ok"
    return ImportSummary("jsonl", imported, skipped, tuple(errors), status=status, dry_run=dry_run)


def import_slack_export(path: Path, db_path: Path, dry_run: bool = False) -> ImportSummary:
    if path.is_symlink():
        return ImportSummary("slack", errors=(f"{path}: refusing symlinked Slack archive path",), status="error", dry_run=dry_run)
    inbox = None if dry_run else ContextInbox(db_path)
    imported = skipped = 0
    errors: list[str] = []
    if path.is_file():
        files = [path]
        channels: dict[str, dict[str, Any]] = {}
        users: dict[str, str] = {}
    else:
        root = path.resolve()
        channels = {}
        for name in ("channels.json", "groups.json", "dms.json", "mpims.json"):
            channels.update(_load_slack_map(path / name, "id"))
        users = _load_slack_users(path / "users.json")
        files = []
        for p in sorted(path.rglob("*")):
            if _is_slack_metadata_file(path, p):
                continue
            if p.is_symlink():
                errors.append(f"{p}: refusing symlinked Slack archive file")
                continue
            if not (p.is_file() and (p.name.endswith(".json") or p.name.endswith(".jsonl") or p.name.endswith(".jsonl.gz"))):
                continue
            try:
                p.resolve().relative_to(root)
            except (OSError, ValueError):
                errors.append(f"{p}: refusing out-of-root Slack archive file")
                continue
            files.append(p)
    for file in files:
        channel_name = file.parent.name if file.parent != path else ""
        channel = next((c for c in channels.values() if c.get("name") == channel_name), {})
        for item in _iter_slack_records(file, errors):
            if not isinstance(item, dict):
                skipped += 1
                continue
            wrapper = _slack_wrapper(item)
            message = wrapper["message"]
            channel_meta = wrapper["channel"] or channel
            if not isinstance(message, dict):
                skipped += 1
                continue
            ts = str(message.get("ts") or message.get("client_msg_id") or "")
            user = str(message.get("user") or message.get("bot_id") or "")
            account = str(wrapper.get("workspace_id") or message.get("team") or message.get("team_id") or "")
            workspace = str(wrapper.get("workspace") or message.get("team") or message.get("team_id") or account)
            self_user_id = str(wrapper.get("self_user_id") or os.environ.get("SLACK_SELF_USER_ID") or "")
            conversation_id = str(channel_meta.get("id") or message.get("channel") or channel_name)
            source_message_id = ts or str(message.get("client_msg_id") or "")
            ev = {
                "platform": "slack",
                "account": account,
                "workspace": workspace,
                "conversation_id": conversation_id,
                "conversation_name": str(channel_meta.get("name") or message.get("channel_name") or channel_name),
                "conversation_type": _slack_conversation_type(channel_meta),
                "sender_id": user,
                "sender_display_name": _slack_sender_name(message, users, user),
                "direction": _direction_from_self_user(user, self_user_id),
                "body": str(message.get("text") or ""),
                "message_ts": slack_ts_to_iso(ts),
                "source_message_id": source_message_id,
                "thread_id": str(message.get("thread_ts") or ""),
                "permalink": str(message.get("permalink") or ""),
                "attachments": slack_attachments(message, file),
                "raw": _safe_slack_raw(message),
                "source_path": str(file),
                "flags": ["bot"] if message.get("subtype") == "bot_message" or message.get("bot_id") else [],
            }
            imported += 1
            if inbox is not None:
                inbox.upsert_event(ev)
    status = "error" if errors else "ok"
    return ImportSummary("slack", imported, skipped, tuple(errors), status=status, dry_run=dry_run)


def import_signal_desktop(path: Path, db_path: Path, dry_run: bool = False) -> ImportSummary:
    inbox = None if dry_run else ContextInbox(db_path)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        message = f"Signal Desktop database unavailable or encrypted; import requires an accessible decrypted export: {exc}"
        if inbox is not None:
            inbox.set_import_status("signal", "blocked_decryption_needed", message)
        return ImportSummary("signal", 0, 0, (message,), "blocked_decryption_needed", dry_run)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "messages" not in tables:
            message = "Signal Desktop schema is unavailable or encrypted; decryption/export is needed. Do not read config.json or Keychain here. Pair/export later with Hermes."
            if inbox is not None:
                inbox.set_import_status("signal", "blocked_decryption_needed", message)
            return ImportSummary("signal", 0, 0, (message,), "blocked_decryption_needed", dry_run)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        body_col = "body" if "body" in columns else "json" if "json" in columns else None
        if not body_col:
            message = "Signal messages table has no plaintext body column; decryption/export is needed."
            if inbox is not None:
                inbox.set_import_status("signal", "blocked_decryption_needed", message)
            return ImportSummary("signal", 0, 0, (message,), "blocked_decryption_needed", dry_run)
        rows = conn.execute(f"SELECT rowid,* FROM messages")
        imported = 0
        for row in rows:
            data = dict(zip([d[0] for d in rows.description], row))
            ev = {
                "platform": "signal",
                "account": "",
                "conversation_id": str(data.get("conversationId") or data.get("conversation_id") or data.get("conversationId".lower()) or ""),
                "sender_id": str(data.get("source") or data.get("sender") or ""),
                "direction": _signal_direction(data),
                "body": str(data.get(body_col) or ""),
                "message_ts": normalize_timestamp(data.get("received_at") or data.get("sent_at") or data.get("timestamp") or ""),
                "source_message_id": str(data.get("id") or data.get("rowid") or ""),
                "raw": {"rowid": data.get("rowid")},
                "source_path": str(path),
            }
            imported += 1
            if not dry_run:
                inbox.upsert_event(ev)
        if inbox is not None:
            inbox.set_import_status("signal", "ok", f"imported {imported} rows")
        return ImportSummary("signal", imported, dry_run=dry_run)
    except sqlite3.Error as exc:
        message = f"Signal import blocked by inaccessible/encrypted schema: {exc}"
        if inbox is not None:
            inbox.set_import_status("signal", "blocked_decryption_needed", message)
        return ImportSummary("signal", 0, 0, (message,), "blocked_decryption_needed", dry_run)
    finally:
        conn.close()


def import_whatsapp_chatstorage(path: Path, db_path: Path, dry_run: bool = False) -> ImportSummary:
    inbox = None if dry_run else ContextInbox(db_path)
    imported = skipped = 0
    errors: list[str] = []
    if not path.exists():
        return ImportSummary("whatsapp", 0, 1, (f"{path}: missing WhatsApp ChatStorage file",), status="error", dry_run=dry_run)
    try:
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            conn = sqlite3.connect(":memory:")
            try:
                source.backup(conn)
            except BaseException:
                # A failed backup must not leak the private in-memory snapshot;
                # close it before the outer handler returns an error summary.
                conn.close()
                raise
        finally:
            source.close()
    except sqlite3.Error as exc:
        return ImportSummary("whatsapp", 0, 1, (f"{path}: unavailable WhatsApp ChatStorage snapshot: {exc}",), status="error", dry_run=dry_run)
    try:
        conn.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"ZWAMESSAGE", "ZWACHATSESSION"}
            if not required.issubset(tables):
                missing = ",".join(sorted(required - tables))
                raise sqlite3.Error(f"missing tables: {missing}")
            message_cols = _table_columns(conn, "ZWAMESSAGE")
            chat_cols = _table_columns(conn, "ZWACHATSESSION")
            member_cols = _table_columns(conn, "ZWAGROUPMEMBER") if "ZWAGROUPMEMBER" in tables else set()
            select_cols = [
                _sql_col("m", message_cols, "Z_PK", "message_pk"),
                _sql_col("m", message_cols, "ZISFROMME", "ZISFROMME"),
                _sql_col("m", message_cols, "ZCHATSESSION", "ZCHATSESSION"),
                _sql_col("m", message_cols, "ZGROUPMEMBER", "ZGROUPMEMBER"),
                _sql_col("m", message_cols, "ZMEDIAITEM", "ZMEDIAITEM"),
                _sql_col("m", message_cols, "ZMESSAGEDATE", "ZMESSAGEDATE"),
                _sql_col("m", message_cols, "ZSENTDATE", "ZSENTDATE"),
                _sql_col("m", message_cols, "ZFROMJID", "ZFROMJID"),
                _sql_col("m", message_cols, "ZPUSHNAME", "ZPUSHNAME"),
                _sql_col("m", message_cols, "ZSTANZAID", "ZSTANZAID"),
                _sql_col("m", message_cols, "ZTEXT", "ZTEXT"),
                _sql_col("m", message_cols, "ZTOJID", "ZTOJID"),
                _sql_col("c", chat_cols, "ZSESSIONTYPE", "ZSESSIONTYPE"),
                _sql_col("c", chat_cols, "ZCONTACTJID", "ZCONTACTJID"),
                _sql_col("c", chat_cols, "ZPARTNERNAME", "ZPARTNERNAME"),
                _sql_col("gm", member_cols, "ZMEMBERJID", "ZMEMBERJID"),
                _sql_col("gm", member_cols, "ZFIRSTNAME", "ZFIRSTNAME"),
                _sql_col("gm", member_cols, "ZCONTACTNAME", "ZCONTACTNAME"),
            ]
            member_join = "LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER" if "ZWAGROUPMEMBER" in tables and "Z_PK" in member_cols and "ZGROUPMEMBER" in message_cols else ""
            order_col = "m.ZMESSAGEDATE" if "ZMESSAGEDATE" in message_cols else "m.Z_PK" if "Z_PK" in message_cols else "rowid"
            messages = conn.execute(
                f"""
                SELECT {', '.join(select_cols)}
                FROM ZWAMESSAGE m
                LEFT JOIN ZWACHATSESSION c ON c.Z_PK = m.ZCHATSESSION
                {member_join}
                ORDER BY {order_col}
                """
            ).fetchall()
            media_by_message = _whatsapp_media_by_message(conn, messages) if "ZWAMEDIAITEM" in tables else {}
            for row in messages:
                data = dict(row)
                attachments = media_by_message.get(data["message_pk"], [])
                sender_id = str(data.get("ZMEMBERJID") or data.get("ZFROMJID") or data.get("ZGROUPMEMBER") or "")
                sender_name = str(data.get("ZPUSHNAME") or data.get("ZFIRSTNAME") or data.get("ZCONTACTNAME") or "")
                ev = {
                    "platform": "whatsapp",
                    "account": "",
                    "conversation_id": str(data.get("ZCONTACTJID") or data.get("ZCHATSESSION") or ""),
                    "conversation_name": str(data.get("ZPARTNERNAME") or data.get("ZCONTACTJID") or ""),
                    "conversation_type": "group" if int(data.get("ZSESSIONTYPE") or 0) else "dm",
                    "sender_id": sender_id,
                    "sender_display_name": sender_name,
                    "direction": "outbound" if int(data.get("ZISFROMME") or 0) else "inbound",
                    "body": str(data.get("ZTEXT") or ""),
                    "message_ts": core_data_ts_to_iso(data.get("ZMESSAGEDATE") or data.get("ZSENTDATE")),
                    "source_message_id": str(data.get("ZSTANZAID") or data.get("message_pk")),
                    "attachments": attachments,
                    "raw": {"message_pk": data.get("message_pk"), "chat_pk": data.get("ZCHATSESSION"), "media_ids": [item["media_id"] for item in attachments]},
                    "source_path": str(path),
                }
                imported += 1
                if inbox is not None:
                    inbox.upsert_event(ev)
        except sqlite3.Error as exc:
            skipped += 1
            errors.append(f"{path}: unsupported WhatsApp ChatStorage schema: {exc}")
        finally:
            conn.close()
    finally:
        pass
    status = "error" if errors else "ok"
    return ImportSummary("whatsapp", imported, skipped, tuple(errors), status=status, dry_run=dry_run)


def score_event(row: dict[str, Any]) -> tuple[int, str, list[str]]:
    text = f"{row.get('body','')} {row.get('conversation_name','')} {row.get('sender_display_name','')}".lower()
    flags = set(json.loads(row.get("flags") or "[]"))
    score = 15
    reasons: list[str] = []
    if row.get("direction") == "inbound":
        score += 10
        reasons.append("inbound")
    if re.search(r"\b(can you|could you|please|pls|need you|would you|do you mind|kannst du|koenntest du|könntest du|bitte|machst du|brauchst du)\b", text):
        score += 30
        reasons.append("direct ask")
    if re.search(r"\b(today|tomorrow|tonight|by\s+\w+|deadline|due|before|after|at\s+\d{1,2}|heute|morgen|uebermorgen|übermorgen|bis|termin|uhr)\b", text):
        score += 20
        reasons.append("deadline/date hint")
    if re.search(r"\b(urgent|asap|important|now|quickly|blocked|dringend|wichtig|sofort|schnell|blockiert)\b", text):
        score += 20
        reasons.append("urgency")
    if re.search(r"\b(mom|dad|family|wife|husband|partner|kids?|home|mama|mutter|papa|vater|familie|eltern)\b", text):
        score += 15
        reasons.append("family/close contact")
    if re.search(r"@\w+", text):
        score += 12
        reasons.append("mention")
    if row.get("conversation_type") == "dm" and row.get("direction") == "inbound":
        score += 12
        reasons.append("unanswered inbound DM")
    if re.search(r"\b(i will|i'll|we will|let's|todo|task|remember to|ich mache|ich kuemmere mich|ich kümmere mich|aufgabe|erinner mich|muss noch)\b", text):
        score += 12
        reasons.append("commitment/task")
    if flags.intersection({"bot", "marketing"}) or re.search(r"\b(newsletter|promo|sale|unsubscribe|marketing|werbung|rabatt|abmelden)\b", text):
        score -= 35
        reasons.append("suppressed bot/marketing/noise")
    if row.get("direction") == "outbound" and not re.search(r"\b(i will|todo|remember to)\b", text):
        score -= 20
        reasons.append("suppressed outbound-only chatter")
    score = max(0, min(100, score))
    tier = "immediate" if score >= 85 else "briefing" if score >= 60 else "archive"
    return score, tier, reasons


def reminder_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    text = row.get("body", "")
    lower = text.lower()
    if not re.search(r"\b(can you|could you|please|todo|task|remember to|need you|remind me|call|send|review|pay|book|kannst du|koenntest du|könntest du|bitte|erinner mich|anrufen|schicken|senden|prüfen|pruefen|bezahlen|buchen|rausbringen|mitbringen)\b", lower):
        return None
    due_hint = ""
    match = re.search(r"\b(today|tomorrow|tonight|next\s+\w+|by\s+\w+|on\s+\w+|at\s+\d{1,2}(?::\d{2})?|heute|morgen|uebermorgen|übermorgen|bis\s+\w+|um\s+\d{1,2}(?::\d{2})?\s*uhr|termin|deadline)\b", lower)
    if match:
        due_hint = match.group(0)
    return {"text": text.strip()[:500], "due_hint": due_hint, "confidence": 0.75 if due_hint else 0.55}


def habit_phrase(text: str) -> str | None:
    lower = text.lower()
    if "every morning" in lower and "standup" in lower:
        return "Possible habit: recurring morning standup"
    if match := re.search(r"\b(every\s+(day|morning|evening|week|monday|tuesday|wednesday|thursday|friday)|jeden\s+(morgen|abend|tag)|jede\s+woche|montags|dienstags|mittwochs|donnerstags|freitags|samstags|sonntags)\b", lower):
        return "Possible habit: " + re.sub(r"\s+", " ", text[:80]).strip()
    return None


def redact_sensitive_text(text: str) -> str:
    redacted = text
    redacted = re.sub(r"(https?://)([^/\s:@]+):([^/\s@]+)@", r"\1[REDACTED]@", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"https?://files\.slack\.com/\S+", "[REDACTED]", redacted, flags=re.IGNORECASE)
    redacted = re.sub(
        r"\b(?:password|passwd|passphrase|token|api[_ -]?key|access[_ -]?key|client[_ -]?secret|access[_ -]?secret|secret)\s*[:=]\s*(['\"])[^'\"]+\1",
        "[REDACTED]",
        redacted,
        flags=re.IGNORECASE,
    )
    patterns = [
        r"\b\d{6}\b",
        r"\b(?:password|passwd|passphrase)\s+(?:is|was)\s+(?:\"[^\"]+\"|'[^']+'|\S+)",
        r"\b(?:password|passwd|passphrase|token|api[_ -]?key|access[_ -]?key|client[_ -]?secret|access[_ -]?secret|secret)\s*[:=]\s*\S+",
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b",
        r"\bsk-[A-Za-z0-9_-]{8,}\b",
        r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{8,}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
        r"\bxox(?:b|p|o|a|r|s)-[A-Za-z0-9-]{8,}\b",
        r"\b[A-Za-z0-9_-]{32,}\b",
        r"\b[A-Za-z0-9._%+-]+:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\b",
    ]
    for pattern in patterns:
        redacted = re.sub(pattern, "[REDACTED]", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"https?://[^\s<>'\")]+", _sanitize_url_match, redacted, flags=re.IGNORECASE)
    return redacted


def _sanitize_url_match(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;:!?)]}":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    if trailing.startswith("]") and raw.endswith("[REDACTED"):
        raw += "]"
        trailing = trailing[1:]
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "[REDACTED]" + trailing
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return "[REDACTED]" + trailing
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        return "[REDACTED]" + trailing
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "", "", "")) + trailing


def _safe_public_body(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", redact_sensitive_text(text)).strip()[:limit]


def parse_iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def default_context_export_path() -> Path:
    return Path(os.environ.get("CONTEXT_EXPORT", str(DEFAULT_CONTEXT_EXPORT))).expanduser()


def default_whatsapp_import_path(context_import_dir: Path | None = None) -> Path:
    override = os.environ.get("WHATSAPP_IMPORT_PATH")
    if override:
        return Path(override).expanduser()
    macos = Path("~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite").expanduser()
    if macos.exists():
        return macos
    base = context_import_dir or Path("~/.hermes/second-brain/import").expanduser()
    return Path(base).expanduser() / "ChatStorage.sqlite"


@contextmanager
def atomic_text_writer(path: Path):
    fd = None
    temp = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp = Path(temp_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            yield fh
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
        _chmod_private_file(path)
        _fsync_dir(path.parent)
    except Exception:
        if fd is not None:
            os.close(fd)
        if temp is not None:
            temp.unlink(missing_ok=True)
        raise


def _prepare_private_file_parent(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not existed or _is_within_private_hermes_root(path):
        _chmod_private_dir(path)


def _is_within_private_hermes_root(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve(strict=False)
        private_root = Path("~/.hermes").expanduser().resolve(strict=False)
    except OSError:
        return False
    return resolved == private_root or private_root in resolved.parents


def _chmod_private_dir(path: Path) -> None:
    _chmod_if_owned(path, 0o700)


def _chmod_private_file(path: Path) -> None:
    _chmod_if_owned(path, 0o600)


def _chmod_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        _chmod_private_file(Path(str(path) + suffix))


def _reject_symlink_sqlite_paths(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.is_symlink():
            raise ValueError(f"refusing symlinked SQLite path: {candidate}")
    target = path.expanduser().absolute()
    for candidate in target.parents:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISLNK(info.st_mode):
            continue
        if not hasattr(os, "geteuid") or info.st_uid == os.geteuid():
            raise ValueError(f"refusing user-owned symlink in SQLite path: {candidate}")


def _chmod_if_owned(path: Path, mode: int) -> None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return
    if hasattr(os, "geteuid") and stat.st_uid != os.geteuid():
        return
    try:
        os.chmod(path, mode)
    except OSError:
        return


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextmanager
def _open_text(path: Path):
    if path.name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            yield fh
    else:
        with path.open("r", encoding="utf-8") as fh:
            yield fh


def _iter_slack_records(path: Path, errors: list[str]) -> Iterable[dict[str, Any]]:
    if path.name.endswith(".jsonl") or path.name.endswith(".jsonl.gz"):
        try:
            with _open_text(path) as fh:
                for line_no, line in enumerate(fh, start=1):
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError as exc:
                        errors.append(f"{path}:{line_no}: malformed JSON: {exc.msg}")
                        continue
                    if isinstance(data, dict):
                        yield data
                    else:
                        errors.append(f"{path}:{line_no}: expected object")
        except OSError as exc:
            errors.append(f"{path}: {exc}")
        return
    try:
        with _open_text(path) as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{path}: {exc}")
        return
    records = raw if isinstance(raw, list) else raw.get("messages", []) if isinstance(raw, dict) else []
    for item in records:
        if isinstance(item, dict):
            yield item


def _slack_wrapper(item: dict[str, Any]) -> dict[str, Any]:
    message = item.get("message")
    if isinstance(message, dict):
        return {
            "message": message,
            "channel": item.get("channel") if isinstance(item.get("channel"), dict) else {},
            "workspace": item.get("workspace"),
            "workspace_id": item.get("workspace_id"),
            "self_user_id": item.get("self_user_id"),
        }
    return {"message": item, "channel": {}, "workspace": None, "workspace_id": None, "self_user_id": None}


def _slack_conversation_type(channel: dict[str, Any]) -> str:
    if channel.get("is_im"):
        return "dm"
    if channel.get("is_mpim"):
        return "mpim"
    if channel.get("is_private"):
        return "private_channel"
    return "channel"


def _direction_from_self_user(user: str, self_user_id: str) -> str:
    if not self_user_id:
        return "unknown"
    return "outbound" if user and user == self_user_id else "inbound"


def _signal_direction(row: dict[str, Any]) -> str:
    value = row.get("direction")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"inbound", "incoming", "received", "receive"}:
            return "inbound"
        if normalized in {"outbound", "outgoing", "sent", "send"}:
            return "outbound"
    for key in ("isOutgoing", "is_outgoing", "outgoing"):
        if key in row and row.get(key) is not None:
            return "outbound" if bool(row.get(key)) else "inbound"
    type_value = str(row.get("type") or row.get("messageType") or row.get("message_type") or "").strip().lower()
    if type_value in {"incoming", "received", "message.received", "sms_received"}:
        return "inbound"
    if type_value in {"outgoing", "sent", "message.sent", "sms_sent"}:
        return "outbound"
    return "unknown"


def _slack_sender_name(message: dict[str, Any], users: dict[str, str], user: str) -> str:
    profile = message.get("user_profile")
    if isinstance(profile, dict):
        name = profile.get("real_name") or profile.get("display_name") or profile.get("name")
        if name:
            return str(name)
    return str(message.get("username") or users.get(user, ""))


def _safe_slack_raw(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": message.get("type"),
        "subtype": message.get("subtype"),
        "team": message.get("team") or message.get("team_id"),
        "client_msg_id": message.get("client_msg_id"),
        "file_ids": [f.get("id") for f in message.get("files") or [] if isinstance(f, dict) and f.get("id")],
    }


def _load_slack_map(path: Path, key: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, list):
        return {}
    return {str(item.get(key)): item for item in data if isinstance(item, dict) and item.get(key)}


def _is_slack_metadata_file(root: Path, path: Path) -> bool:
    metadata = {
        "channels.json",
        "users.json",
        "groups.json",
        "dms.json",
        "mpims.json",
        "canvases.json",
        "integration_logs.json",
        "file_conversations.json",
        "usergroups.json",
    }
    return path.parent == root and path.name in metadata


def _load_slack_users(path: Path) -> dict[str, str]:
    users = _load_slack_map(path, "id")
    return {
        user_id: str(item.get("real_name") or item.get("name") or item.get("profile", {}).get("real_name") or item.get("profile", {}).get("display_name") or "")
        for user_id, item in users.items()
    }


def slack_attachments(item: dict[str, Any], source_file: Path | None = None) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for file in item.get("files") or []:
        if isinstance(file, dict):
            meta = {k: file.get(k) for k in ("id", "name", "mimetype", "filetype", "size", "permalink") if k in file}
            local_path = _slack_local_attachment_path(file, source_file)
            if local_path:
                meta["local_archive_path"] = str(local_path)
            attachments.append(meta)
    for attachment in item.get("attachments") or []:
        if isinstance(attachment, dict):
            attachments.append({k: attachment.get(k) for k in ("id", "title", "text", "fallback") if k in attachment})
    return attachments


def _slack_local_attachment_path(file: dict[str, Any], source_file: Path | None) -> Path | None:
    if source_file is None:
        return None
    file_id = str(file.get("id") or "")
    if not re.fullmatch(r"[A-Z0-9]{2,32}", file_id):
        return None
    name = str(file.get("name") or "")
    suffix = Path(name).suffix
    if not suffix and file.get("filetype"):
        suffix = "." + str(file.get("filetype"))
    if suffix and not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix):
        return None
    for base in (source_file.parent / "attachments", source_file.parent.parent / "attachments"):
        try:
            base_resolved = base.resolve(strict=True)
        except OSError:
            continue
        candidate = base / f"{file_id}{suffix}"
        if _safe_attachment_file(candidate, base_resolved):
            return candidate
        matches = sorted(base.glob(f"{file_id}.*")) if base.exists() else []
        for match in matches:
            if _safe_attachment_file(match, base_resolved):
                return match
    return None


def _safe_attachment_file(path: Path, base_resolved: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        path.resolve().relative_to(base_resolved)
    except (OSError, ValueError):
        return False
    return True


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _sql_col(alias: str, columns: set[str], column: str, output: str) -> str:
    if column in columns:
        return f"{alias}.{column} AS {output}"
    return f"NULL AS {output}"


def _whatsapp_media_by_message(conn: sqlite3.Connection, messages: list[sqlite3.Row]) -> dict[int, list[dict[str, Any]]]:
    message_pks = {int(row["message_pk"]) for row in messages if row["message_pk"] is not None}
    media_links = {int(row["ZMEDIAITEM"]): int(row["message_pk"]) for row in messages if row["ZMEDIAITEM"] is not None and row["message_pk"] is not None}
    media: dict[int, list[dict[str, Any]]] = {pk: [] for pk in message_pks}
    columns = _table_columns(conn, "ZWAMEDIAITEM")
    if "Z_PK" not in columns:
        return media
    rows = conn.execute(
        "SELECT "
        + ", ".join(
            [
                _sql_col("", columns, "Z_PK", "media_pk").lstrip("."),
                _sql_col("", columns, "ZMESSAGE", "ZMESSAGE").lstrip("."),
                _sql_col("", columns, "ZFILESIZE", "ZFILESIZE").lstrip("."),
                _sql_col("", columns, "ZMEDIALOCALPATH", "ZMEDIALOCALPATH").lstrip("."),
                _sql_col("", columns, "ZMEDIAURL", "ZMEDIAURL").lstrip("."),
                _sql_col("", columns, "ZTITLE", "ZTITLE").lstrip("."),
            ]
        )
        + " FROM ZWAMEDIAITEM"
    ).fetchall()
    seen: set[tuple[int, int]] = set()
    for row in rows:
        data = dict(row)
        targets: set[int] = set()
        if data.get("ZMESSAGE") is not None and int(data["ZMESSAGE"]) in message_pks:
            targets.add(int(data["ZMESSAGE"]))
        if data.get("media_pk") is not None and int(data["media_pk"]) in media_links:
            targets.add(media_links[int(data["media_pk"])])
        for message_pk in targets:
            key = (message_pk, int(data["media_pk"]))
            if key in seen:
                continue
            seen.add(key)
            media.setdefault(message_pk, []).append(
                {
                    "media_id": data.get("media_pk"),
                    "size": data.get("ZFILESIZE"),
                    "local_path": data.get("ZMEDIALOCALPATH") or "",
                    "media_url": data.get("ZMEDIAURL") or "",
                    "title": data.get("ZTITLE") or "",
                }
            )
    return media


def slack_ts_to_iso(value: str) -> str:
    try:
        return _iso_z(dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc))
    except (TypeError, ValueError, OverflowError):
        return ""


def core_data_ts_to_iso(value: Any) -> str:
    try:
        return _iso_z(CORE_DATA_EPOCH + dt.timedelta(seconds=float(value)))
    except (TypeError, ValueError, OverflowError):
        return ""


def normalize_timestamp(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, dt.datetime):
        parsed = value.astimezone() if value.tzinfo is None else value
        return _iso_z(parsed.astimezone(dt.timezone.utc))
    if isinstance(value, (int, float)):
        try:
            seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
            return _iso_z(dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc))
        except (OSError, ValueError, OverflowError):
            return ""
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return slack_ts_to_iso(text)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return _iso_z(parsed.astimezone(dt.timezone.utc))


def _iso_z(value: dt.datetime) -> str:
    text = value.astimezone(dt.timezone.utc).isoformat(timespec="microseconds" if value.microsecond else "seconds")
    return text.replace("+00:00", "Z")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def print_or_json(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, sort_keys=True))
    else:
        print(data)


def db_from_args(args: argparse.Namespace, manifest_loader: Any | None = None) -> Path:
    if getattr(args, "db", None):
        return Path(args.db).expanduser()
    manifest_path = getattr(args, "manifest", None)
    if manifest_path and manifest_loader:
        manifest = manifest_loader(Path(manifest_path))
        return getattr(manifest, "context_inbox_db", DEFAULT_CONTEXT_DB)
    return DEFAULT_CONTEXT_DB
