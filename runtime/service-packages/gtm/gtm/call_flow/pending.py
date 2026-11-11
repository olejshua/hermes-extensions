"""Durable bounded read-only retries of explicitly requested business recordings."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time


class PendingRequests:
    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink():raise ValueError('symlink_source_requests_root')
        self.root.mkdir(parents=True,mode=0o700,exist_ok=True)
        self.path = self.root / 'source-requests.sqlite3'
        if self.path.is_symlink(): raise ValueError('symlink_source_requests')
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, request TEXT NOT NULL, binding TEXT, state TEXT NOT NULL, workflow_id TEXT, next_read REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, reason TEXT)')
            columns={r['name'] for r in db.execute('PRAGMA table_info(requests)')}
            for name,kind in [('created_at','REAL'),('origin_key','TEXT'),('wake_state','TEXT'),('wake_at','REAL'),('wake_attempts','INTEGER NOT NULL DEFAULT 0'),('completed_phases',"TEXT NOT NULL DEFAULT '[]'")]:
                if name not in columns:db.execute('ALTER TABLE requests ADD COLUMN '+name+' '+kind)
            db.execute('CREATE TABLE IF NOT EXISTS origins (session_key TEXT PRIMARY KEY, source TEXT NOT NULL)')
        self.path.chmod(0o600)
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15); db.row_factory = sqlite3.Row
        try:
            with db:yield db
        finally:db.close()
    def accept(self, args):
        key = hashlib.sha256(args['manual_request_id'].encode()).hexdigest()
        canonical = json.dumps(args,sort_keys=True,ensure_ascii=False)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM requests WHERE id=?',(key,)).fetchone()
            if row and row['request'] != canonical: raise ValueError('manual_request_changed')
            if not row:
                if db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]>=10000:raise ValueError('source_request_budget_exhausted')
                now=time.time()
                db.execute('INSERT INTO requests(id,request,state,next_read,created_at) VALUES(?,?,?,?,?)',(key,canonical,'resolving',now,now))
        return key
    def get(self,key):
        if not isinstance(key,str) or len(key)!=64: raise ValueError('invalid_source_request_id')
        with self.connect() as db:
            row = db.execute('SELECT * FROM requests WHERE id=?',(key,)).fetchone()
        if not row: raise ValueError('unknown_source_request')
        return dict(row)
    def pending(self,key,binding,now=None):
        # No raw transcript is ever stored in this queue.
        exact = {k:binding[k] for k in ('source_id','calendar_event_id','category','account')}
        if any(not isinstance(v,str) or not v for k,v in exact.items() if k!='source_id') or (exact['source_id'] is not None and not isinstance(exact['source_id'],str)):
            raise ValueError('pending_binding_missing')
        encoded=json.dumps(exact,sort_keys=True)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT * FROM requests WHERE id=?',(key,)).fetchone()
            if row['state']=='cancelled':return
            old=json.loads(row['binding']) if row['binding'] else {}
            if any(v is not None and exact[k]!=v for k,v in old.items()):raise ValueError('pending_binding_changed')
            current=time.time() if now is None else now
            if row['created_at'] and current-row['created_at']>86400:
                db.execute('UPDATE requests SET state=?,reason=? WHERE id=?',('source_review','transcript_still_missing_after_one_day',key));return
            delay=min(900,60*2**min(row['attempts'],4))
            db.execute('UPDATE requests SET binding=?,state=?,next_read=?,attempts=attempts+1,reason=? WHERE id=?',
                       (encoded,'source_pending',current+delay,'provider_source_not_ready',key))
    def finish(self,key,workflow_id):
        with self.connect() as db:
            db.execute('UPDATE requests SET workflow_id=? WHERE id=?',(workflow_id,key))
            db.execute("UPDATE requests SET state=?,workflow_id=?,reason=NULL WHERE id=? AND state!='cancelled'",('source_ready',workflow_id,key))
    def cancelled(self,workflow_id):
        with self.connect() as db:
            row=db.execute('SELECT state FROM requests WHERE workflow_id=? ORDER BY rowid DESC LIMIT 1',(workflow_id,)).fetchone()
        return row is not None and row['state']=='cancelled'
    def review(self,key,reason):
        with self.connect() as db:db.execute('UPDATE requests SET state=?,reason=? WHERE id=?',('source_review',reason,key))
    def due(self,now=None,limit=2):
        with self.connect() as db:
            return [dict(r)for r in db.execute('SELECT * FROM requests WHERE state=? AND next_read<=? ORDER BY next_read LIMIT ?',('source_pending',time.time() if now is None else now,max(0,min(2,limit))))]

    def save_origin(self,key,source):
        encoded=json.dumps(source,sort_keys=True)
        with self.connect() as db:db.execute('INSERT OR REPLACE INTO origins VALUES(?,?)',(key,encoded))

    def origins(self):
        with self.connect() as db:return [dict(r)for r in db.execute('SELECT * FROM origins')]

    def bind_origin(self,key,origin):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT origin_key FROM requests WHERE id=?',(key,)).fetchone()
            if row['origin_key'] and row['origin_key']!=origin:raise ValueError('manual_request_origin_changed')
            db.execute('UPDATE requests SET origin_key=? WHERE id=?',(origin,key))

    def waiting_wakes(self):
        with self.connect() as db:return [dict(r) for r in db.execute("""SELECT r.* FROM requests r WHERE origin_key IS NOT NULL AND state='source_ready'
            AND wake_state IN ('foreground','waiting','attempted','accepted')
            AND NOT EXISTS(SELECT 1 FROM requests older WHERE older.workflow_id=r.workflow_id AND older.state!='cancelled' AND older.rowid<r.rowid)
            ORDER BY created_at LIMIT 2""")]

    def alerts(self):
        with self.connect() as db:return [dict(r)for r in db.execute("SELECT * FROM requests WHERE origin_key IS NOT NULL AND state='source_review' AND wake_state IN ('foreground','waiting','attempted','accepted') ORDER BY created_at LIMIT 2")]

    def wake(self,key,state,now=None):
        with self.connect() as db:
            db.execute('UPDATE requests SET wake_state=?,wake_at=?,wake_attempts=wake_attempts+? WHERE id=?',
                       (state,time.time() if now is None else now,int(state=='attempted'),key))

    def progress(self,key,phases):
        """Only a newly completed actual phase renews continuation attempts."""
        completed={name for name,state in phases.items() if state=='completed'}
        with self.connect()as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT completed_phases FROM requests WHERE id=?',(key,)).fetchone()
            previous=set(json.loads(row['completed_phases']))
            if completed-previous:
                db.execute('UPDATE requests SET completed_phases=?,wake_attempts=0 WHERE id=?',
                           (json.dumps(sorted(previous|completed)),key))

    def cancel(self,key):
        self.get(key)
        with self.connect() as db:db.execute("UPDATE requests SET state='cancelled',wake_state='cancelled',reason='cancelled_by_ole' WHERE id=?",(key,))


class PendingSupervisor:
    """Opt-in caller-owned loop; no thread/start at import, no model wake or write target."""
    def __init__(self,runtime):self.runtime=runtime;self.stop=threading.Event()
    def tick(self,now=None):
        results=[]
        for row in self.runtime.requests.due(now):
            results.append(self.runtime.resume_source(row['id']))
        return results
    def run(self):
        while not self.stop.is_set():
            try:self.tick()
            except Exception:pass  # Durable requests retained; never fabricate success.
            self.stop.wait(60)
