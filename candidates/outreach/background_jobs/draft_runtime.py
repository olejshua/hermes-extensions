"""Host-only composition of the existing acquisition, worker and Sales phases.

No daemon, queue or model router. Production policy still rejects the staging
profiles; private process compositions replace SaaS/quality at one explicit seam.
"""
from functools import partial
import importlib.util
import json
from pathlib import Path

from .contracts import ContractError, PermanentFailure, approved_path, canonical, digest, private_file
from .draft_intake import ACQUIRE_PROFILE, DraftIntake, load, save
from .sales_admission import PROFILE


def domain_factory(store, policy):
    private = policy.get('private_domain_factory')
    if private is not None:
        if policy.get('backend') != 'fake' or policy.get('private_outreach_runtime') is not True:
            raise ContractError('private_domain_factory_not_permitted')
        if set(private) != {'path', 'sha256'}:
            raise ContractError('private_domain_factory_binding_required')
        path = Path(private['path'])
        root = Path(policy['private_fixture_root'])
        file = approved_path(root, str(path.relative_to(root)))
        if digest(file.read_bytes()) != private['sha256']:
            raise ContractError('private_domain_factory_changed')
        spec = importlib.util.spec_from_file_location('background_private_domain', file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.HostDomainFactory(policy)
    if policy.get('outreach_package'):
        import sys
        package=Path(policy['outreach_package'])
        if not package.is_absolute()or package.is_symlink()or not package.is_dir():
            raise ContractError('native_outreach_package_required')
        if str(package)not in sys.path:sys.path.insert(0,str(package))
    from outreach_night import domain_factory as host
    return host


def orders(store, policy, domain=None, *, start_background=None):
    from gtm.call_flow.main_binding import MainBinding
    from .main_draft_orders import MainDraftOrders
    host = domain or domain_factory(store, policy)
    return MainDraftOrders(store, policy, MainBinding(user_id=policy['owner'].removeprefix('telegram:')),
        describe_named_pool=host.describe_pool, start_background=start_background)


class AcquisitionAdapter:
    def __init__(self, store, policy, identity, contract, cancelled, host):
        self.store, self.policy, self.identity, self.contract = store, policy, identity, contract
        self.cancelled, self.host = cancelled, host
        self.intake = DraftIntake(store, policy)
        self.request_id = contract['intake_request_id']

    def readback(self, identity):
        if identity != self.identity:
            raise ContractError('draft_intake_identity_changed')
        _, link, _ = self.intake.original(self.request_id)
        if not link['child_job'] or not link['resolution_ref']:
            return dict(state='absent', identity=identity)
        child = self.store.row(link['child_job'])
        if child['state'] == 'accepting':
            return dict(state='absent', identity=identity)
        resolution = load(self.store.root, link['resolution_ref'])
        self.intake.resolved_order(self.request_id)
        return dict(state='verified', identity=identity, destination_id=link['child_job'],
            proof_sha256=digest(resolution), source_originals_verified=True, draft_order_completed=False)

    def preflight(self, identity):
        if identity != self.identity:
            raise ContractError('draft_intake_identity_changed')
        original = self.intake.current(self.request_id)
        if self.host.describe_pool(original['recipe']['name']) != original['recipe']:
            raise ContractError('draft_intake_recipe_changed')

    def execute_once(self, identity, active):
        self.preflight(identity)
        def checked_active():
            active()
            self.intake.current(self.request_id)
        checked_active()
        original = self.intake.current(self.request_id)
        ref = 'draft-intake-resolutions/' + self.request_id + '.json'
        if (self.store.root / ref).exists():
            self.intake.resume_publication(self.request_id, checked_active)
            return
        from outreach_night.write_scope import NativeWriteScope
        native_scope = NativeWriteScope.background(self.store, identity, checked_active)
        result = self.host.acquire_pool(original['recipe'], self.store, self.policy, checked_active,
                                        target=original['main_decision']['args']['target'],
                                        native_scope=native_scope)
        if not isinstance(result, dict) or set(result) != {'named_pool', 'source_manifest', 'resolution_ref', 'resolution_sha256'}:
            raise ContractError('draft_intake_host_sources_required')
        raw = approved_path(self.store.root, result['resolution_ref']).read_bytes()
        if digest(raw) != result['resolution_sha256']:
            raise ContractError('draft_intake_host_resolution_changed')
        checked_active()
        self.intake.publish(self.request_id, result['named_pool'], result['source_manifest'], checked_active,
            provenance=dict(ref=result['resolution_ref'], sha256=result['resolution_sha256']))


class ContinuationAcquisitionAdapter(AcquisitionAdapter):
    """Same native source lease/Attempt, with a retained full-pool original."""
    def continuation(self):
        from .pool_continuation import PoolContinuation
        helper = PoolContinuation(self.store, self.policy)
        intent = helper.read_intent(self.contract['pool_continuation_ref'])
        if self.contract != helper.source_contract(intent):
            raise ContractError('pool_continuation_source_scope_changed')
        return helper, intent

    def preflight(self, identity):
        if identity != self.identity:
            raise ContractError('draft_intake_identity_changed')
        helper, intent = self.continuation()
        helper._current(intent, source_job=identity['job_id'])

    def readback(self, identity):
        if identity != self.identity:
            raise ContractError('draft_intake_identity_changed')
        helper, intent = self.continuation()
        if not (self.store.root / helper.result_ref(intent)).exists():
            return dict(state='absent', identity=identity)
        value = helper._resolution(intent)
        return dict(state='verified', identity=identity, destination_id=identity['job_id'],
                    proof_sha256=digest(value), source_originals_verified=value['blocker'] is None, draft_order_completed=False)

    def execute_once(self, identity, active):
        self.preflight(identity)
        helper, intent = self.continuation()
        def checked():
            active()
            helper._current(intent, source_job=identity['job_id'])
        from outreach_night.pool_successor import acquire_successor
        from outreach_night.write_scope import NativeWriteScope
        scope = NativeWriteScope.background(self.store, identity, checked)
        from .pool_continuation import ContinuationSourceScope
        scope = ContinuationSourceScope.from_native(scope, intent)
        acquire_successor(self.host, self.store, self.policy, checked, intent=intent,
                          native_scope=scope, result_ref=helper.result_ref(intent))


class DraftRuntime:
    def __init__(self, store, policy):
        self.store, self.policy = store, policy
        self.host = domain_factory(store, policy)
        self.orders = orders(store, policy, self.host)
        from .outreach_batch_gate import DraftBatchGate
        from outreach_night.batch_ready import checked_ready_batch
        self.gate = DraftBatchGate(store, current_admission=self.orders.check_current,
                                  ready_batch=partial(checked_ready_batch, store))

    def factory(self, job_id):
        contract = json.loads(self.store.row(job_id)['contract'])
        if contract['profile'] == ACQUIRE_PROFILE:
            cls = ContinuationAcquisitionAdapter if contract.get('pool_continuation_ref') else AcquisitionAdapter
            return lambda identity, batch, cancelled: cls(
                self.store, self.policy, identity, batch, cancelled, self.host)
        base = self.orders.sales_factory(job_id, contract['request_id'], self.host.native_sales_factory)
        if not contract.get('pool_continuation_ref'):
            return base
        runtime = self
        def build(identity, batch, cancelled):
            adapter = base(identity, batch, cancelled)
            if identity['phase'] != 'draft':
                return adapter
            class Retained:
                policy = getattr(adapter, 'policy', {})
                def readback(self, actual):
                    return adapter.readback(actual)
                def preflight(self, actual):
                    from .pool_continuation import PoolContinuation
                    PoolContinuation(runtime.store, runtime.policy).verify_retained_live(runtime, batch)
                    return adapter.preflight(actual)
                def execute_once(self, actual, active):
                    # Preflight has freshly verified K; every actual write gate
                    # continues to check the original chain and current fence.
                    return adapter.execute_once(actual, active)
            return Retained()
        return build

    def engine(self, job_id):
        from .engine import PhaseEngine
        from .outreach_batch_gate import BatchPhaseEngine
        contract = json.loads(self.store.row(job_id)['contract'])
        cls = BatchPhaseEngine if contract['profile'] == PROFILE else PhaseEngine
        args = (self.store, self.factory(job_id))
        return cls(*args, *([self.gate] if cls is BatchPhaseEngine else []),
                   lease_seconds=self.policy.get('lease_seconds', 30))

    def live_readiness(self):
        if self.policy.get('backend') == 'fake' and self.policy.get('private_domain_factory'):
            return self.host.current_readiness(self.store, self.policy, self.orders.check_current)
        from outreach_night.binding import CurrentReadiness
        return CurrentReadiness(self.store, self.policy, self.orders.check_current)

    def _record_finished(self, row, items):
        """Advance the existing cursor only on actual completed native proofs."""
        from .outreach_pool import OutreachPool
        from .outreach_candidate import native_plan
        from outreach_night.binding import _recorded_quality
        pool = OutreachPool(self.store)
        contract = json.loads(row['contract'])
        states = {v['item_id']: v for v in items}
        while True:
            status = pool.status(row['job_id'], row['owner'], row['session_id'])
            if status['state'] not in {'pending', 'blocked_unknown_write'}:
                return status
            _, scope, cursor = pool._bound(row['job_id'], row['owner'], row['session_id'])
            if cursor == len(scope['candidates']):
                return status
            candidate = scope['candidates'][cursor]
            item = states[candidate['item_id']]
            identity = self.gate.identity(row['job_id'], candidate['item_id'])
            with self.store.lifecycle.connect() as c:
                attempted = pool._attempted(c, row['job_id'], candidate['item_id'])
            if item['state'] == 'verified':
                # Full final readback, including fresh Gmail/CRM factual gates.
                proof = self.factory(row['job_id'])({**identity, 'phase': 'readback'}, contract, lambda: False).readback({**identity, 'phase': 'readback'})
                if proof.get('state') != 'verified' or not proof.get('exact_draft_readback'):
                    self.store.set_item(row['job_id'], candidate['item_id'], 'blocked', 'final_live_readback_pending')
                    return status
                pool.record(row['job_id'], row['owner'], row['session_id'], candidate,
                            state='verified', proof=proof['exact_draft_readback'])
                continue
            if attempted:
                return status  # never reject or replace any uncertain write
            if item['state'] == 'blocked':
                if item['blocked_reason'] not in {'candidate_not_ready','duplicate_candidate_family',
                        'current_complete_crm_pool_blocks_candidate','current_authoritative_protections_block_candidate'}:
                    raise ContractError('outreach_preparation_access_or_scope_blocked')
                pool.record(row['job_id'], row['owner'], row['session_id'], candidate,
                            state='blocked', reason=item['blocked_reason'] or 'preparation_blocked')
                continue
            try:
                result, original, _ = _recorded_quality(self.store, identity)
                plan = native_plan(result, identity, candidate, original)
            except (FileNotFoundError, ContractError):
                return status
            if plan is not None:
                return status
            self.store.set_item(row['job_id'], candidate['item_id'], 'blocked', 'candidate_not_ready')
            pool.record(row['job_id'], row['owner'], row['session_id'], candidate,
                        state='rejected', reason='candidate_not_ready')

    def _seal_if_prepared(self, row, items):
        from .outreach_candidate import native_plan
        from .outreach_pool import OutreachPool
        from outreach_night.binding import _recorded_quality
        from outreach_night.batch_ready import seal_ready_batch, seal_ready_successor
        probe=next((v for v in items if v['state']!='blocked'),items[0])
        identity = self.gate.identity(row['job_id'], probe['item_id'])
        current=self.gate.current(identity, missing_ok=True)
        states = {v['item_id']: v for v in items}
        if current is not None and not any(states[v['item_id']]['state']=='blocked'for v in current['members']):
            return
        _, pool, _ = OutreachPool(self.store)._bound(row['job_id'], row['owner'], row['session_id'])
        prepared = self.gate.phase_proofs(row['job_id'], json.loads(row['contract']))
        states = {v['item_id']: v for v in items}
        chosen, families, mailboxes, domains = [], set(), set(), set()
        live = self.live_readiness()
        with self.store.lifecycle.connect() as c:
            completed = {r['item_id']:json.loads(r['receipt']) for r in c.execute(
                "SELECT item_id,receipt FROM outreach_pool_results WHERE job_id=? AND state='verified'", (row['job_id'],))}
        for candidate in pool['candidates']:
            key = candidate['item_id']
            if states[key]['state'] == 'blocked' or any((key, phase) not in prepared for phase in ('source', 'analysis')):
                continue
            member = self.gate.identity(row['job_id'], key)
            result, original, _ = _recorded_quality(self.store, member)
            plan = native_plan(result, member, candidate, original)
            if plan is None:
                self.store.set_item(row['job_id'], key, 'blocked', 'candidate_not_ready')
                continue
            try:
                live(member, plan, existing_draft_id=completed.get(key,{}).get('draft_id'))
            except PermanentFailure as exc:
                if key in completed or str(exc) not in {'current_complete_crm_pool_blocks_candidate',
                        'current_authoritative_protections_block_candidate','gmail_family_history_collision'}:
                    raise ContractError('outreach_preparation_access_or_scope_blocked') from exc
                self.store.set_item(row['job_id'], key, 'blocked',
                    'current_complete_crm_pool_blocks_candidate' if str(exc)=='gmail_family_history_collision' else str(exc))
                continue
            mailbox, domain = candidate['to'].casefold(), candidate['to'].split('@')[1].casefold()
            if candidate['family_id'] in families or mailbox in mailboxes or domain in domains:
                self.store.set_item(row['job_id'], key, 'blocked', 'duplicate_candidate_family')
                continue
            chosen.append(member);families.add(candidate['family_id']);mailboxes.add(mailbox);domains.add(domain)
            if len(chosen) == pool['target']:
                seal=seal_ready_successor if current is not None else seal_ready_batch
                seal(self.store, self.policy, row['job_id'], chosen,
                     self.live_readiness(), self.orders.check_current)
                return prepared
        return prepared

    def reconcile(self, job_id):
        """Runs inside an existing worker slot, never the supervisor event loop."""
        import fcntl
        import os
        path=self.store.root/('draft-reconcile-'+job_id+'.lock')
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            row=self.store.row(job_id)
            if not row['cancel_requested']:
                if row['profile'] == ACQUIRE_PROFILE:
                    from .pool_continuation import PoolContinuation
                    try:
                        PoolContinuation(self.store, self.policy).publish(job_id)
                    except ContractError as exc:
                        # A durable source may outlive its original grant. A
                        # rejected publication pauses once; the supervisor must
                        # not repeatedly respawn the same revoked publication.
                        with self.store.lifecycle.connect() as c:
                            c.execute('BEGIN IMMEDIATE')
                            current=c.execute('SELECT cancel_requested FROM background_jobs WHERE job_id=?',(job_id,)).fetchone()
                            if not current['cancel_requested']:
                                reason=('outreach_original_order_changed' if str(exc) in {
                                    'draft_intake_cancelled_or_changed','pool_continuation_cancelled_or_revised',
                                    'pool_continuation_source_cancelled_or_revised'} else 'outreach_source_continuation_review_required')
                                c.execute("UPDATE background_jobs SET state='paused',runtime_blocker=? WHERE job_id=?",(reason,job_id))
                else:
                    self._reconcile(row,self.store.items(job_id))
        finally:
            os.close(fd)

    def _reconcile(self, row, items):
        from .outreach_pool import OutreachPool
        if row['cancel_requested']:
            return self.gate.recovery_items(row, items)
        try:
            from .pool_continuation import PoolContinuation
            helper=PoolContinuation(self.store,self.policy)
            if (self.store.root/helper.intent_ref(row['job_id'])).exists():
                # An older finishing worker may still reconcile after the new
                # source job was admitted. Its raw subset has no new work.
                helper.start(row)
                self.store.update_job(row['job_id'],state='paused',runtime_blocker='permitted_pool_continuing')
                return []
            status = self._record_finished(row, items)
            if status['state'] == 'blocked_unknown_write':
                return self.gate.recovery_items(row, self.store.items(row['job_id']))
            if status['state'] in {'completed', 'pool_exhausted'}:
                contract = json.loads(row['contract'])
                if status['state']=='pool_exhausted':
                    # The execution subset is not the full authorized pool.
                    # A retained source/scope continuation is still required;
                    # never turn unacquired permitted members into exhaustion.
                    resolution=load(self.store.root,'draft-intake-resolutions/'+contract['request_id']+'.json')
                    provenance=resolution.get('provenance')
                    if provenance:
                        raw=approved_path(self.store.root,provenance['ref']).read_bytes()
                        if digest(raw)!=provenance['sha256']:raise ContractError('draft_intake_host_resolution_changed')
                        native=json.loads(raw);remaining=native.get('unacquired_item_ids')
                        if not isinstance(remaining,list):raise ContractError('draft_intake_full_pool_remainder_unknown')
                        if remaining:
                            from .pool_continuation import PoolContinuation
                            if PoolContinuation(self.store, self.policy).start(row):return []
                factory = self.factory(row['job_id'])
                if contract.get('pool_continuation_ref'):
                    from .pool_continuation import PoolContinuation
                    PoolContinuation(self.store,self.policy).verify_retained_live(self,contract)
                def readback(candidate):
                    identity = self.gate.identity(row['job_id'], candidate['item_id'])
                    proof = factory({**identity, 'phase': 'readback'}, contract, lambda: False).readback({**identity, 'phase': 'readback'})
                    return proof.get('exact_draft_readback', {})
                ref = OutreachPool(self.store).completion(row['job_id'], row['owner'], row['session_id'], readback=readback)
                receipt = load(self.store.root, ref)
                final = receipt['status']
                self.store.update_job(row['job_id'], receipt_ref=ref,
                    state='completed' if final['state'] == 'completed' else 'completed_with_blockers',
                    runtime_blocker=None if final['state'] == 'completed' else final['state'])
                return []
            preparation_snapshot=self._seal_if_prepared(row, self.store.items(row['job_id']))
            pending=self.gate.pending_items(row, self.store.items(row['job_id']))
            if not pending and not any(v['state']=='running' for v in self.store.items(row['job_id'])):
                # Another real native analysis may have completed between the
                # selection snapshot and pending selection. Reconcile its new
                # proof once on the normal next dispatch, without declaring a
                # source/pool blocker from the older preparation snapshot.
                latest_preparation=self.gate.phase_proofs(row['job_id'],json.loads(row['contract']))
                if preparation_snapshot is not None and latest_preparation!=preparation_snapshot:
                    return []
                # Live selection may just have rejected the last acquired
                # members. Advance their actual results before deciding that
                # more sources or a genuine K/N exhaustion receipt is needed.
                latest=self._record_finished(row,self.store.items(row['job_id']))
                if latest['state'] in {'completed','pool_exhausted'}:
                    return self._reconcile(row,self.store.items(row['job_id']))
                if latest['state']=='blocked_unknown_write':
                    return self.gate.recovery_items(row,self.store.items(row['job_id']))
                from .pool_continuation import PoolContinuation
                from outreach_night.binding import _recorded_quality
                from .outreach_candidate import native_plan
                _, scope, _ = OutreachPool(self.store)._bound(row['job_id'], row['owner'], row['session_id'])
                carried=[]
                for candidate in scope['candidates']:
                    item=next(v for v in self.store.items(row['job_id']) if v['item_id']==candidate['item_id'])
                    if item['state'] in {'blocked','verified'}:continue
                    identity=self.gate.identity(row['job_id'],candidate['item_id'])
                    result,original,_=_recorded_quality(self.store,identity)
                    if native_plan(result,identity,candidate,original) is not None:
                        carried.append(dict(candidate=candidate,item=identity['item']))
                if PoolContinuation(self.store,self.policy).start(row,carried=carried):return []
                raise ContractError('additional_permitted_sources_required')
            return pending
        except ContractError:
            self.store.update_job(row['job_id'], state='paused', runtime_blocker='outreach_batch_review_required')
            return []

    def pending_items(self,row,items):
        """Metadata-only native dispatch; all SaaS/readiness stays in workers."""
        from .outreach_pool import OutreachPool
        if row['cancel_requested']:
            return self.gate.recovery_items(row,items)
        if (self.store.root/('pool-continuations/'+row['job_id']+'.json')).exists():
            from .pool_continuation import PoolContinuation, job_id
            helper=PoolContinuation(self.store,self.policy)
            intent=helper.read_intent(helper.intent_ref(row['job_id']))
            source=job_id(helper.source_contract(intent))
            with self.store.lifecycle.connect()as c:
                admitted=c.execute('SELECT state FROM background_jobs WHERE job_id=?',(source,)).fetchone()
            if not admitted or admitted['state']=='accepting':
                return [{**items[0],'state':'pending','reconcile_only':True}]
            return []
        pool=OutreachPool(self.store)
        status=pool.status(row['job_id'],row['owner'],row['session_id'])
        if status['state']=='blocked_unknown_write':
            recovery=self.gate.recovery_items(row,items)
            if not recovery and not any(v['state']=='running' for v in items):
                if not self.gate.unresolved(row['job_id']):
                    # Native final proof may have committed between the status
                    # projection and recovery selection. Reconcile its K/N;
                    # a normal completion race must not pause the whole job.
                    return [{**items[0],'state':'pending','reconcile_only':True}]
                self.store.update_job(row['job_id'],state='paused',runtime_blocker='owned_readback_unresolved')
            return recovery
        if status['state'] in {'completed','pool_exhausted'}:
            return [{**items[0],'state':'pending','reconcile_only':True}]
        _,scope,cursor=pool._bound(row['job_id'],row['owner'],row['session_id'])
        current=next(v for v in items if v['item_id']==scope['candidates'][cursor]['item_id'])
        if current['state'] in {'verified','blocked'}:
            return [{**current,'state':'pending','reconcile_only':True}]
        try:
            pending=self.gate.pending_items(row,items)
        except ContractError:
            self.store.update_job(row['job_id'],state='paused',runtime_blocker='outreach_batch_review_required')
            return []
        if pending:return pending
        if any(v['state']=='running' for v in items):return []
        return [{**current,'state':'pending','reconcile_only':True}]
