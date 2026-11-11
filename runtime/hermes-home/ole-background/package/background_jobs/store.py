"""Concrete workflow state, reusing native LifecycleStore leases and transitions."""
import json
import time
from pathlib import Path
from hermes_second_brain.lifecycle import LifecycleStore
from .contracts import ContractError, PermanentFailure, canonical, digest, private_dir, private_file, PROFILES

class Store:
    def __init__(self, root):
        self.root = private_dir(root)
        self.lifecycle = LifecycleStore(self.root / 'state.sqlite3')
        with self.lifecycle.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS background_jobs(
                job_id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                owner TEXT NOT NULL, session_id TEXT NOT NULL, contract TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'accepting', cancel_requested INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
                receipt_ref TEXT, supersedes TEXT, submission_ref TEXT,
                scope_hash TEXT NOT NULL DEFAULT '', profile TEXT NOT NULL DEFAULT '',
                account TEXT NOT NULL DEFAULT '', total INTEGER NOT NULL DEFAULT 0, runtime_blocker TEXT);
            CREATE TABLE IF NOT EXISTS background_items(
                job_id TEXT NOT NULL, item_id TEXT NOT NULL, identity TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', blocked_reason TEXT,
                PRIMARY KEY(job_id,item_id), FOREIGN KEY(job_id) REFERENCES background_jobs(job_id));
            CREATE INDEX IF NOT EXISTS idx_background_item_state ON background_items(job_id,state);
            CREATE TABLE IF NOT EXISTS background_phases(
                job_id TEXT NOT NULL, item_id TEXT NOT NULL, phase TEXT NOT NULL,
                native_job_id TEXT NOT NULL, PRIMARY KEY(job_id,item_id,phase));
            CREATE TABLE IF NOT EXISTS background_attempts(
                native_job_id TEXT PRIMARY KEY, attempted REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS background_proofs(
                native_job_id TEXT PRIMARY KEY, proof TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS background_controller(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),pid INTEGER NOT NULL,birth TEXT NOT NULL,heartbeat REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS background_worker_births(
                slot INTEGER PRIMARY KEY, worker_id TEXT NOT NULL, pid INTEGER NOT NULL, birth TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS background_revision_requests(
                request_key TEXT PRIMARY KEY,parent_job TEXT UNIQUE NOT NULL,inbound_id TEXT NOT NULL,
                correction TEXT NOT NULL,state TEXT NOT NULL,next_job TEXT,blocker TEXT);
            CREATE TABLE IF NOT EXISTS background_budget_jobs(job_id TEXT PRIMARY KEY,root_job TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS background_item_budgets(
                root_job TEXT NOT NULL,item_id TEXT NOT NULL,iterations INTEGER NOT NULL,
                attempts INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(root_job,item_id));
            CREATE TABLE IF NOT EXISTS background_request_charges(
                root_job TEXT NOT NULL,item_id TEXT NOT NULL,request_id TEXT NOT NULL,
                observed INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(root_job,item_id,request_id));
            CREATE TABLE IF NOT EXISTS background_phase_charges(
                root_job TEXT NOT NULL,item_id TEXT NOT NULL,phase TEXT NOT NULL,
                native_id TEXT NOT NULL,generation INTEGER NOT NULL,
                PRIMARY KEY(root_job,item_id,phase,native_id,generation));
            ''')
        # Additive metadata projection; full contracts are read once for old rows,
        # never for every status/list request.
        with self.lifecycle.connect() as c:
            columns={r['name'] for r in c.execute('PRAGMA table_info(background_jobs)')}
            for name,kind in [('scope_hash',"TEXT NOT NULL DEFAULT ''"),('profile',"TEXT NOT NULL DEFAULT ''"),('account',"TEXT NOT NULL DEFAULT ''"),('total','INTEGER NOT NULL DEFAULT 0'),('runtime_blocker','TEXT')]:
                if name not in columns:c.execute('ALTER TABLE background_jobs ADD COLUMN '+name+' '+kind)
            for row in c.execute("SELECT job_id,contract FROM background_jobs WHERE scope_hash=''").fetchall():
                value=json.loads(row['contract']);c.execute('UPDATE background_jobs SET scope_hash=?,profile=?,account=?,total=? WHERE job_id=?',(digest(value),value['profile'],value['account'],len(value['items']),row['job_id']))
    def row(self, job_id, owner=None, session_id=None):
        with self.lifecycle.connect() as c:
            row = c.execute('SELECT * FROM background_jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None or (owner is not None and (row['owner'] != owner or row['session_id'] != session_id)):
            raise ContractError('unknown_owned_job')
        return dict(row)
    def items(self, job_id):
        with self.lifecycle.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM background_items WHERE job_id=? ORDER BY item_id', (job_id,))]
    def add(self, contract):
        key = digest([contract['owner'], contract['session_id'], contract['request_id']])
        job_id = 'bg_' + key[:32]
        encoded = canonical(contract)
        # Source hashes are pinned now; full acquisition happens in each fenced
        # source phase. Main never reads or retains all original bodies.
        private_dir(self.root / 'sources' / job_id)
        with self.lifecycle.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT contract FROM background_jobs WHERE request_key=?', (key,)).fetchone()
            if old and old['contract'] != encoded: raise ContractError('request_scope_changed')
            c.execute('INSERT OR IGNORE INTO background_jobs(job_id,request_key,owner,session_id,contract,created,supersedes,scope_hash,profile,account,total) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (job_id,key,contract['owner'],contract['session_id'],encoded,time.time(),contract.get('supersedes'),digest(contract),contract['profile'],contract['account'],len(contract['items'])))
            for item in contract['items']:
                c.execute('INSERT OR IGNORE INTO background_items(job_id,item_id,identity) VALUES(?,?,?)', (job_id,item['item_id'],canonical(item)))
            if old is None and contract['profile']=='readonly-research':
                root=contract.get('budget_root',job_id)
                c.execute('INSERT INTO background_budget_jobs VALUES(?,?)',(job_id,root))
                for item in contract['items']:
                    if root==job_id:
                        c.execute('INSERT INTO background_item_budgets(root_job,item_id,iterations,attempts) VALUES(?,?,?,?)',
                                  (root,item['item_id'],contract['per_item_iterations'],contract['per_item_attempts']))
                    elif not c.execute('SELECT 1 FROM background_item_budgets WHERE root_job=? AND item_id=?',(root,item['item_id'])).fetchone():
                        raise ContractError('legacy_iteration_budget_unknown')
        accepted={'contract':'background-accepted-v1','job_id':job_id,'owner':contract['owner'],'session_id':contract['session_id'],'scope_hash':digest(contract),'total':len(contract['items'])}
        ref='accepted/'+job_id+'.'+digest(accepted)+'.json'
        try:private_file(self.root/ref,canonical(accepted).encode())
        except FileExistsError:
            if (self.root/ref).read_bytes()!=canonical(accepted).encode():raise ContractError('submission_proof_conflict')
        with self.lifecycle.connect() as c:
            c.execute("UPDATE background_jobs SET submission_ref=?,state=CASE WHEN state='accepting' THEN 'queued' ELSE state END WHERE job_id=?",(ref,job_id))
        return job_id
    def budget_root(self,job_id):
        with self.lifecycle.connect() as c:
            row=c.execute('SELECT root_job FROM background_budget_jobs WHERE job_id=?',(job_id,)).fetchone()
        return row[0] if row else None
    def budget_state(self,job_id,item_id):
        with self.lifecycle.connect() as c:
            row=c.execute('''SELECT i.* FROM background_item_budgets i JOIN background_budget_jobs b
                ON b.root_job=i.root_job WHERE b.job_id=? AND i.item_id=?''',(job_id,item_id)).fetchone()
        return dict(row) if row else None
    def reserve_phase(self,job_id,item_id,phase,native_id,generation):
        root=self.budget_root(job_id)
        if root is None:return # Existing legacy jobs run unchanged; revision rejects them.
        with self.lifecycle.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            key=(root,item_id,phase,native_id,generation)
            if c.execute('SELECT 1 FROM background_phase_charges WHERE root_job=? AND item_id=? AND phase=? AND native_id=? AND generation=?',key).fetchone():return
            limit=c.execute('SELECT attempts FROM background_item_budgets WHERE root_job=? AND item_id=?',(root,item_id)).fetchone()[0]
            used=c.execute('SELECT COUNT(*) FROM background_phase_charges WHERE root_job=? AND item_id=? AND phase=?',(root,item_id,phase)).fetchone()[0]
            if used>=limit:raise PermanentFailure('item_retry_budget_exhausted')
            c.execute('INSERT INTO background_phase_charges VALUES(?,?,?,?,?)',key)
    def reserve_request(self,job_id,item_id,native_id,owner,generation,request_id,*,observed=False,check_only=False):
        if not isinstance(request_id,str) or not request_id:raise ContractError('native_request_binding_required')
        with self.lifecycle.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            fence=c.execute('''SELECT l.*,j.cancel_requested,b.root_job FROM lifecycle_jobs l
                JOIN background_phases p ON p.native_job_id=l.job_id JOIN background_jobs j ON j.job_id=p.job_id
                JOIN background_budget_jobs b ON b.job_id=j.job_id
                WHERE p.job_id=? AND p.item_id=? AND p.phase='analysis' AND l.job_id=?''',(job_id,item_id,native_id)).fetchone()
            if (not fence or fence['cancel_requested'] or fence['owner']!=owner or fence['lease_generation']!=generation
                    or fence['lease_until']<=time.time() or fence['state'] not in ('running','checkpointed')):
                raise ContractError('native_lease_lost')
            if check_only:return
            key=(fence['root_job'],item_id,request_id)
            charged=c.execute('SELECT 1 FROM background_request_charges WHERE root_job=? AND item_id=? AND request_id=?',key).fetchone()
            if observed:
                if not charged:raise ContractError('uncharged_native_request')
                c.execute('UPDATE background_request_charges SET observed=1 WHERE root_job=? AND item_id=? AND request_id=?',key)
                return
            if charged:return
            changed=c.execute('UPDATE background_item_budgets SET used=used+1 WHERE root_job=? AND item_id=? AND used<iterations',key[:2]).rowcount
            if changed!=1:raise ContractError('iteration_budget_exhausted')
            c.execute('INSERT INTO background_request_charges(root_job,item_id,request_id) VALUES(?,?,?)',key)
    def request_revision(self,job_id,owner,session_id,expected_revision,inbound_id,correction):
        key=digest([owner,session_id,inbound_id])
        with self.lifecycle.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            previous=c.execute('SELECT * FROM background_revision_requests WHERE request_key=?',(key,)).fetchone()
            if previous:
                if previous['parent_job']!=job_id or previous['correction']!=correction:raise ContractError('revision_inbound_conflict')
                return dict(previous)
            row=c.execute('SELECT * FROM background_jobs WHERE job_id=? AND owner=? AND session_id=?',(job_id,owner,session_id)).fetchone()
            if not row:raise ContractError('unknown_owned_job')
            if row['profile']!='readonly-research':raise ContractError('revision_profile_unsupported')
            if row['revision']!=expected_revision:raise ContractError('revision_changed')
            if not c.execute('SELECT 1 FROM background_budget_jobs WHERE job_id=?',(job_id,)).fetchone():raise ContractError('legacy_iteration_budget_unknown')
            if c.execute('SELECT 1 FROM background_revision_requests WHERE parent_job=?',(job_id,)).fetchone():raise ContractError('job_already_revised')
            if row['state'] in ('completed','completed_with_blockers','cancelled'):raise ContractError('revision_requires_live_job')
            c.execute('INSERT INTO background_revision_requests VALUES(?,?,?,?,?,?,?)',(key,job_id,inbound_id,correction,'requested',None,None))
            c.execute("UPDATE background_jobs SET cancel_requested=1,state='cancelling',revision=revision+1 WHERE job_id=?",(job_id,))
            # Same DB transaction as admission: stale owners cannot complete after this fence.
            c.execute("""UPDATE lifecycle_jobs SET state='cancelled',lease_generation=lease_generation+1,lease_until=0
                WHERE job_id IN (SELECT native_job_id FROM background_phases WHERE job_id=?)
                AND state IN ('running','checkpointed','verifying')""",(job_id,))
        return {'request_key':key,'parent_job':job_id,'state':'requested','next_job':None}
    def plan_item(self,job_id,item_id):
        contract=json.loads(self.row(job_id)['contract'])
        if item_id not in {v['item_id'] for v in contract['items']}:raise ContractError('unknown_owned_item')
        for phase in PROFILES[contract['profile']]:
            native=self.lifecycle.enqueue_job(idempotency_key='background:'+digest([job_id,item_id,phase]),
                job_kind='background_'+phase,max_attempts=contract['per_item_attempts'])
            with self.lifecycle.connect() as c:
                c.execute('INSERT OR IGNORE INTO background_phases VALUES(?,?,?,?)',(job_id,item_id,phase,native['job_id']))
    def phase_id(self, job_id, item_id, phase):
        with self.lifecycle.connect() as c:
            r = c.execute('SELECT native_job_id FROM background_phases WHERE job_id=? AND item_id=? AND phase=?', (job_id,item_id,phase)).fetchone()
        if not r:
            self.plan_item(job_id,item_id)
            with self.lifecycle.connect() as c:r=c.execute('SELECT native_job_id FROM background_phases WHERE job_id=? AND item_id=? AND phase=?',(job_id,item_id,phase)).fetchone()
        if not r:raise ContractError('phase_plan_missing')
        return r[0]
    def attempted(self, native_id):
        with self.lifecycle.connect() as c: return bool(c.execute('SELECT 1 FROM background_attempts WHERE native_job_id=?', (native_id,)).fetchone())
    def mark_attempt(self, native_id, owner, generation):
        with self.lifecycle.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r = c.execute('SELECT * FROM lifecycle_jobs WHERE job_id=?', (native_id,)).fetchone()
            if r['owner'] != owner or r['lease_generation'] != generation or r['lease_until'] <= time.time() or r['state'] not in {'running','checkpointed'}:
                raise ContractError('phase_fence_expired')
            if c.execute('SELECT 1 FROM background_attempts WHERE native_job_id=?', (native_id,)).fetchone(): return False
            c.execute('INSERT INTO background_attempts VALUES(?,?)', (native_id,time.time()))
        return True
    def set_item(self, job_id, item_id, state, reason=None):
        with self.lifecycle.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE background_items SET state=?,blocked_reason=? WHERE job_id=? AND item_id=?', (state,reason,job_id,item_id))
            c.execute('UPDATE background_jobs SET revision=revision+1 WHERE job_id=?', (job_id,))
    def snapshot_path(self, job_id, item_id):
        return self.root / 'sources' / job_id / (digest(item_id)+'.original')
    def controller_alive(self,conn):
        from .processes import alive
        controller=conn.execute('SELECT pid,birth FROM background_controller WHERE singleton=1').fetchone()
        return bool(controller and alive(controller['pid'],controller['birth']))
    @staticmethod
    def public(row,counts,alive):
        state=row['state']
        if not alive and state=='running':state='paused'
        return dict(job_id=row['job_id'],owner=row['owner'],session_id=row['session_id'],state=state,runner_alive=alive,
            total=row['total'],**counts,cancel_requested=bool(row['cancel_requested']),runtime_blocker=row['runtime_blocker'],revision=row['revision'],receipt_ref=row['receipt_ref'],
            accepted=bool(row['submission_ref']),submission_ref=row['submission_ref'],scope_hash=row['scope_hash'],profile=row['profile'],
            processed=counts['verified'],result_review_required=True,verified_definition='native_phase_readback_only',content_approval=None,
            ready_for_send=False,fixture_only=row['profile']=='fixture-outreach',blocked_items_in_receipt=True)
    def status(self,job_id,owner=None,session_id=None):
        with self.lifecycle.connect() as c:
            row=c.execute('SELECT job_id,owner,session_id,state,total,cancel_requested,revision,receipt_ref,submission_ref,scope_hash,profile,runtime_blocker FROM background_jobs WHERE job_id=?',(job_id,)).fetchone()
            if row is None or (owner is not None and (row['owner']!=owner or row['session_id']!=session_id)):raise ContractError('unknown_owned_job')
            raw=dict(c.execute('SELECT state,COUNT(*) FROM background_items WHERE job_id=? GROUP BY state',(job_id,)))
            counts={s:raw.get(s,0) for s in ('pending','running','verified','blocked')}
            alive=self.controller_alive(c)
        return self.public(row,counts,alive)
    def recent_owned(self,owner,session_id):
        with self.lifecycle.connect() as c:
            rows=c.execute("""SELECT j.job_id,j.owner,j.session_id,j.state,j.total,j.cancel_requested,j.revision,j.receipt_ref,j.submission_ref,j.scope_hash,j.profile,j.runtime_blocker,
                SUM(CASE WHEN i.state='pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN i.state='running' THEN 1 ELSE 0 END) AS running,
                SUM(CASE WHEN i.state='verified' THEN 1 ELSE 0 END) AS verified,
                SUM(CASE WHEN i.state='blocked' THEN 1 ELSE 0 END) AS blocked
                FROM background_jobs j LEFT JOIN background_items i ON j.job_id=i.job_id
                WHERE j.owner=? AND j.session_id=? GROUP BY j.job_id ORDER BY j.created DESC LIMIT 100""",(owner,session_id)).fetchall()
            alive=self.controller_alive(c)
        return [self.public(row,{s:int(row[s] or 0) for s in ('pending','running','verified','blocked')},alive) for row in rows]
    def item_page(self,job_id,owner,session_id,*,offset=0,length=100):
        if type(offset) is not int or offset<0 or type(length) is not int or not 1<=length<=100:raise ContractError('bounded_item_page')
        # Targeted detail is separate from the constant-size control response.
        with self.lifecycle.connect() as c:
            job=c.execute('SELECT total FROM background_jobs WHERE job_id=? AND owner=? AND session_id=?',(job_id,owner,session_id)).fetchone()
            if not job:raise ContractError('unknown_owned_job')
            rows=[dict(r) for r in c.execute('SELECT item_id,state,blocked_reason FROM background_items WHERE job_id=? ORDER BY item_id LIMIT ? OFFSET ?',(job_id,length,offset))]
        end=offset+len(rows)
        return {'job_id':job_id,'items':rows,'next_offset':end if end<job['total'] else None,'total':job['total'],'truncated':False}
    def update_job(self, job_id, **fields):
        if set(fields)-{'state','cancel_requested','receipt_ref','runtime_blocker'}: raise ContractError('invalid_job_update')
        with self.lifecycle.connect() as c:
            c.execute('UPDATE background_jobs SET '+','.join(k+'=?' for k in fields)+',revision=revision+1 WHERE job_id=?', (*fields.values(),job_id))
    def completion(self, job_id):
        items = self.items(job_id)
        if any(i['state'] in {'pending','running'} for i in items): return
        row = self.row(job_id)
        if row['cancel_requested']: return
        with self.lifecycle.connect() as c:
            proofs = [dict(r) for r in c.execute('SELECT item_id,phase,proof FROM background_phases JOIN background_proofs USING(native_job_id) WHERE job_id=? ORDER BY item_id,phase', (job_id,))]
        receipt = {'contract':'background-receipt-v1','job_id':job_id,'scope_hash':digest(json.loads(row['contract'])),
                   'items':[{'item_id':i['item_id'],'identity':json.loads(i['identity']),'state':i['state'],'blocked_reason':i['blocked_reason']} for i in items],
                   'proofs':proofs,'email_send_enabled':False,'external_writes_enabled':False,'profile':json.loads(row['contract'])['profile'],
                   'result_review_required':True,'verified_definition':'native_phase_readback_only','content_approval':None,'ready_for_send':False}
        ref = 'receipts/'+job_id+'.'+digest(receipt)+'.json'
        try: private_file(self.root/ref,canonical(receipt).encode())
        except FileExistsError:
            if (self.root/ref).read_bytes()!=canonical(receipt).encode(): raise ContractError('receipt_conflict')
        self.update_job(job_id,state='completed_with_blockers' if any(i['state']=='blocked' for i in items) else 'completed',receipt_ref=ref)
