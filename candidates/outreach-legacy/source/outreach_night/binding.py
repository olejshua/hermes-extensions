"""Native source -> same-run quality -> independently reviewed R3 draft adapter.

No registration happens here. This factory is reachable only through a separate
explicitly reviewed profile overlay; current production BG remains readonly.
"""
import json
from background_jobs.adapters import LocalReadonly
from background_jobs.contracts import ContractError, canonical, digest, private_file, approved_path
from background_jobs.outreach_candidate import GmailDraftAdapter
from .native import run_quality
from .sources import GmailSources,TwentySources
from .readiness import AcribicReadiness
from .clients import existing_gmail,existing_owned_gmail,existing_twenty,existing_code_calendar,load_existing
from .holds import ManagedDraftAdapter
from .wire import checked_quality_receipts


class QualityPhase(LocalReadonly):
    def execute_once(self,identity,active):
        result=run_quality(self.store,self.policy,identity,self.contract,self.cancelled,active)
        active()
        private_file(self.target,canonical({'identity':identity,'result':result,'result_hash':digest(result)}).encode())


def _recorded_quality(store,identity):
    expected={**identity,'phase':'analysis'}
    ref='results/'+identity['job_id']+'/'+digest(identity['item']['item_id'])+'/analysis.json'
    packet=json.loads(approved_path(store.root,ref).read_bytes())
    if packet.get('identity')!=expected or packet.get('result_hash')!=digest(packet.get('result')):
        raise ContractError('native_quality_result_changed')
    result=packet['result']
    original=store.snapshot_path(identity['job_id'],identity['item']['item_id']).read_bytes()
    from .sourcegraph import checked_dependencies
    checked_dependencies(store.root,original)
    checked_quality_receipts(result,expected,original)
    return result,original,json.loads(original)['candidate']


def _current_admission(store,policy,identity,current_admission):
    if policy.get('enabled')is not True or policy.get('outreach_profile_enabled')is not True:
        raise ContractError('outreach_staging_profile_not_activated')
    if not callable(current_admission):raise ContractError('native_main_readiness_gate_missing')
    # A host-wired native gate checks CURRENT human source/Main identity before
    # returning this already captured immutable packet. A boolean is no grant.
    packet=current_admission(identity)
    if (not isinstance(packet,dict)or packet.get('contract')!='hermes-main-sales-handoff-v1'or
        packet.get('job_id')!=identity['job_id']or packet.get('scope_hash')!=identity['scope_hash']):
        raise ContractError('native_main_readiness_binding_required')
    from background_jobs.sales_admission import _bound,_checked
    request=packet.get('request',{})
    batch,pool=_bound(store,identity['job_id'],request.get('owner'),request.get('session_id'))
    _checked(request,batch,pool)
    if packet.get('pool_contract_sha256')!=digest(pool)or identity['item']not in batch['items']:
        raise ContractError('native_main_readiness_binding_required')
    raw=approved_path(store.root,'main-sales-handoffs/'+digest(packet)+'.json').read_bytes()
    if raw!=canonical(packet).encode():raise ContractError('main_sales_handoff_changed')
    return packet


class CurrentReadiness:
    """Construct fresh native readers only after the exact current Main gate."""
    def __init__(self,store,policy,current_admission,*,domain_factory=None,authority=None):
        self.store,self.policy,self.current_admission=store,policy,current_admission
        from .authority import WriteAuthority
        self.domain_factory=domain_factory
        self.authority=authority or WriteAuthority(store.root,policy)
    def __call__(self,identity,plan,*,existing_draft_id=None):
        _current_admission(self.store,self.policy,identity,self.current_admission)
        category=identity['item']['category']
        if category!='acribic':raise ContractError('stealth_native_full_source_binding_missing')
        def active():_current_admission(self.store,self.policy,identity,self.current_admission)
        with self.authority.hold(identity['item']['account'],active)as authority_check:
            from .histories import check_native_attempt_history
            history=check_native_attempt_history(self.store,identity,plan['candidate'])
            from .domain_factory import HostDomainFactory
            domain=self.domain_factory or HostDomainFactory.from_existing(self.store,self.policy)
            live=domain.current_readiness(self.store,self.policy,lambda:(active(),authority_check()))
            live.native_history=history
            result=live(identity,plan,existing_draft_id=existing_draft_id)
            active();authority_check()
            return result
    def pre_reservation(self,identity,plan):return self(identity,plan)


def _new_write_adapter(store,policy,identity,contract,cancelled,current_admission):
    _current_admission(store,policy,identity,current_admission)
    result,original,candidate=_recorded_quality(store,identity)
    category=identity['item']['category']
    # Native caller's current existing helper/auth routes, not a fixture callback.
    gmail=existing_gmail(policy,category)
    live=CurrentReadiness(store,policy,current_admission)
    if json.loads(result['analysis']).get('proposed_slots'):
        adapter=ManagedDraftAdapter(store,identity,result,candidate,original,gmail,live_check=live)
        managed=adapter.managed(load_existing(policy['existing_scripts_dir'],'mail_policy',policy['active_dependencies']),
                                existing_code_calendar(policy))
        return AuthorityDraft(managed,live)
    return AuthorityDraft(GmailDraftAdapter(store,identity,result,candidate,original,gmail,live_check=live),live)


class AuthorityDraft:
    """The actual create AND immediate exact readback share one live lock."""
    def __init__(self,adapter,live):self.adapter,self.live=adapter,live
    def readback(self,identity):return self.adapter.readback(identity)
    def preflight(self,identity):return self.adapter.preflight(identity)
    def execute_once(self,identity,active):
        with self.live.authority.hold(identity['item']['account'],active)as check:
            def current():active();check()
            self.adapter.execute_once(identity,current)
            current()
            # Unknown outcome stays an attempted write; engine recovery does
            # not create a replacement after an absent/failed immediate read.
            self.adapter.readback(identity)
            current()


class DeferredDraft:
    """No auth/source/LLM action at construction, including after revocation."""
    def __init__(self,store,policy,identity,contract,cancelled,current_admission):
        self.store,self.policy,self.identity,self.contract=store,policy,identity,contract
        self.cancelled,self.current_admission=cancelled,current_admission
    def _bound(self,identity):
        if identity!=self.identity or json.loads(self.store.row(identity['job_id'])['contract'])!=self.contract:
            raise ContractError('outreach_scope_changed')
    def readback(self,identity):
        self._bound(identity)
        from .recovery import phase,load_intent,OwnedDraftReadback
        native=phase(self.store,identity)
        attempted=bool(native and self.store.attempted(native))
        try:
            _current_admission(self.store,self.policy,identity,self.current_admission)
        except Exception:
            if not attempted:return {'state':'absent','identity':identity,'fully_ready':False}
            try:
                # Validate saved original before auth. No new quality run,
                # sources, CRM, SMTP, TED, Notion or calendar operation.
                load_intent(self.store,identity)
                gmail=existing_owned_gmail(self.policy,self.store,identity)
                return OwnedDraftReadback(self.store,identity,gmail).readback(identity)
            except FileNotFoundError:
                # A pre-intent legacy attempt may retain its exact native
                # quality/source proof. Reuse it statically, never regenerate.
                try:
                    result,original,candidate=_recorded_quality(self.store,identity)
                    gmail=existing_owned_gmail(self.policy,self.store,identity)
                    adapter=GmailDraftAdapter(self.store,identity,result,candidate,original,gmail)
                    return adapter.readback_owned(identity)
                except Exception:return {'state':'unknown','identity':identity,'fully_ready':False}
            except ContractError:return {'state':'conflict','identity':identity,'fully_ready':False}
            except Exception:return {'state':'unknown','identity':identity,'fully_ready':False}
        return _new_write_adapter(self.store,self.policy,identity,self.contract,self.cancelled,
                                  self.current_admission).readback(identity)
    def preflight(self,identity):
        self._bound(identity)
        return _new_write_adapter(self.store,self.policy,identity,self.contract,self.cancelled,
                                  self.current_admission).preflight(identity)
    def execute_once(self,identity,active):
        self._bound(identity)
        active()
        return _new_write_adapter(self.store,self.policy,identity,self.contract,self.cancelled,
                                  self.current_admission).execute_once(identity,active)


def draft_adapter(store,policy,identity,contract,cancelled,*,current_admission=None):
    return DeferredDraft(store,policy,identity,contract,cancelled,current_admission)


class FinalReadback:
    def __init__(self,store,policy,identity,contract,cancelled,current_admission=None):
        self.identity=identity
        self.draft=draft_adapter(store,policy,{**identity,'phase':'draft'},contract,cancelled,current_admission=current_admission)
    def readback(self,identity):
        if identity!=self.identity:raise ContractError('outreach_scope_changed')
        proof=self.draft.readback(self.draft.identity)
        if proof['state']!='verified':return {'state':proof['state'],'identity':identity}
        if not proof.get('fully_ready')or not proof.get('draft_receipt'):
            return {'state':'unknown','identity':identity,'owned_target_verified':True,
                    'owned_draft_readback':proof,'fully_ready':False}
        return {'state':'verified','identity':identity,'destination_id':proof['destination_id'],
                'proof_sha256':digest(proof),'exact_draft_readback':proof}
    def preflight(self,identity):raise ContractError('final_readback_never_writes')
    def execute_once(self,identity,active):raise ContractError('final_readback_never_writes')


def factory(store,policy,*,current_admission=None):
    def build(identity,contract,cancelled):
        if contract['profile']!='outreach-drafts-staging-v1'and not (
            contract['profile']=='fixture-outreach'and policy.get('backend')=='fake'and identity['phase']in {'draft','readback'}):
            raise ContractError('outreach_profile_scope_changed')
        if identity['phase']=='source':return LocalReadonly(store,policy,identity,contract,cancelled)
        if identity['phase']=='analysis':return QualityPhase(store,policy,identity,contract,cancelled)
        if identity['phase']=='draft':return draft_adapter(store,policy,identity,contract,cancelled,current_admission=current_admission)
        if identity['phase']=='readback':return FinalReadback(store,policy,identity,contract,cancelled,current_admission)
        raise ContractError('outreach_profile_phase_changed')
    return build
