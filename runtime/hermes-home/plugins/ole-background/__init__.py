"""Native tools; rights come from private deployment policy and gateway ContextVars."""
import json
import os
from pathlib import Path
import sys

_SERVICE=None
_DESKTOP_BINDING=None

def _desktop_spec():
    from hermes_cli.plugins import _delivery_manager
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_state import _plugin_settings_entry
    manager=_delivery_manager();plugin=manager._plugins.get('ole-background')
    if plugin is None or not plugin.enabled or plugin.module is not sys.modules[__name__]:
        raise ValueError('native_private_session_required')
    current=((_plugin_settings_entry(load_config_readonly() or {},'ole-background') or {}).get('settings') or {}).get('desktop_root_binding')
    if current is None or current!=_DESKTOP_BINDING:
        raise ValueError('verified_desktop_binding_required')
    return current

def service():
    global _SERVICE
    if _SERVICE is not None:return _SERVICE
    policy_path=Path(os.environ.get('OLE_BACKGROUND_POLICY','/home/hermes/.hermes/ole-background/policy.json'))
    if not policy_path.is_absolute() or policy_path.is_symlink() or not policy_path.is_file():raise ValueError('reviewed_policy_required')
    if policy_path.stat().st_uid!=os.getuid() or policy_path.stat().st_mode&0o077:raise ValueError('private_policy_required')
    policy=json.loads(policy_path.read_bytes())
    if policy.get('policy_path')!=str(policy_path):raise ValueError('policy_path_mismatch')
    for key in ('payload_root','brain_package'):
        path=Path(policy[key])
        if not path.is_absolute() or not path.is_dir() or path.is_symlink():raise ValueError('trusted_absolute_package_required')
        sys.path.insert(0,str(path))
    from background_jobs.service import BackgroundService
    _SERVICE=BackgroundService(policy['state_root'],policy)
    return _SERVICE

def desktop_identity(actor, native_session_id):
    from hermes_constants import get_hermes_home
    from .desktop_binding import identity
    return identity(_desktop_spec(), service().policy, get_hermes_home(), actor, native_session_id)

def desktop_delivery_binding(descriptor):
    from hermes_constants import get_hermes_home
    from .desktop_binding import owner_binding,session_key
    s=service();verified=owner_binding(_desktop_spec(),s.policy,get_hermes_home())
    if (descriptor.get('kind')!='desktop' or descriptor.get('binding_sha256')!=verified.fingerprint
            or not descriptor.get('native_session_key') or not descriptor.get('native_session_id')):
        raise ValueError('native_private_session_required')
    return verified,s.policy['owner'],session_key(verified,descriptor['native_session_key'],descriptor['native_session_id'])

def desktop_transport_allowed(transport):
    from hermes_constants import get_hermes_home
    from .desktop_binding import owner_binding
    from tui_gateway.plugin_continuation import login_matches
    binding=owner_binding(_desktop_spec(),service().policy,get_hermes_home())
    return login_matches(getattr(transport,'_hermes_native_login',None),binding)

def binding(parent_agent=None, session_id=None):
    from gateway.session_context import get_session_env,session_context_engaged
    try:
        from tui_gateway.plugin_continuation import CURRENT
        native_frame=CURRENT.get()
    except ImportError:
        native_frame=None
    # A failed Desktop proof never borrows another platform's context. Telegram
    # keeps its native existing path without extra Desktop configuration reads.
    if native_frame is not None:
        desktop=desktop_identity(parent_agent,session_id)
        return service(),desktop.owner,desktop.session
    if not session_context_engaged():raise ValueError('native_private_session_required')
    platform=get_session_env('HERMES_SESSION_PLATFORM')
    kind=get_session_env('HERMES_SESSION_CHAT_TYPE')
    user=get_session_env('HERMES_SESSION_USER_ID')
    session=get_session_env('HERMES_SESSION_KEY')
    profile=get_session_env('HERMES_SESSION_PROFILE')
    s=service()
    expected_profile=s.policy.get('gateway_profile','')
    if expected_profile is None:expected_profile=''  # No named deployment profile.
    owner='telegram:'+str(user)
    if platform!='telegram' or kind not in {'dm','private'} or not session or owner!=s.policy['owner'] or profile!=expected_profile:
        raise ValueError('native_private_session_required')
    return s,owner,session

def register(ctx):
    global _DESKTOP_BINDING
    _DESKTOP_BINDING=ctx.get_config('desktop_root_binding',None)
    ctx.register_skill('background-jobs',Path(__file__).parent/'skills/background-jobs/SKILL.md',
                       'Prepare explicitly authorized originals and a private immutable source manifest, then submit a durable read-only background job.')
    identifier={'type':'string','maxLength':128}
    definitions=[
        ('background_job_submit','Accept an explicitly authorized scoped thorough background job and return its durable ID immediately. First read native skill ole-background:background-jobs to prepare the private manifest yourself from authorized existing originals. The manifest pins each original/version/hash/owner/account. Read-only research profile only; no outreach processing, drafts, scheduling or sending. The detached native subscription worker preserves per-item checks and existing two child slots. For an explicit correction to remaining work use background_job_revise; do not start an unrelated fresh job to reset progress or budgets.',
         {'request_id':identifier,'goal':{'type':'string','maxLength':80000},'manifest_ref':{'type':'string','maxLength':256},'profile':{'type':'string','enum':['readonly-research']},'supersedes':identifier},
         ['request_id','goal','manifest_ref','profile'],lambda s,a,o,k:s.submit(**a,owner=o,session_id=k)),
        ('background_job_status','Read durable owned job progress and verified receipt without a model/worker call. Omitting job_id lists recent jobs for this exact private owner/session.',
         {'job_id':identifier},[],lambda s,a,o,k:s.status(a['job_id'],o,k) if a.get('job_id') else {'jobs':s.list_owned(o,k)}),
        ('background_job_cancel','Request cancellation of this exact owned job. Returns current cancelling state immediately; verified item results remain available. In-flight uncertain writes are readback-only on resume.',
         {'job_id':identifier},['job_id'],lambda s,a,o,k:s.cancel(a['job_id'],o,k)),
        ('background_job_resume','Resume the same immutable owned scope after cancellation/restart, preserving completed item states and once-attempt readbacks. Does not authorize a changed source, write or recipient.',
         {'job_id':identifier},['job_id'],lambda s,a,o,k:s.resume(a['job_id'],o,k)),
        ('background_job_revise','Apply an explicit user correction to the remaining work of one exact owned readonly-research job. First read current status and pass its exact revision. Preserve the original task except the stated correction. A normal question, unrelated new topic, criticism of an old result, or explanation request alone is NOT a revision. Never infer permission to change existing mail/calendar/CRM data. If multiple jobs could be meant, resolve which one before this call. Sources, account, permissions and total item budgets cannot expand. Returns durable requested state first; do not claim worker applied until status proves replacement_job. Legacy jobs with unknown iteration accounting are unsupported. Native private current Telegram message binding is required.',
         {'job_id':identifier,'expected_revision':{'type':'integer','minimum':1},'correction':{'type':'string','minLength':1,'maxLength':20000}},
         ['job_id','expected_revision','correction'],None),
        ('background_job_items_read','Read a bounded metadata page of exact owned item IDs/status/blockers. Complete original content is a separate source read; control status stays small.',
         {'job_id':identifier,'offset':{'type':'integer','minimum':0},'length':{'type':'integer','minimum':1,'maximum':100}},['job_id'],
         lambda s,a,o,k:s.item_page(a['job_id'],o,k,offset=a.get('offset',0),length=a.get('length',100))),
        ('background_job_result_read','Read every bounded page of an exact owned completed item analysis, after checking both native completed phase proofs and current artifact hashes. Result remains for review; this does not approve content or sending. No arbitrary file path.',
         {'job_id':identifier,'item_id':identifier,'offset':{'type':'integer','minimum':0},'length':{'type':'integer','minimum':1,'maximum':40000}},['job_id','item_id'],
         lambda s,a,o,k:s.result_read(a['job_id'],a['item_id'],o,k,offset=a.get('offset',0),length=a.get('length',40000))),
        ('background_job_source_read','Read exact immutable original pages of an owned background item. All pages remain available; no silent truncation or ambient file access.',
         {'job_id':identifier,'item_id':identifier,'offset':{'type':'integer','minimum':0},'length':{'type':'integer','minimum':1,'maximum':40000}},['job_id','item_id'],
         lambda s,a,o,k:s.original(a['job_id'],a['item_id'],o,k,offset=a.get('offset',0),length=a.get('length',40000))),
    ]
    for name,description,props,required,handler in definitions:
        def wrap(args,_props=props,_required=required,_handler=handler,_name=name,**native):
            try:
                if not isinstance(args,dict) or set(args)-set(_props) or set(_required)-set(args):raise ValueError('invalid_arguments')
                s,owner,key=binding(native.get('parent_agent'),native.get('session_id'))
                result=None
                if _name=='background_job_revise':
                    if key.startswith('desktop:'):raise ValueError('revision_profile_unsupported')
                    from hermes_cli.plugins import _delivery_manager
                    dialogue=_delivery_manager()._ole_dialogue_instance
                    inbound=dialogue.steering.current_inbound(key,native.get('parent_agent'),native.get('session_id'))
                    result=s.revise(**args,owner=owner,session_id=key,inbound_id=inbound)
                if key.startswith('desktop:') and _name in ('background_job_cancel','background_job_resume'):
                    from tui_gateway.plugin_dialogue import controlled
                    result=controlled(desktop_identity(native.get('parent_agent'),native.get('session_id')),
                                      'cancel' if _name.endswith('cancel') else 'resume',args['job_id'])
                if result is None:result=_handler(s,args,owner,key)
                if _name=='background_job_submit' and result.get('accepted') is True and key.startswith('desktop:'):
                    # Canonical tool persistence happened first; the native registered
                    # dialog stores the exact original route even for a mixed tool batch.
                    try:
                        from tui_gateway.plugin_dialogue import submitted
                        result['native_dialogue_receipt_saved']=submitted(desktop_identity(native.get('parent_agent'),native.get('session_id')),result)
                    except Exception:
                        result['native_dialogue_receipt_saved']=False
                return json.dumps(result,ensure_ascii=False)
            except Exception as exc:
                codes={'review_result_pending','review_artifact_corrupt','review_profile_unavailable','unknown_owned_item','unknown_owned_job','source_acquisition_pending','native_private_session_required',
                       'legacy_iteration_budget_unknown','revision_changed','revision_inbound_conflict','revision_profile_unsupported','job_already_revised','revision_requires_live_job','revised_scope_cannot_resume'}
                code=str(exc) if isinstance(exc,ValueError) and str(exc) in codes else 'owned_reviewed_background_scope_required'
                return json.dumps({'state':'unavailable','error':code,'success_claimed':False})
        ctx.register_tool(name,'ole-background',{'name':name,'description':description,'parameters':{'type':'object','properties':props,'required':required,'additionalProperties':False}},wrap)

    # A reviewed enabled native policy restarts the existing durable queue at gateway startup.
    try:service().ensure_supervisor()
    except Exception:pass # Disabled/unreviewed policy is inert; every tool still fails closed.
