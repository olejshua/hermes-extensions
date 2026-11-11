"""Durable cause isolation, feedback gates and measured repair-time circuit breaker."""
from __future__ import annotations
import hashlib
import json
import math
import time

WINDOW_SECONDS=24*3600
MODEL_TIME_SECONDS=2*3600
COOLDOWN_SECONDS=30*60

def cause_key(component,code):
    return hashlib.sha256(json.dumps([component,code],separators=(',',':')).encode()).hexdigest()

class RepairGuard:
    def __init__(self,lifecycle):
        self.lifecycle=lifecycle
        with lifecycle.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            columns={r['name'] for r in c.execute('PRAGMA table_info(repair_dispatches)')}
            for name,kind in [('reserved_seconds','REAL'),('spent_seconds','REAL'),
                              ('finished','REAL NOT NULL DEFAULT 0'),('outcome',"TEXT NOT NULL DEFAULT ''"),
                              ('cause',"TEXT NOT NULL DEFAULT ''"),('retry_key',"TEXT NOT NULL DEFAULT ''")]:
                if name not in columns:c.execute('ALTER TABLE repair_dispatches ADD COLUMN '+name+' '+kind)

    def reserve(self,job_id,phase,*,seconds=2460,retry_key='initial',now=None):
        now=time.time() if now is None else now
        if not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or seconds<=0:
            raise ValueError('invalid_repair_time_reservation')
        def no(code,retry_at=0):return {'allowed':False,'code':code,'retry_at':retry_at}
        with self.lifecycle.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM repair_dispatches WHERE job_id=? AND phase=?',(job_id,phase)).fetchone()
            if old:
                return no('dispatch_already_finished') if old['finished'] else {'allowed':True,'reused':True}
            incident=c.execute('SELECT component,code FROM repair_incidents WHERE job_id=?',(job_id,)).fetchone()
            cause=cause_key(incident['component'],incident['code']) if incident else cause_key('private',job_id)
            if phase.startswith('recovery:'):
                # A single blocked conversation must not freeze first-time
                # preparation of every other conversation. The native case
                # epoch prevents repeats; code builds still share cause limits.
                cause=cause_key('source_recovery',job_id)
            prior=c.execute('SELECT * FROM repair_dispatches WHERE cause=? ORDER BY started DESC,rowid DESC LIMIT 1',(cause,)).fetchone()
            # A changed unrelated file/version must not create a fresh retry
            # budget for an unresolved cause. Other causes remain independent.
            if prior and prior['job_id']!=job_id and prior['outcome']!='passed':return no('same_cause_paused')
            if retry_key!='initial' and c.execute('SELECT 1 FROM repair_dispatches WHERE cause=? AND retry_key=?',(cause,retry_key)).fetchone():
                return no('same_feedback_already_tried')
            family=c.execute('SELECT outcome FROM repair_dispatches WHERE cause=? ORDER BY started DESC,rowid DESC',(cause,)).fetchall()
            unresolved=0
            for row in family:
                if row['outcome']=='passed':break
                unresolved+=1
            if unresolved>=2:return no('cause_attempts_exhausted')
            recent=c.execute("SELECT finished,outcome FROM repair_dispatches WHERE phase LIKE 'build:%' AND outcome IN ('passed','failed') ORDER BY finished DESC,rowid DESC LIMIT 3").fetchall()
            if phase.startswith('build:')and len(recent)==3 and all(r['outcome']=='failed' for r in recent):
                until=recent[0]['finished']+COOLDOWN_SECONDS
                if until>now:return no('repair_circuit_cooldown',until)
            if seconds>MODEL_TIME_SECONDS:return no('automatic_repair_scope_needs_review')
            rows=c.execute('''SELECT started,finished,
                CASE WHEN finished=0 THEN COALESCE(reserved_seconds,CASE WHEN phase LIKE 'recovery:%' THEN 300 ELSE 2460 END)
                     ELSE COALESCE(spent_seconds,reserved_seconds,2460) END AS charged
                FROM repair_dispatches WHERE finished=0 OR finished>?
                ORDER BY CASE WHEN finished=0 THEN 1 ELSE 0 END,finished''',(now-WINDOW_SECONDS,)).fetchall()
            used=sum(r['charged'] for r in rows)
            if used+seconds>MODEL_TIME_SECONDS:
                released=0;until=0  # An active reservation has no known release time.
                for row in rows:
                    if not row['finished']:continue
                    released+=row['charged']
                    if used-released+seconds<=MODEL_TIME_SECONDS:
                        until=row['finished']+WINDOW_SECONDS;break
                return no('repair_time_budget_wait',until)
            c.execute('''INSERT INTO repair_dispatches(job_id,phase,started,reserved_seconds,spent_seconds,cause,retry_key)
                         VALUES(?,?,?,?,0,?,?)''',(job_id,phase,now,seconds,cause,retry_key))
        return {'allowed':True,'reserved_seconds':seconds}

    def spent(self,job_id,phase,seconds):
        if not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or seconds<0:raise ValueError('invalid_repair_time')
        with self.lifecycle.connect() as c:
            c.execute('UPDATE repair_dispatches SET spent_seconds=COALESCE(spent_seconds,0)+? WHERE job_id=? AND phase=? AND finished=0',
                      (seconds,job_id,phase))

    def finish(self,job_id,phase,state,*,now=None):
        passed=state in {'restored','restored_without_code','published','verified_candidate'}
        with self.lifecycle.connect() as c:
            c.execute('UPDATE repair_dispatches SET finished=?,outcome=? WHERE job_id=? AND phase=? AND finished=0',
                      (time.time() if now is None else now,'passed' if passed else 'failed',job_id,phase))

    def settle_abandoned(self,*,now=None):
        """Cold resume: unknown interrupted work keeps its full reservation.

        Queued reservations are reusable; live leases are never settled here.
        This changes accounting only, never native jobs or business data.
        """
        now=time.time() if now is None else now
        with self.lifecycle.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=c.execute('''SELECT d.*,j.state FROM repair_dispatches d JOIN lifecycle_jobs j USING(job_id)
                WHERE d.finished=0 AND j.state IN ('completed','failed','stalled','cancelled')''').fetchall()
            for row in rows:
                known=(row['state']=='completed' and row['spent_seconds'] is not None)
                charge=(row['spent_seconds'] if known else row['reserved_seconds'] if row['reserved_seconds'] is not None
                        else 300 if row['phase'].startswith('recovery:') else 2460)
                c.execute('UPDATE repair_dispatches SET finished=?,spent_seconds=?,outcome=? WHERE job_id=? AND phase=? AND finished=0',
                          (now,charge,'passed' if row['state']=='completed' else 'failed',row['job_id'],row['phase']))
