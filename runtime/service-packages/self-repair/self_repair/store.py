"""Incident deduplication on the existing durable lifecycle implementation."""
from __future__ import annotations
import hashlib
import fcntl
import json
import os
import time
import uuid
from pathlib import Path
from hermes_second_brain.lifecycle import LifecycleStore

def canonical(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def hash_value(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def private_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    with temp.open('x') as f:
        os.fchmod(f.fileno(),0o600);f.write(canonical(value));f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

class Incidents:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.root.is_symlink():raise ValueError('repair_state_symlink')
        self.lifecycle=LifecycleStore(self.root/'state.sqlite3')
        with self.lifecycle.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS repair_incidents(
                signature TEXT PRIMARY KEY,job_id TEXT NOT NULL,component TEXT NOT NULL,
                code TEXT NOT NULL,versions TEXT NOT NULL,created REAL NOT NULL,
                result TEXT NOT NULL DEFAULT '{}')''')
            c.execute('''CREATE TABLE IF NOT EXISTS repair_sources(
                source_key TEXT PRIMARY KEY,signature TEXT NOT NULL,snapshot_path TEXT NOT NULL,
                outcome TEXT NOT NULL DEFAULT 'pending')''')
            c.execute('''CREATE TABLE IF NOT EXISTS repair_dispatches(
                job_id TEXT NOT NULL,phase TEXT NOT NULL,started REAL NOT NULL,
                thread_id TEXT NOT NULL DEFAULT '',result TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(job_id,phase))''')
            columns={r['name']for r in c.execute('PRAGMA table_info(repair_incidents)')}
            if 'base_signature'not in columns:
                c.execute("ALTER TABLE repair_incidents ADD COLUMN base_signature TEXT NOT NULL DEFAULT ''")
                c.execute('UPDATE repair_incidents SET base_signature=signature')
        from self_repair.guard import RepairGuard
        self.guard=RepairGuard(self.lifecycle)
    def seen(self,key):
        with self.lifecycle.connect() as c:return c.execute('SELECT 1 FROM repair_sources WHERE source_key=?',(key,)).fetchone() is not None
    def record(self,*,source_key,component,code,versions,snapshot):
        if component not in {'mail','briefing'}:raise ValueError('unsupported_component')
        base=hash_value([component,code,versions])
        path=self.root/'originals'/(hash_value(source_key)+'.json')
        # Serialize the lifecycle enqueue and source attachment together. A
        # running/completed bundle is sealed; later originals need a new batch.
        # Cause-level guard budgets still span batches and source versions.
        with (self.root/'incident-record.lock').open('a')as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
            with self.lifecycle.connect()as c:
                old=c.execute('SELECT signature FROM repair_sources WHERE source_key=?',(source_key,)).fetchone()
                if old:return self.get(old['signature'])
                queued=c.execute('''SELECT i.signature FROM repair_incidents i
                    JOIN lifecycle_jobs j USING(job_id) WHERE i.base_signature=? AND j.state='queued'
                    AND j.attempts=0 ORDER BY i.created LIMIT 1''',(base,)).fetchone()
                prior=c.execute('SELECT 1 FROM repair_incidents WHERE signature=?',(base,)).fetchone()
            sig=queued['signature']if queued else hash_value([base,source_key])if prior else base
            private_json(path,snapshot)
            job=self.lifecycle.enqueue_job(idempotency_key='repair:'+sig,job_kind='hermes_repair',max_attempts=2)
            with self.lifecycle.connect()as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute('''INSERT OR IGNORE INTO repair_incidents
                    (signature,job_id,component,code,versions,created,base_signature)VALUES(?,?,?,?,?,?,?)''',
                    (sig,job['job_id'],component,code,canonical(versions),time.time(),base))
                c.execute('INSERT OR IGNORE INTO repair_sources(source_key,signature,snapshot_path) VALUES(?,?,?)',
                    (source_key,sig,str(path)))
        return self.get(sig)
    def get(self,sig):
        with self.lifecycle.connect() as c:
            row=c.execute('SELECT * FROM repair_incidents WHERE signature=?',(sig,)).fetchone()
            refs=c.execute('SELECT * FROM repair_sources WHERE signature=? ORDER BY source_key',(sig,)).fetchall()
        if not row:raise ValueError('unknown_incident')
        return {**dict(row),'versions':json.loads(row['versions']),'result':json.loads(row['result']),
                'sources':[dict(x) for x in refs],'job':self.lifecycle.get_job(row['job_id'])}
    def pending(self):
        with self.lifecycle.connect() as c:
            return [self.get(r['signature']) for r in c.execute('''SELECT i.signature FROM repair_incidents i
                JOIN lifecycle_jobs j ON j.job_id=i.job_id WHERE j.state='queued' ORDER BY i.created''')]
    def lease_fence(self,job_id):
        # The public lifecycle result intentionally omits the lease owner.
        # Read the durable fence rather than reconstructing it from a report.
        with self.lifecycle.connect() as c:
            row=c.execute('SELECT owner,lease_generation FROM lifecycle_jobs WHERE job_id=?',(job_id,)).fetchone()
        if row is None:raise ValueError('unknown_incident_job')
        return dict(row)
    def dispatch(self,job_id,phase,**options):
        return self.guard.reserve(job_id,phase,**options)
    def dispatch_result(self,job_id,phase,result,thread_id=''):
        with self.lifecycle.connect() as c:
            c.execute('UPDATE repair_dispatches SET result=?,thread_id=? WHERE job_id=? AND phase=?',
                      (canonical(result),thread_id,job_id,phase))
    def result(self,sig,value):
        with self.lifecycle.connect() as c:
            c.execute('UPDATE repair_incidents SET result=? WHERE signature=?',(canonical(value),sig))
