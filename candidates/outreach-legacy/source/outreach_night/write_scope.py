"""Process-local capabilities derived from the existing native lease and scope.

No request JSON, policy boolean, command flag or new registry creates a scope.
Background callers supply their real PhaseEngine active callback; GTM callers
receive the stamp directly from Coordinator after its native durable Attempt.
"""
from dataclasses import dataclass
import json
import time
from background_jobs.contracts import ContractError,canonical,digest


@dataclass(frozen=True)
class NativeWriteScope:
    lifecycle: object
    kind: str
    identity: dict
    native_id: str
    owner: str
    generation: int
    phase: str
    account: str
    request_key: str
    active: object

    @classmethod
    def background(cls,store,identity,active):
        if not callable(active):raise ContractError('native_active_fence_required')
        active();native=store.phase_id(identity['job_id'],identity['item']['item_id'],identity['phase'])
        row=store.row(identity['job_id']);batch=json.loads(row['contract'])
        if (identity['scope_hash']!=digest(batch)or identity['item']not in batch['items']or
            batch['account']!=identity['item']['account']):raise ContractError('native_scope_changed')
        with store.lifecycle.connect()as c:
            fence=c.execute('SELECT owner,lease_generation FROM lifecycle_jobs WHERE job_id=?',(native,)).fetchone()
        if not fence or not fence['owner']:raise ContractError('native_active_fence_required')
        scope=cls(store.lifecycle,'background',json.loads(canonical(identity)),native,fence['owner'],fence['lease_generation'],
                  identity['phase'],batch['account'],batch['request_id'],active)
        active();scope.check();return scope

    @classmethod
    def gtm(cls,coordinator,identity,phase,native_id,owner,generation):
        if not ((identity.kind=='reply'and phase=='draft')or(identity.kind=='call'and phase=='call_email_followup')):
            raise ContractError('native_mail_phase_not_admitted')
        if coordinator._job_id(identity,phase)!=native_id:raise ContractError('native_gtm_scope_changed')
        scope=cls(coordinator.store,'gtm',identity.fields(),native_id,owner,generation,phase,identity.account,
                  coordinator._phase_key(identity,phase),lambda:None)
        scope.check();return scope

    @property
    def binding(self):
        return {'native_db':str(self.lifecycle.db_path),'kind':self.kind,'identity':self.identity,'native_id':self.native_id,'owner':self.owner,
                'generation':self.generation,'phase':self.phase,'account':self.account,'request_key':self.request_key}

    def check_sql(self,c,*,now=None,schema="main"):
        if schema not in {"main","native_scope"}:raise ContractError("native_scope_schema_not_allowed")
        clock=time.time()if now is None else now
        row=c.execute(f'SELECT * FROM {schema}.lifecycle_jobs WHERE job_id=?',(self.native_id,)).fetchone()
        if (not row or row['owner']!=self.owner or row['lease_generation']!=self.generation or
            row['state']not in ('running','checkpointed')or row['lease_until']is None or row['lease_until']<=clock):
            raise ContractError('native_write_lease_lost')
        if self.kind=='background':
            identity=self.identity
            bound=c.execute(f'''SELECT p.native_job_id,j.contract,j.scope_hash,j.cancel_requested,i.identity
                FROM {schema}.background_phases p JOIN {schema}.background_jobs j ON j.job_id=p.job_id
                JOIN {schema}.background_items i ON i.job_id=p.job_id AND i.item_id=p.item_id
                WHERE p.job_id=?AND p.item_id=?AND p.phase=?''',
                (identity['job_id'],identity['item']['item_id'],identity['phase'])).fetchone()
            if (not bound or bound['native_job_id']!=self.native_id or bound['cancel_requested']or
                digest(json.loads(bound['contract']))!=identity['scope_hash']or bound['scope_hash']!=identity['scope_hash']or
                json.loads(bound['identity'])!=identity['item']or json.loads(bound['contract'])['account']!=self.account):
                raise ContractError('native_write_scope_or_cancel_changed')
            contract=json.loads(bound['contract'])
            if contract.get('intake_request_id'):
                if c.execute(f'SELECT 1 FROM {schema}.background_revision_requests WHERE parent_job=?',(identity['job_id'],)).fetchone():
                    raise ContractError('native_acquisition_revised')
        elif self.kind=='gtm':
            # request_key is the existing native phase idempotency key, not an
            # arbitrary source ID. Its workflow source is immutable in GTM.
            if row['idempotency_key']!=self.request_key:raise ContractError('native_gtm_scope_changed')
            key=dict(self.identity);key.pop('source_hash')
            workflow=digest(json.dumps(key,sort_keys=True).encode())
            source=c.execute(f'SELECT identity FROM {schema}.gtm_sources WHERE workflow_id=?',(workflow,)).fetchone()
            if not source or json.loads(source[0])!=self.identity:raise ContractError('native_gtm_source_changed')
            attempted=c.execute(f'SELECT 1 FROM {schema}.gtm_attempts WHERE phase_key=?',(self.request_key,)).fetchone()
            if not attempted:raise ContractError('native_gtm_attempt_required')
        else:raise ContractError('native_write_scope_kind_not_allowed')
        return row

    def check(self):
        self.active()
        with self.lifecycle.connect()as c:self.check_sql(c)
        return self.binding
