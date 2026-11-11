"""Bounded metadata-only German morning operations report."""

from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .dreaming.config import DreamingConfig
from .dreaming.store import DreamStore


MAX_MORNING_REPORT_CHARACTERS = 3900


@dataclass(frozen=True)
class MorningDelivery:
    text: str
    report_id: str
    owner: str


def claim_morning_report(
    config: DreamingConfig,
    *,
    context_db: Path,
    owner: str,
    now: dt.datetime | None = None,
) -> MorningDelivery | None:
    """Claim today's newest trigger and freeze its metadata-only rendering."""

    if not config.dream_state_db.is_file() or config.dream_state_db.is_symlink():
        return None
    clock = _aware_now(now)
    local_clock = clock.astimezone(config.tzinfo())
    day_start = dt.datetime.combine(
        local_clock.date(), dt.time.min, tzinfo=config.tzinfo()
    )
    next_day = local_clock.date() + dt.timedelta(days=1)
    day_end = dt.datetime.combine(next_day, dt.time.min, tzinfo=config.tzinfo())
    store = DreamStore(config.dream_state_db)
    row = store.claim_report(
        owner=owner,
        now=clock.timestamp(),
        day_start=day_start.timestamp(),
        day_end=day_end.timestamp(),
        delivery_day=local_clock.date().isoformat(),
    )
    if row is None:
        return None
    try:
        text = str(row.get("delivery_snapshot") or "")
        if not text:
            text = build_morning_report(
                config,
                context_db=context_db,
                now=clock,
                store=store,
                run_id=str(row["run_id"]),
                delivery_id=str(row["report_id"]),
            )
            text = store.save_report_delivery_snapshot(
                str(row["report_id"]), owner, text
            )
    except Exception:
        store.release_report_claim(str(row["report_id"]), owner)
        raise
    return MorningDelivery(text=text, report_id=str(row["report_id"]), owner=owner)


def build_morning_report(
    config: DreamingConfig,
    *,
    context_db: Path,
    now: dt.datetime | None = None,
    store: DreamStore | None = None,
    run_id: str | None = None,
    delivery_id: str | None = None,
) -> str:
    clock = _aware_now(now)
    dream = store or DreamStore(config.dream_state_db)
    latest = dream.run_by_id(run_id) if run_id else dream.latest_run()
    latest = latest or {}
    last_status = str(latest.get("status") or "noch_nie")
    if last_status not in {
        "complete", "failed", "deadline_reached", "running", "publication_pending", "noch_nie"
    }:
        last_status = "unbekannt"
    rounds = latest.get("rounds", 0)
    if type(rounds) is not int:
        rounds = 0
    publications = dream.publication_counts()
    staging = dream.staging_counts()
    pending_sessions = dream.pending_source_count()
    contradictions = _scalar(
        config.dream_state_db,
        "SELECT COUNT(*) FROM insights WHERE kind IN ('contradiction','stale_conflict')",
    )
    stalled = _scalar(
        config.lifecycle_db,
        "SELECT COUNT(*) FROM lifecycle_jobs WHERE state IN ('stalled','failed')",
    )
    approvals = _scalar(
        config.improvement_db,
        "SELECT COUNT(*) FROM improvement_proposals WHERE state='reviewed'",
    )
    expired_ttl = _scalar(
        Path(context_db),
        "SELECT COUNT(*) FROM context_temporary_memory "
        "WHERE status='expired' OR (status='active' AND expires_at_epoch<=?)",
        (clock.timestamp(),),
    )
    lines = [
        f"Hermes Morgenlage · {clock.astimezone(config.tzinfo()).date().isoformat()}",
        *([f"Liefer-ID: {delivery_id}"] if delivery_id else []),
        f"Dream-Gesundheit: {last_status}; abgeschlossene Runden im letzten Lauf: {max(0, rounds)}",
        f"Dream-Rückstand: {max(0, pending_sessions)} Sessions",
        (
            "Publikationswarteschlange: "
            f"{publications.get('pending', 0)} ausstehend; "
            f"{publications.get('locally_published', 0)} lokal veröffentlicht; "
            f"{publications.get('synced', 0)} nachweislich synchronisiert"
        ),
        (
            "Dream-Vorschlagswarteschlange: "
            f"{staging.get('pending', 0)} ausstehend; {staging.get('staged', 0)} "
            "zur Freigabe vorgemerkt"
        ),
        f"Blockierte/stehende Jobs: {max(0, stalled)}",
        f"Ausstehende Verbesserungsfreigaben: {max(0, approvals)}",
        f"Abgelaufene TTL-Einträge: {max(0, expired_ttl)}",
        f"Widersprüche: {max(0, contradictions)}",
        "Hinweis: ausschließlich aggregierte Metadaten; keine Transcript-, Tool- oder Nachrichteninhalte.",
    ]
    return ("\n".join(lines) + "\n")[:MAX_MORNING_REPORT_CHARACTERS]


def emit_morning_report(
    config: DreamingConfig,
    *,
    context_db: Path,
    stdout: TextIO | None = None,
    owner: str,
    now: dt.datetime | None = None,
) -> int:
    """Deliver at least once across crashes; normal claim/ack emits once."""

    stream = stdout or sys.stdout
    delivery: MorningDelivery | None = None
    store: DreamStore | None = None
    try:
        delivery = claim_morning_report(
            config, context_db=context_db, owner=owner, now=now
        )
        if delivery is None or not delivery.text.strip():
            return 0
        stream.write(delivery.text)
        stream.flush()
        store = DreamStore(config.dream_state_db)
        if not store.ack_report(delivery.report_id, delivery.owner):
            store.release_report_claim(delivery.report_id, delivery.owner)
            return 1
        return 0
    except Exception:
        if delivery is not None:
            try:
                (store or DreamStore(config.dream_state_db)).release_report_claim(
                    delivery.report_id, delivery.owner
                )
            except Exception:
                pass
        return 1


def _scalar(path: Path, sql: str, parameters: tuple[Any, ...] = ()) -> int:
    """Read one count without creating or migrating a missing database."""

    target = Path(path).expanduser().absolute()
    if not target.is_file() or target.is_symlink():
        return 0
    try:
        uri = target.as_uri() + "?mode=ro"
        # ``with sqlite3.connect(...)`` only commits/rolls back; it never closes
        # the handle. ``closing`` guarantees the read-only connection is closed
        # on both the success and exception paths so descriptors cannot leak.
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute(sql, parameters).fetchone()
        return int(row[0]) if row is not None else 0
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return 0


def _aware_now(value: dt.datetime | None) -> dt.datetime:
    clock = value or dt.datetime.now(dt.timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("morning report clock must be timezone-aware")
    return clock
