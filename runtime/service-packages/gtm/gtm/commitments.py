"""Open commitments the owner has made, tracked with evidence, reminded to the owner only.

Entries need a source reference (a mail, note or message the owner can open).
Nothing here sends to anyone but the owner, and reminding never counts as
doing the work. A reminder is marked sent only after a delivery receipt.
"""
from __future__ import annotations
import datetime as dt
import sqlite3
from contextlib import contextmanager
from pathlib import Path

REMIND_EVERY_HOURS = 24
SOON_HOURS = 24


class CommitmentStore:
    def __init__(self, path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS commitment(id INTEGER PRIMARY KEY, text TEXT NOT NULL, ref TEXT NOT NULL UNIQUE, '
                       'due TEXT, state TEXT NOT NULL DEFAULT "open", last_reminder_receipt TEXT, last_reminded REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS digest(id INTEGER PRIMARY KEY, refs TEXT NOT NULL, message TEXT NOT NULL, '
                       'state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, receipt TEXT, updated REAL NOT NULL)')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15); db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def add(self, text, ref, due=None):
        if not text.strip() or not ref.strip():
            raise ValueError('commitment_requires_text_and_source_ref')
        if due is not None:
            moment = dt.datetime.fromisoformat(due)
            if moment.tzinfo is None: raise ValueError('due_requires_offset')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO commitment(text,ref,due) VALUES(?,?,?)', (text.strip(), ref.strip(), due))

    def close(self, ref, state='done'):
        if state not in ('done', 'dropped'): raise ValueError('unknown_state')
        with self.connect() as db:
            db.execute("UPDATE commitment SET state=? WHERE ref=? AND state='open'", (state, ref))

    def due_for_reminder(self, now):
        horizon = now + dt.timedelta(hours=SOON_HOURS); rows = []
        with self.connect() as db:
            for row in db.execute("SELECT * FROM commitment WHERE state='open' AND due IS NOT NULL"):
                if dt.datetime.fromisoformat(row['due']) > horizon: continue
                if row['last_reminded'] and now.timestamp() - row['last_reminded'] < REMIND_EVERY_HOURS * 3600: continue
                rows.append(dict(row))
        return sorted(rows, key=lambda r: r['due'])

    def mark_uncertain(self, refs, now):
        """Cooldown without a receipt: the reminder may have gone out, so do not resend now."""
        with self.connect() as db:
            for ref in refs:
                db.execute('UPDATE commitment SET last_reminded=? WHERE ref=?', (now.timestamp(), ref))

    def mark_reminded(self, refs, receipt, now):
        if not receipt: raise ValueError('receipt_required')
        with self.connect() as db:
            for ref in refs:
                db.execute('UPDATE commitment SET last_reminded=?, last_reminder_receipt=? WHERE ref=?', (now.timestamp(), receipt, ref))


def render_digest(rows, now, tz):
    if not rows: return None
    lines = []
    for row in rows[:5]:
        due = dt.datetime.fromisoformat(row['due']).astimezone(tz)
        status = 'überfällig seit' if dt.datetime.fromisoformat(row['due']) < now else 'fällig'
        lines.append(f"{row['text']} ({status} {due:%d.%m. %H:%M})")
    head = 'Offene Zusage von dir: ' if len(lines) == 1 else 'Offene Zusagen von dir: '
    return head + '; '.join(lines) + '. Soll ich etwas davon vorbereiten? Nichts davon wurde nach außen geschickt.'


RETRY_AFTER_SECONDS = 600
MAX_ATTEMPTS = 3
STALE_SECONDS = 300


def _claim_digest(store, refs, message, clock):
    """One digest at a time: nothing starts while another is in flight, uncertain or recently rejected."""
    import json
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE digest SET state='delivery_unknown' WHERE state='send_attempted' AND ?-updated>?", (clock, STALE_SECONDS))
        busy = db.execute("SELECT 1 FROM digest WHERE state='send_attempted' OR (state='delivery_failed' AND (?-updated<? OR attempts>=?))",
                          (clock, RETRY_AFTER_SECONDS, MAX_ATTEMPTS)).fetchone()
        if busy:
            return None
        for row in db.execute("SELECT refs FROM digest WHERE state IN ('delivered','delivery_unknown') AND ?-updated<?",
                              (clock, REMIND_EVERY_HOURS * 3600)):
            if set(json.loads(row['refs'])) & set(refs):
                return None  # already reminded (or possibly reminded) within the cooldown
        failed = db.execute("SELECT id, attempts FROM digest WHERE state='delivery_failed' ORDER BY id DESC LIMIT 1").fetchone()
        attempts = 1
        if failed:
            db.execute("UPDATE digest SET state='superseded' WHERE id=?", (failed['id'],))
            attempts = failed['attempts'] + 1
        cursor = db.execute("INSERT INTO digest(refs,message,state,attempts,updated) VALUES(?,?,'send_attempted',?,?)",
                            (json.dumps(refs), message, attempts, clock))
        return cursor.lastrowid


def commitment_tick(store, sender, now, tz, *, activate=False):
    """Remind the owner of due commitments. Reports planned, sent and delivered separately.

    A reminder counts only after a sender receipt. A rejected send is retried later
    (bounded). An uncertain send is never replayed: its commitments get a cooldown so
    they come back tomorrow instead of being sent twice now.
    """
    import json
    rows = store.due_for_reminder(now)
    report = dict(due=len(rows), attempted=0, delivered=0, rejected=0, unknown=0, dry_run=not activate)
    message = render_digest(rows, now, tz)
    if not rows or not activate:
        report['message'] = message
        return report
    clock = now.timestamp(); refs = [r['ref'] for r in rows[:5]]
    digest = _claim_digest(store, refs, message, clock)
    if digest is None:
        return report
    report['attempted'] = 1
    try:
        result = sender.send(f'commitments-{digest}', message)
    except Exception:
        result = None
    with store.connect() as db:
        if isinstance(result, dict) and result.get('success') is True and result.get('message_id'):
            db.execute("UPDATE digest SET state='delivered', receipt=?, updated=? WHERE id=?", (str(result['message_id']), clock, digest))
            report['delivered'] = 1
        elif isinstance(result, dict) and result.get('success') is False and not result.get('message_id'):
            db.execute("UPDATE digest SET state='delivery_failed', updated=? WHERE id=? AND state='send_attempted'", (clock, digest))
            report['rejected'] = 1
        else:
            db.execute("UPDATE digest SET state='delivery_unknown', updated=? WHERE id=? AND state='send_attempted'", (clock, digest))
            report['unknown'] = 1
    if report['delivered']:
        store.mark_reminded(refs, str(result['message_id']), now)
    elif report['unknown']:
        store.mark_uncertain(refs, now)
    return report
