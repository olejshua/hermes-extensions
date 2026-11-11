"""Native fenced phase execution. Durable attempt always precedes a write."""
import json
import threading
import time
import uuid
from .contracts import ContractError, TemporaryFailure, PermanentFailure, digest, canonical, PROFILES

class PhaseEngine:
    def __init__(self, store, adapter_factory, *, lease_seconds=30):
        self.store, self.factory, self.lease_seconds = store,adapter_factory,lease_seconds
    def recover(self): self.store.lifecycle.detect_stale_jobs()
    def run_item(self, job_id, item_id, *, cancelled=lambda:False):
        self.store.plan_item(job_id,item_id)
        row = self.store.row(job_id)
        contract = json.loads(row['contract'])
        item = next(i for i in self.store.items(job_id) if i['item_id']==item_id)
        if item['state']=='verified': return
        self.store.set_item(job_id,item_id,'running')
        try:
            for phase in PROFILES[contract['profile']]:
                if cancelled() or self.store.row(job_id)['cancel_requested']: raise InterruptedError()
                reason = self.run_phase(job_id,item_id,phase,contract,cancelled)
                if reason:
                    self.store.set_item(job_id,item_id,'blocked',reason); return
            self.store.set_item(job_id,item_id,'verified')
        except InterruptedError:
            self.store.set_item(job_id,item_id,'pending')
        except Exception:
            # Do not copy private exception strings into metadata.
            self.store.set_item(job_id,item_id,'blocked','phase_unavailable')
    def run_phase(self, job_id, item_id, phase, contract, cancelled):
        native_id = self.store.phase_id(job_id,item_id,phase)
        identity = {'job_id':job_id,'item':next(v for v in contract['items'] if v['item_id']==item_id),
                    'scope_hash':digest(contract),'phase':phase}
        adapter = self.factory(identity,contract,cancelled)
        lifecycle = self.store.lifecycle
        while True:
            if cancelled() or self.store.row(job_id)['cancel_requested']: raise InterruptedError()
            job = lifecycle.get_job(native_id)
            if job['state']=='completed':
                # Prior proof cannot silently bless changed destination/source.
                proof = adapter.readback(identity)
                return None if self.checked(proof,identity) else 'completed_proof_changed'
            if job['state'] in {'running','checkpointed','verifying'}:
                lifecycle.detect_stale_jobs()
                job = lifecycle.get_job(native_id)
                if job['state']!='stalled': return 'waiting_native_lease'
            if job['state'] in {'failed','stalled'}:
                with lifecycle.connect() as c:
                    fence=c.execute('SELECT owner,lease_generation FROM lifecycle_jobs WHERE job_id=?',(native_id,)).fetchone()
                try: lifecycle.retry_job(native_id,owner=fence['owner'],lease_generation=fence['lease_generation'])
                except ValueError: return 'item_retry_budget_exhausted'
            owner = 'bg_'+uuid.uuid4().hex
            try: owned = lifecycle.start_job(native_id,owner=owner,lease_seconds=self.lease_seconds)
            except ValueError: return 'waiting_native_lease'
            generation = owned['lease_generation']
            stop = threading.Event(); lost = threading.Event()
            def heartbeat():
                while not stop.wait(self.lease_seconds/3):
                    try: lifecycle.heartbeat(native_id,owner=owner,lease_generation=generation,lease_seconds=self.lease_seconds)
                    except Exception: lost.set(); return
            thread = threading.Thread(target=heartbeat,daemon=True); thread.start()
            def active():
                if lost.is_set(): raise ContractError('native_lease_lost')
                if cancelled() or self.store.row(job_id)['cancel_requested']: raise InterruptedError()
                if self.store.budget_root(job_id) is not None:
                    from .slots import session_is_foreground
                    # Existing short foreground lease holds new phase/result commits while
                    # the conversation interprets a possible correction. No job is cancelled
                    # merely because a person asks an independent question.
                    slot_root=getattr(adapter,'policy',{}).get('slot_root')
                    while slot_root and session_is_foreground(slot_root,contract['owner'],contract['session_id']):
                        if cancelled() or self.store.row(job_id)['cancel_requested']:raise InterruptedError()
                        time.sleep(.05)
                # Heartbeat thread renews every lease/3. Step gates read the fence;
                # they don't fsync another heartbeat on every page/provider poll.
                with lifecycle.connect() as c:
                    fence=c.execute('SELECT owner,lease_generation,lease_until,state FROM lifecycle_jobs WHERE job_id=?',(native_id,)).fetchone()
                if fence['owner']!=owner or fence['lease_generation']!=generation or fence['lease_until']<=time.time() or fence['state'] not in {'running','checkpointed','verifying'}:
                    raise ContractError('native_lease_lost')
            retry = False
            try:
                proof = adapter.readback(identity)
                if not self.checked(proof,identity):
                    if proof.get('state') != 'absent': return 'readback_unverified'
                    from .adapters import LocalReadonly,FakeAdapter
                    repeat_readonly=(isinstance(adapter,LocalReadonly) and contract['profile']=='readonly-research' and phase in {'source','analysis','artifact'}) or (isinstance(adapter,FakeAdapter) and phase=='source')
                    if self.store.attempted(native_id) and not repeat_readonly: return 'uncertain_write_readback_only'
                    adapter.preflight(identity)
                    active()
                    self.store.reserve_phase(job_id,item_id,phase,native_id,generation)
                    lifecycle.checkpoint_job(native_id,owner=owner,lease_generation=generation,checkpoint='once_attempt')
                    if not self.store.mark_attempt(native_id,owner,generation) and not repeat_readonly: return 'uncertain_write_readback_only'
                    try: adapter.execute_once(identity,active)
                    except (InterruptedError,PermanentFailure): raise
                    except Exception: pass
                    active()
                    proof = adapter.readback(identity)
                    if not self.checked(proof,identity): return 'uncertain_write_readback_only'
                active()
                lifecycle.begin_verification(native_id,owner=owner,lease_generation=generation)
                with lifecycle.connect() as c:
                    c.execute('INSERT OR REPLACE INTO background_proofs VALUES(?,?)', (native_id,canonical(proof)))
                lifecycle.complete_job(native_id,owner=owner,lease_generation=generation,
                                       verification_evidence='readback_sha256:'+digest(proof))
                return None
            except TemporaryFailure:
                # Only a demonstrably pre-attempt transient failure is retryable.
                retry = not self.store.attempted(native_id)
                if not retry: return 'uncertain_write_readback_only'
            except PermanentFailure as exc:
                return str(exc) if str(exc) in {'source_changed','source_too_large','snapshot_corrupt','source_delivery_incomplete','iteration_budget_exhausted','item_retry_budget_exhausted','native_request_binding_required'} else 'permanent_item_failure'
            finally:
                stop.set(); thread.join(timeout=1)
                current = lifecycle.get_job(native_id)
                if current['state'] in {'running','checkpointed','verifying'}:
                    try: lifecycle.fail_job(native_id,owner=owner,lease_generation=generation,error_type='pre_attempt_transient' if retry else 'readback_pending')
                    except ValueError: pass
            if retry:
                # Separate per-item retry budget, bounded backoff, interruptible.
                for _ in range(min(2**(owned['attempts']-1),8)*10):
                    if cancelled() or self.store.row(job_id)['cancel_requested']: raise InterruptedError()
                    time.sleep(.01)
    @staticmethod
    def checked(proof, identity):
        if not isinstance(proof,dict) or proof.get('identity')!=identity: raise ContractError('readback_identity_mismatch')
        return proof.get('state')=='verified' and isinstance(proof.get('destination_id'),str) and bool(proof.get('proof_sha256'))
