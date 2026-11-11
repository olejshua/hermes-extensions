"""TTL-bound temporary context and episode storage in Context Inbox SQLite."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .context_inbox import ContextInbox, DEFAULT_CONTEXT_DB, redact_sensitive_text


KINDS = {"context", "episode"}
MAX_TEXT_CHARACTERS = 4000
MAX_SOURCE_REF_CHARACTERS = 500
MAX_TTL = dt.timedelta(days=365)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class TemporaryMemoryStore:
    def __init__(self, db_path: Path | str = DEFAULT_CONTEXT_DB):
        self.inbox = ContextInbox(db_path)

    @property
    def db_path(self) -> Path:
        return self.inbox.db_path

    def add(
        self,
        *,
        idempotency_key: str,
        kind: str,
        text: str,
        expires_at: str | dt.datetime,
        source_ref: str = "",
        now: str | dt.datetime | None = None,
        supersedes_record_id: str | None = None,
        supersedes_source_ref: str | None = None,
        supersedes_source_version: str | None = None,
    ) -> dict[str, Any]:
        clock = coerce_utc_clock(now)
        expiry = parse_utc_timestamp(expires_at, "expires_at")
        key = validate_idempotency_key(idempotency_key)
        normalized_kind = validate_kind(kind)
        normalized_text = validate_bounded_text(text, "text", MAX_TEXT_CHARACTERS, required=True)
        normalized_source = validate_bounded_text(
            source_ref, "source_ref", MAX_SOURCE_REF_CHARACTERS, required=False
        )
        record_id = "tmp_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        created_at = canonical_utc(clock)
        expires_at_value = canonical_utc(expiry)
        if supersedes_record_id is not None and (
            not isinstance(supersedes_record_id, str)
            or not re.fullmatch(r"tmp_[0-9a-f]{32}", supersedes_record_id)
        ):
            raise ValueError("invalid superseded temporary-memory record")
        if supersedes_record_id is None and (supersedes_source_ref is not None or supersedes_source_version is not None):
            raise ValueError("supersession requires a record identity")
        with self.inbox.closing_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if supersedes_record_id is not None:
                prior = conn.execute(
                    "SELECT * FROM context_temporary_memory WHERE record_id=?", (supersedes_record_id,)
                ).fetchone()
                # Explicit correction only: never infer a version or person match from prose.
                # Pin the target's actual source and version as well as owner/work order.
                try:
                    old_scope, new_scope = json.loads(prior["source_ref"]), json.loads(normalized_source)
                    same_scope = all(isinstance(old_scope.get(k), str) and old_scope[k]
                                     and old_scope[k] == new_scope.get(k) for k in ("owner", "work_order"))
                    source_bound = all(isinstance(v, str) and v for v in
                                       (supersedes_source_ref, supersedes_source_version)) and (
                        old_scope.get("source_ref") == supersedes_source_ref
                        and old_scope.get("source_version") == supersedes_source_version)
                except (ValueError, TypeError, AttributeError):
                    same_scope = source_bound = False
                if not same_scope or not source_bound or prior["kind"] != normalized_kind:
                    raise ValueError("supersession owner/work-order/kind/source/version mismatch")
            existing = conn.execute(
                "SELECT * FROM context_temporary_memory WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing is not None:
                # An identical retry of an already-inserted key returns the
                # original record even at/after its expiry; only insert-only
                # freshness (future expiry, max TTL) is enforced for new rows,
                # after confirming the content matches structurally.
                record = dict(existing)
                expected = (normalized_kind, normalized_text, normalized_source, expires_at_value)
                actual = (
                    record["kind"],
                    record["text"],
                    record["source_ref"],
                    record["expires_at"],
                )
                if actual != expected:
                    raise ValueError("idempotency_key already exists with different temporary-memory content")
                predecessor = conn.execute(
                    "SELECT prior_record_id FROM context_temporary_supersessions WHERE replacement_record_id=?",
                    (record_id,),
                ).fetchone()
                if (predecessor[0] if predecessor else None) != supersedes_record_id:
                    raise ValueError("idempotency_key already exists with different supersession")
                successor = conn.execute(
                    "SELECT replacement_record_id FROM context_temporary_supersessions WHERE prior_record_id=?",
                    (record_id,),
                ).fetchone()
                record["superseded_by_record_id"] = successor[0] if successor else None
                record["supersedes_record_id"] = supersedes_record_id
                return self._record_result(record, clock, created=False)
            if expiry <= clock:
                raise ValueError("expires_at must be in the future")
            if expiry - clock > MAX_TTL:
                raise ValueError("expires_at exceeds the maximum TTL of 365 days")
            if supersedes_record_id is not None:
                if conn.execute(
                    "SELECT 1 FROM context_temporary_supersessions WHERE prior_record_id=?", (supersedes_record_id,)
                ).fetchone():
                    raise ValueError("superseded record missing or already replaced")
            conn.execute(
                """
                INSERT INTO context_temporary_memory(
                  record_id,idempotency_key,kind,text,source_ref,created_at,created_at_epoch,
                  expires_at,expires_at_epoch,status,expired_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,'active','',?)
                """,
                (
                    record_id,
                    key,
                    normalized_kind,
                    normalized_text,
                    normalized_source,
                    created_at,
                    clock.timestamp(),
                    expires_at_value,
                    expiry.timestamp(),
                    created_at,
                ),
            )
            record = dict(
                conn.execute(
                    "SELECT * FROM context_temporary_memory WHERE record_id=?", (record_id,)
                ).fetchone()
            )
            if supersedes_record_id is not None:
                conn.execute(
                    "INSERT INTO context_temporary_supersessions(prior_record_id,replacement_record_id,created_at) VALUES(?,?,?)",
                    (supersedes_record_id, record_id, created_at),
                )
            record["supersedes_record_id"] = supersedes_record_id
        return self._record_result(record, clock, created=True)

    def list(
        self,
        *,
        now: str | dt.datetime | None = None,
        include_expired: bool = False,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        clock = coerce_utc_clock(now)
        parameters: list[Any] = []
        clauses: list[str] = []
        if include_expired:
            clauses.append("1=1")
        else:
            clauses.extend(("status='active'", "expires_at_epoch>?",
                            "NOT EXISTS(SELECT 1 FROM context_temporary_supersessions s WHERE s.prior_record_id=m.record_id)"))
            parameters.append(clock.timestamp())
        if kind is not None:
            clauses.append("kind=?")
            parameters.append(validate_kind(kind))
        query = ("SELECT m.*, "
                 "(SELECT replacement_record_id FROM context_temporary_supersessions s WHERE s.prior_record_id=m.record_id) AS superseded_by_record_id, "
                 "(SELECT prior_record_id FROM context_temporary_supersessions s WHERE s.replacement_record_id=m.record_id) AS supersedes_record_id "
                 "FROM context_temporary_memory m WHERE " + " AND ".join(clauses))
        query += " ORDER BY expires_at_epoch,record_id"
        with self.inbox.closing_connection() as conn:
            rows = [dict(row) for row in conn.execute(query, parameters)]
        return [self._record_result(row, clock, created=False) for row in rows]

    def expire(
        self,
        *,
        now: str | dt.datetime | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        clock = coerce_utc_clock(now)
        stamp = canonical_utc(clock)
        with self.inbox.closing_connection() as conn:
            if not dry_run:
                conn.execute("BEGIN IMMEDIATE")
            record_ids = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT record_id FROM context_temporary_memory
                    WHERE status='active' AND expires_at_epoch<=?
                    ORDER BY record_id
                    """,
                    (clock.timestamp(),),
                )
            ]
            if not dry_run and record_ids:
                conn.execute(
                    """
                    UPDATE context_temporary_memory
                    SET status='expired',expired_at=?,updated_at=?
                    WHERE status='active' AND expires_at_epoch<=?
                    """,
                    (stamp, stamp, clock.timestamp()),
                )
        return {"operation": "expire", "matched": len(record_ids), "dry_run": dry_run, "record_ids": record_ids}

    def purge(
        self,
        *,
        now: str | dt.datetime | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        clock = coerce_utc_clock(now)
        with self.inbox.closing_connection() as conn:
            if not dry_run:
                conn.execute("BEGIN IMMEDIATE")
            # Keep an entire correction chain while any member is unexpired.
            # Removing only an expired successor would revive its predecessor;
            # removing only a predecessor would lose the correction's provenance.
            record_ids = [
                row[0]
                for row in conn.execute(
                    """
                    WITH RECURSIVE retained(record_id) AS (
                      SELECT record_id FROM context_temporary_memory WHERE expires_at_epoch>?
                      UNION
                      SELECT CASE WHEN s.prior_record_id=r.record_id
                                  THEN s.replacement_record_id ELSE s.prior_record_id END
                      FROM context_temporary_supersessions s JOIN retained r
                        ON s.prior_record_id=r.record_id OR s.replacement_record_id=r.record_id
                    )
                    SELECT record_id FROM context_temporary_memory
                    WHERE expires_at_epoch<=? AND record_id NOT IN (SELECT record_id FROM retained)
                    ORDER BY record_id
                    """,
                    (clock.timestamp(), clock.timestamp()),
                )
            ]
            eligible = set(record_ids)
            retained_record_ids = [row[0] for row in conn.execute(
                "SELECT record_id FROM context_temporary_memory WHERE expires_at_epoch<=? ORDER BY record_id",
                (clock.timestamp(),),
            ) if row[0] not in eligible]
            if not dry_run and record_ids:
                conn.executemany(
                    "DELETE FROM context_temporary_supersessions WHERE prior_record_id=? OR replacement_record_id=?",
                    [(record_id, record_id) for record_id in record_ids],
                )
                conn.executemany("DELETE FROM context_temporary_memory WHERE record_id=?",
                                 [(record_id,) for record_id in record_ids])
        return {"operation": "purge", "matched": len(record_ids), "dry_run": dry_run, "record_ids": record_ids,
                "retained_lineage_record_ids": retained_record_ids}

    @staticmethod
    def _record_result(record: dict[str, Any], clock: dt.datetime, *, created: bool) -> dict[str, Any]:
        active = (record["status"] == "active" and float(record["expires_at_epoch"]) > clock.timestamp()
                  and not record.get("superseded_by_record_id"))
        return {
            "record_id": record["record_id"],
            "kind": record["kind"],
            "text": record["text"],
            "source_ref": record["source_ref"],
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
            "status": record["status"],
            "expired_at": record["expired_at"],
            "active": active,
            "created": created,
            "supersedes_record_id": record.get("supersedes_record_id"),
            "superseded_by_record_id": record.get("superseded_by_record_id"),
        }


def public_temporary_record(record: dict[str, Any]) -> dict[str, Any]:
    public = dict(record)
    public["text"] = _safe_display(str(public.get("text") or ""), MAX_TEXT_CHARACTERS)
    public["source_ref"] = _safe_display(str(public.get("source_ref") or ""), MAX_SOURCE_REF_CHARACTERS)
    return public


def validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise ValueError("idempotency_key must be 1..128 safe identifier characters")
    return value


def validate_kind(value: Any) -> str:
    if not isinstance(value, str) or value not in KINDS:
        raise ValueError("kind must be 'context' or 'episode'")
    return value


def validate_bounded_text(value: Any, label: str, maximum: int, *, required: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if "\x00" in value or any(ord(character) < 32 and character not in "\n\t\r" for character in value):
        raise ValueError(f"{label} contains control characters")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{label} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    return normalized


def parse_utc_timestamp(value: str | dt.datetime, label: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO-8601 UTC timestamp") from exc
    else:
        raise ValueError(f"{label} must be an ISO-8601 UTC timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a UTC timezone")
    if parsed.utcoffset() != dt.timedelta(0):
        raise ValueError(f"{label} must use UTC (Z or +00:00)")
    return parsed.astimezone(dt.timezone.utc)


def coerce_utc_clock(value: str | dt.datetime | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.timezone.utc)
    return parse_utc_timestamp(value, "now")


def canonical_utc(value: dt.datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be a number from 0 to 1")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0 <= normalized <= 1:
        raise ValueError("confidence must be a finite number from 0 to 1")
    return normalized


def _safe_display(value: str, maximum: int) -> str:
    return " ".join(redact_sensitive_text(value).split())[:maximum]
