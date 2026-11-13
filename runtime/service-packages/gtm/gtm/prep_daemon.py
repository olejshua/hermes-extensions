"""Dry-run-first preparation pass: plan, collect permitted evidence, render, deliver once.

Everything outside the pure planner is injected, so the pass is testable without
accounts: `reads` (calendar), `evidence` (permitted sources), `drafter` (turns
evidence into questions) and `sender` (private owner channel). Only the sender
can mark anything delivered. A rendered note is never reported as delivered.

Not wired into the live services: activation goes through the normal source
release path and needs an explicit owner-reviewed permission file.
"""
from __future__ import annotations
import datetime as dt
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo
from gtm.prep import PrepConfig, due_preparation, render_prep

MAX_ITEM_CHARS = 1200
MAX_RETRIES = 2  # definite delivery failures only; an uncertain send is never replayed
RETRY_AFTER_SECONDS = 600


class PrepReview(ValueError):
    pass


class PrepStore:
    """States: claimed, rendered, send_attempted, delivered, delivery_failed,
    delivery_unknown, review_required. Only delivered carries a receipt."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS prep(identity TEXT PRIMARY KEY, event_id TEXT NOT NULL, '
                       'start TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, '
                       'message TEXT, receipt TEXT, code TEXT, updated REAL NOT NULL)')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, identity):
        with self.connect() as db:
            row = db.execute('SELECT * FROM prep WHERE identity=?', (identity,)).fetchone()
        return dict(row) if row else None

    def claim(self, due, clock):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM prep WHERE identity=?', (due.identity,)).fetchone():
                return False
            db.execute('INSERT INTO prep(identity,event_id,start,state,updated) VALUES(?,?,?,?,?)',
                       (due.identity, due.event_id, due.starts_at.isoformat(), 'claimed', clock))
        return True

    def begin_send(self, identity, clock):
        """Atomic rendered -> send_attempted. Only one caller can win, so
        concurrent ticks or a resumed job never send twice."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE prep SET state='send_attempted', attempts=attempts+1, updated=? "
                                 "WHERE identity=? AND state IN ('rendered','delivery_failed')", (clock, identity)).rowcount
        return changed == 1

    def settle_stale(self, clock, max_age=300):
        """A process that died mid-send left send_attempted behind. The send may or
        may not have happened, so it becomes unknown and is never replayed."""
        with self.connect() as db:
            return db.execute("UPDATE prep SET state='delivery_unknown', code='stale_send_attempt', updated=? "
                              "WHERE state='send_attempted' AND ?-updated>?", (clock, clock, max_age)).rowcount

    def set(self, identity, state, clock, *, message=None, receipt=None, code=None, attempt=False, only_from=None):
        if receipt is not None and state != 'delivered':
            raise PrepReview('receipt_only_for_delivered')
        guard = ''
        args = [state, message, receipt, code, int(attempt), clock, identity]
        if only_from:
            guard = ' AND state IN (' + ','.join('?' * len(only_from)) + ')'
            args += list(only_from)
        with self.connect() as db:
            return db.execute('UPDATE prep SET state=?, message=coalesce(?,message), receipt=coalesce(?,receipt), '
                              'code=?, attempts=attempts+?, updated=? WHERE identity=?' + guard, args).rowcount == 1

def permitted_evidence(items, allowed_sources):
    """Keep only owner-allowed sources with a real reference. Returns (kept, dropped_sources).

    Nothing is widened to make a note richer: items from other sources are
    dropped and only named as a gap.
    """
    allowed = {s.casefold() for s in allowed_sources}
    kept, dropped = [], set()
    for item in items:
        source = str(item.get('source', '')).casefold()
        if source not in allowed:
            dropped.add(source or 'unknown'); continue
        if not item.get('ref') or not isinstance(item.get('text'), str) or not item['text'].strip():
            continue
        kept.append(dict(source=source, ref=str(item['ref']), text=item['text'].strip()[:MAX_ITEM_CHARS]))
    return kept, sorted(dropped)


def validate_draft(draft, evidence):
    """Every known fact and question must cite a collected reference."""
    refs = {e['ref'] for e in evidence}
    def keep(entries):
        return [e['text'].strip() for e in entries if isinstance(e, dict) and isinstance(e.get('text'), str)
                and e['text'].strip() and e.get('ref') in refs]
    return keep(draft.get('known', [])), keep(draft.get('open_questions', []))


def calendar_facts(event, tz):
    start = dt.datetime.fromisoformat(event['start']['dateTime'].replace('Z', '+00:00')).astimezone(ZoneInfo(tz))
    names = [a.get('displayName') or a['email'].split('@')[0] for a in event.get('attendees', [])
             if a.get('email') and not a.get('self') and not a.get('resource') and a.get('responseStatus') != 'declined']
    return start, names


def prepare_note(event, evidence, drafter, gaps, tz='Europe/Berlin'):
    start, names = calendar_facts(event, tz)
    known, questions = validate_draft(drafter(event, evidence) if evidence else {}, evidence)
    days = ('Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So')
    label = f'{days[start.weekday()]} {start:%H:%M} Uhr'
    title = str(event.get('summary') or 'Termin')
    known = known or [f'Im Kalender: {title}, {label}' + (f', mit {", ".join(names)}' if names else '') + '.']
    # The drafter can fail to produce usable questions; one honest question beats an invented list.
    questions = questions or ['Was soll ich dafür vorbereiten, und welches Ergebnis willst du aus dem Termin mitnehmen?']
    return render_prep(time_label=label, title=title, people=names, known=known[:5],
                       open_questions=questions[:5], gaps=gaps[:3] or None)


def tick(reads, store, sender, evidence, drafter, *, now, config=None, allowed_sources=(), own_addresses=(),
         activate=False, tz='Europe/Berlin'):
    """One pass. Report keys separate planned, rendered and delivered counts."""
    config = config or PrepConfig()
    report = dict(due=0, rendered=0, delivered=0, failed_delivery=0, unknown_delivery=0, review=0,
                  retried=0, dry_run=not activate, notes=[])
    clock = now.timestamp()
    if activate:
        report['stale_settled'] = store.settle_stale(clock)
    for event in reads.events(now):
        try:
            due = due_preparation(event, calendar_id=reads.calendar_id(event), own_addresses=own_addresses,
                                  now=now, config=config, tz=tz)
        except ValueError:
            report['review'] += 1; continue
        if due is None:
            continue
        if isinstance(due, dict):
            report['review'] += 1; continue
        report['due'] += 1
        row = store.get(due.identity)
        if row and row['state'] in ('delivered', 'send_attempted', 'delivery_unknown', 'review_required'):
            continue  # done, in flight, or uncertain: never replay
        if row and row['state'] == 'delivery_failed':
            if row['attempts'] > MAX_RETRIES or clock - row['updated'] < RETRY_AFTER_SECONDS:
                continue
            report['retried'] += 1
        if not activate:
            note = _render(reads, evidence, drafter, event, allowed_sources, tz, report)
            if note:
                report['notes'].append(note)
            continue
        if not row and not store.claim(due, clock):
            continue
        if not row or row['state'] == 'claimed':
            note = _render(reads, evidence, drafter, event, allowed_sources, tz, report)
            if note is None:
                store.set(due.identity, 'review_required', clock, code='render_failed', only_from=('claimed',)); continue
            store.set(due.identity, 'rendered', clock, message=note, only_from=('claimed',))
        else:
            note = row['message']
        # Re-read the event before sending: a moved or cancelled meeting must not get a stale note.
        fresh = reads.current_event(due.event_id)
        again = due_preparation(fresh, calendar_id=due.calendar_id, own_addresses=own_addresses,
                                now=now, config=config, tz=tz)
        if not hasattr(again, 'identity') or again.identity != due.identity:
            store.set(due.identity, 'review_required', clock, code='event_changed', only_from=('claimed', 'rendered', 'delivery_failed')); report['review'] += 1; continue
        if not store.begin_send(due.identity, clock):
            continue  # another tick owns this send
        try:
            result = sender.send(due.identity, note)
        except Exception:
            result = None
        if isinstance(result, dict) and result.get('success') is True and result.get('message_id'):
            store.set(due.identity, 'delivered', clock, receipt=str(result['message_id']), only_from=('send_attempted', 'delivery_unknown')); report['delivered'] += 1
        elif isinstance(result, dict) and result.get('success') is False and not result.get('message_id'):
            store.set(due.identity, 'delivery_failed', clock, code='send_rejected', only_from=('send_attempted',)); report['failed_delivery'] += 1
        else:
            store.set(due.identity, 'delivery_unknown', clock, code='no_receipt', only_from=('send_attempted',)); report['unknown_delivery'] += 1
    return report


def _render(reads, evidence, drafter, event, allowed_sources, tz, report):
    try:
        try:
            raw, failed = evidence.collect(event, tuple(allowed_sources)), False
        except Exception:  # source rejected or unreachable: say so, still prepare from the calendar
            raw, failed = [], True
        items, dropped = permitted_evidence(raw, allowed_sources)
        gaps = ['Eine freigegebene Quelle war nicht erreichbar.'] if failed else (
            ['Keine freigegebene Quelle lieferte Kontext.'] if not items else [])
        if dropped:
            gaps.append('Nicht freigegebene Quellen wurden nicht verwendet: ' + ', '.join(dropped) + '.')
        note = prepare_note(event, items, drafter, gaps, tz)
        report['rendered'] += 1
        return note
    except (ValueError, KeyError):
        report['review'] += 1
        return None
