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
