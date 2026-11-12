"""Shared SMTP day reservations in the existing native SQLite, not a new queue.

Every reservation, including an interrupted or unknown probe, consumes capacity.
A mid-day enforcement cutover cannot manufacture an empty history for that day.
"""
from dataclasses import dataclass
from datetime import datetime,timezone,time as datetime_time
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from background_jobs.contracts import ContractError,PermanentFailure,canonical,digest

BERLIN=ZoneInfo('Europe/Berlin')


@dataclass(frozen=True)
class ProbePermit:
    budget: object
    key: str
    scope: object
    recipient: str
    day: str
    def check(self):
        self.scope.check();proof=self.budget._verified()
        with self.budget.lifecycle.connect()as c:
            row=c.execute('SELECT * FROM outreach_smtp_reservations WHERE attempt_key=?',(self.key,)).fetchone()
            epoch=c.execute('SELECT fingerprint FROM outreach_smtp_enforcement_epoch').fetchone()
        if not epoch or epoch['fingerprint']!=proof['fingerprint']:raise ContractError('smtp_shared_enforcement_changed')
        if (not row or row['native_scope']!=canonical(self.scope.binding)or row['recipient_sha256']!=digest(self.recipient.encode())or
            row['day']!=self.day or row['original_ref']is not None):raise ContractError('smtp_probe_permit_changed')
        if self.budget.now().astimezone(BERLIN).date().isoformat()!=self.day:raise ContractError('smtp_probe_day_changed')
        return row


class NativeSMTPBudget:
    def __init__(self,lifecycle,enforcer,*,clock=lambda:datetime.now(timezone.utc),original_root=None):
        self.lifecycle,self.enforcer,self.clock=lifecycle,enforcer,clock
        from pathlib import Path
        self.original_root=Path(original_root)if original_root is not None else Path(enforcer.root)
        with lifecycle.connect()as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS outreach_smtp_enforcement_epoch(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),since_utc TEXT NOT NULL,fingerprint TEXT NOT NULL,evidence TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outreach_smtp_reservations(
                attempt_key TEXT PRIMARY KEY,day TEXT NOT NULL,recipient_sha256 TEXT NOT NULL,native_scope TEXT NOT NULL,
                reserved_at TEXT NOT NULL,original_ref TEXT,original_sha256 TEXT);
                CREATE INDEX IF NOT EXISTS outreach_smtp_day_reservations ON outreach_smtp_reservations(day);''')
    def now(self):
        now=self.clock()
        if not isinstance(now,datetime)or now.tzinfo is None:raise ContractError('smtp_aware_native_clock_required')
        return now
    def _verified(self):
        # enforcer is the host's real installed/candidate writer verifier, never
        # a JSON assertion supplied by an SMTP or model caller.
        value=self.enforcer.verify()
        if (not isinstance(value,dict)or value.get('contract')!='outreach-enforced-writer-set-v2'or
            not isinstance(value.get('fingerprint'),str)or len(value['fingerprint'])!=64 or
            not value.get('native_evidence_ref')or not value.get('native_evidence_sha256')or
            not ('smtp'in value.get('supported_routes',[])or
                 ('smtp_native'in value.get('supported_routes',[])and value.get('coverage_scope')=='private_candidate_runtime'))):
            raise ContractError('smtp_writer_enforcement_unproved')
        self.enforcer.check_original(value)
        return value
    def establish_epoch(self):
        proof=self._verified();now=self.now()
        with self.lifecycle.connect()as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM outreach_smtp_enforcement_epoch').fetchone()
            if old:
                if old['fingerprint']!=proof['fingerprint']:raise ContractError('smtp_enforcement_epoch_changed')
                return dict(old)
            c.execute('INSERT INTO outreach_smtp_enforcement_epoch VALUES(1,?,?,?)',
                      (now.isoformat(),proof['fingerprint'],canonical(proof)))
            return {'since_utc':now.isoformat(),'fingerprint':proof['fingerprint'],'evidence':canonical(proof)}
    def status(self):
        proof=self._verified();now=self.now();day=now.astimezone(BERLIN).date()
        with self.lifecycle.connect()as c:
            epoch=c.execute('SELECT * FROM outreach_smtp_enforcement_epoch').fetchone()
            used=c.execute('SELECT COUNT(*) FROM outreach_smtp_reservations WHERE day=?',(day.isoformat(),)).fetchone()[0]
        start=datetime.combine(day,datetime_time.min,tzinfo=BERLIN)
        complete=bool(epoch and epoch['fingerprint']==proof['fingerprint']and datetime.fromisoformat(epoch['since_utc'])<=start)
        return {'day':day.isoformat(),'known_reserved':used,'used':used if complete else None,
                'remaining':200-used if complete else None,'history_complete':complete,'limit':200}
    def reserve(self,scope,recipient,attempt_key):
        scope.check();proof=self._verified();now=self.now();day=now.astimezone(BERLIN).date()
        if not isinstance(attempt_key,str)or not attempt_key or len(attempt_key)>256:raise ContractError('smtp_native_attempt_key_required')
        if not isinstance(recipient,str)or recipient!=recipient.strip().lower()or '@'not in recipient:
            raise ContractError('smtp_exact_existing_recipient_required')
        with self.lifecycle.connect()as c:
            schema='main'
            if scope.lifecycle.db_path!=self.lifecycle.db_path:
                c.execute('ATTACH DATABASE ? AS native_scope',(str(scope.lifecycle.db_path),));schema='native_scope'
            c.execute('BEGIN IMMEDIATE');scope.check_sql(c,schema=schema)
            epoch=c.execute('SELECT * FROM outreach_smtp_enforcement_epoch').fetchone()
            start=datetime.combine(day,datetime_time.min,tzinfo=BERLIN)
            if not epoch or epoch['fingerprint']!=proof['fingerprint']:
                raise ContractError('smtp_shared_enforcement_epoch_missing_or_changed')
            if datetime.fromisoformat(epoch['since_utc'])>start:raise PermanentFailure('smtp_current_day_prior_usage_unknown')
            prior=c.execute('SELECT * FROM outreach_smtp_reservations WHERE attempt_key=?',(attempt_key,)).fetchone()
            if prior:
                if prior['recipient_sha256']!=digest(recipient.encode())or prior['native_scope']!=canonical(scope.binding):
                    raise ContractError('smtp_attempt_binding_changed')
                # A persisted reservation without a source is still consumed.
                # A cold worker cannot infer that no RCPT was issued.
                raise PermanentFailure('smtp_existing_attempt_requires_original_readback')
            used=c.execute('SELECT COUNT(*) FROM outreach_smtp_reservations WHERE day=?',(day.isoformat(),)).fetchone()[0]
            if used>=200:raise PermanentFailure('smtp_daily_quota_exhausted')
            c.execute('INSERT INTO outreach_smtp_reservations VALUES(?,?,?,?,?,NULL,NULL)',
                      (attempt_key,day.isoformat(),digest(recipient.encode()),canonical(scope.binding),now.isoformat()))
        return ProbePermit(self,attempt_key,scope,recipient,day.isoformat())
    def finish(self,permit,original_ref,original_sha256):
        # Recording the already obtained original remains possible after Cancel;
        # this grants no permission for another probe or business operation.
        from background_jobs.contracts import approved_path,private_file
        raw=approved_path(self.original_root,original_ref).read_bytes()
        if digest(raw)!=original_sha256:raise ContractError('smtp_probe_original_changed')
        value=json.loads(raw)['original']
        if (value.get('quota_authority',{}).get('attempt_key')!=permit.key or
            value.get('identity',{}).get('item',{}).get('account')!=permit.scope.account or
            value.get('candidate',{}).get('to')!=permit.recipient):
            raise ContractError('smtp_attempt_binding_changed')
        copies=[(original_ref,raw,original_sha256)]
        trace_ref=value.get('trace_ref')
        if trace_ref:
            trace=approved_path(self.original_root,trace_ref).read_bytes()
            if digest(trace)!=value.get('trace_sha256'):raise ContractError('smtp_probe_original_changed')
            copies.append((trace_ref,trace,value['trace_sha256']))
        for ref,data,sha in copies:
            target=Path(self.enforcer.root)/ref
            try:private_file(target,data)
            except FileExistsError:
                if digest(approved_path(self.enforcer.root,ref).read_bytes())!=sha:
                    raise ContractError('smtp_probe_original_changed')
        with self.lifecycle.connect()as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM outreach_smtp_reservations WHERE attempt_key=?',(permit.key,)).fetchone()
            if not row or row['native_scope']!=canonical(permit.scope.binding)or row['recipient_sha256']!=digest(permit.recipient.encode()):
                raise ContractError('smtp_attempt_binding_changed')
            if row['original_ref']is not None and (row['original_ref'],row['original_sha256'])!=(original_ref,original_sha256):
                raise ContractError('smtp_probe_original_changed')
            c.execute('UPDATE outreach_smtp_reservations SET original_ref=?,original_sha256=? WHERE attempt_key=?',
                      (original_ref,original_sha256,permit.key))

    def verify_original(self,attempt_key,sha256,recipient):
        from background_jobs.contracts import approved_path
        with self.lifecycle.connect()as c:
            row=c.execute('SELECT * FROM outreach_smtp_reservations WHERE attempt_key=?',(attempt_key,)).fetchone()
        if (not row or row['recipient_sha256']!=digest(recipient.encode())or row['original_sha256']!=sha256 or
            not row['original_ref']or digest(approved_path(self.enforcer.root,row['original_ref']).read_bytes())!=sha256):
            raise ContractError('smtp_shared_original_receipt_required')
        value=json.loads(approved_path(self.enforcer.root,row['original_ref']).read_bytes())['original']
        if value.get('trace_ref')and digest(approved_path(self.enforcer.root,value['trace_ref']).read_bytes())!=value.get('trace_sha256'):
            raise ContractError('smtp_probe_original_changed')
        return dict(row)
