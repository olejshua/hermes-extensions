"""Private durable transport receipts. Contains no credentials and emits no content to logs."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from contextlib import contextmanager


def fingerprint(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class Receipts:
    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError('monolith_state_symlink')
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.root.stat().st_uid != os.getuid():
            raise ValueError('monolith_state_owner')
        self.root.chmod(0o700)
        self.lock = os.open(self.root / 'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(self.lock, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.lock)
            raise
        try:
            self.path = self.root / 'receipts.sqlite3'
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_uid != os.getuid():
                    raise ValueError('monolith_receipt_owner')
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            with self.connection() as db:
                db.execute('CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, packet TEXT NOT NULL)')
        except BaseException:
            self.close()
            raise

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, record):
        # The complete reply is committed/fsynced before any completion HTTP request.
        with self.connection() as db:
            db.execute('INSERT INTO receipts VALUES(?,?) ON CONFLICT(id) DO UPDATE SET packet=excluded.packet',
                       (record['id'], json.dumps(record, ensure_ascii=False, sort_keys=True)))

    def get(self, identity):
        with self.connection() as db:
            row = db.execute('SELECT packet FROM receipts WHERE id=?', (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def forget_empty_claim(self, identity):
        with self.connection() as db:
            db.execute('DELETE FROM receipts WHERE id=?', (identity,))

    def pending(self):
        with self.connection() as db:
            packets = [json.loads(row[0]) for row in db.execute('SELECT packet FROM receipts ORDER BY rowid')]
        return [row for row in packets if row['phase'] not in ('delivered', 'empty')]

    def close(self):
        if getattr(self, 'lock', None) is not None:
            os.close(self.lock); self.lock = None
