"""Private origin/outbox; content-free latency measurements are kept separately."""
from __future__ import annotations
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time

def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24]

class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.root.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
            raise PermissionError('unsafe_dialogue_root')
        os.chmod(self.root, 0o700)
        self.db = self.root / 'dialogue.sqlite3'
        try:
            fd = os.open(self.db, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(fd)
        except FileExistsError:
            st = self.db.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise PermissionError('unsafe_dialogue_db')
            os.chmod(self.db, 0o600)
        self.lock = threading.RLock()
        with self.connection() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS origins(session TEXT PRIMARY KEY, owner TEXT, source TEXT);
            CREATE TABLE IF NOT EXISTS outbox(job TEXT, phase TEXT, session TEXT, owner TEXT, status TEXT, attempts INTEGER, updated REAL, PRIMARY KEY(job,phase));
            CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY, event TEXT, t REAL, session_hash TEXT, message_hash TEXT, job_hash TEXT, elapsed_ms REAL, queue_wait_ms REAL);''')
            c.execute("UPDATE outbox SET status='unknown' WHERE status='sending'")

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.db, timeout=0.2)
        c.execute('PRAGMA busy_timeout=200')
        try:
            with c:
                yield c
        finally:
            c.close()

    def origin(self, session, owner, source):
        allowed = ('platform','chat_id','user_id','chat_type','thread_id','profile','scope_id','message_id','transport_profile')
        payload = {k: source.get(k) for k in allowed}
        with self.lock, self.connection() as c:
            c.execute('INSERT OR REPLACE INTO origins VALUES(?,?,?)', (session, owner, json.dumps(payload)))

    def admission(self,session,owner,source,message,*,elapsed_ms=None,queue_wait_ms=None,new_inbound=False,start_turn=False):
        allowed=('platform','chat_id','user_id','chat_type','thread_id','profile','scope_id','message_id','transport_profile')
        payload={k:source.get(k) for k in allowed}
        rows=[('admitted',elapsed_ms,None)]
        if new_inbound:rows.append(('inbound',None,None))
        if start_turn:rows.append(('turn_started',None,queue_wait_ms))
        with self.lock,self.connection() as c:
            c.execute('INSERT OR REPLACE INTO origins VALUES(?,?,?)',(session,owner,json.dumps(payload)))
            c.executemany('INSERT INTO metrics(event,t,session_hash,message_hash,job_hash,elapsed_ms,queue_wait_ms) VALUES(?,?,?,?,?,?,?)',
                          [(event,time.time(),digest(session),digest(message),None,elapsed,wait) for event,elapsed,wait in rows])

    def origins(self):
        with self.lock, self.connection() as c:
            return [(s,o,json.loads(p)) for s,o,p in c.execute('SELECT session,owner,source FROM origins')]

    def reserve(self, job, phase, session, owner):
        with self.lock, self.connection() as c:
            c.execute('INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?,?,?)', (job,phase,session,owner,'pending',0,time.time()))
            c.execute("UPDATE outbox SET status='sending',attempts=attempts+1,updated=? WHERE job=? AND phase=? AND status='pending' AND attempts<3", (time.time(),job,phase))
            return c.execute('SELECT changes()').fetchone()[0] == 1

    def settled(self, job, phase, *, success, certain_not_delivered=False):
        state = 'delivered' if success else ('pending' if certain_not_delivered else 'unknown')
        with self.lock, self.connection() as c:
            c.execute('UPDATE outbox SET status=?,updated=? WHERE job=? AND phase=?', (state,time.time(),job,phase))

    def phase(self, job, phase):
        with self.lock, self.connection() as c:
            row = c.execute('SELECT status FROM outbox WHERE job=? AND phase=?', (job,phase)).fetchone()
            return row[0] if row else None

    def metric(self, event, session=None, message=None, job=None, elapsed_ms=None, queue_wait_ms=None):
        if event not in ('inbound','admitted','queued','turn_started','first_visible','answer_accepted','ack_visible','turn_finished','turn_reconciled','job_final_visible','delivery_unknown'):
            raise ValueError('unknown_metric')
        def number(v):
            return v if isinstance(v,(int,float)) and not isinstance(v,bool) and v >= 0 else None
        with self.lock, self.connection() as c:
            c.execute('INSERT INTO metrics(event,t,session_hash,message_hash,job_hash,elapsed_ms,queue_wait_ms) VALUES(?,?,?,?,?,?,?)',
                      (event,time.time(),digest(session) if session else None,digest(message) if message else None,digest(job) if job else None,number(elapsed_ms),number(queue_wait_ms)))
