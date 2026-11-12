"""Native mail process wiring over the existing GTM lifecycle database.

The fixed, host-owned release file pins code and paths, never a write grant.
Every mutation still needs the real Coordinator phase/attempt/owner fence.
Google imports are lazy: the 3.14 call parent only binds saved originals; the
existing 3.12 watcher/helper constructs the SDK and the HostRuntime itself.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import json
import os
import stat
import sys
import threading

# The standalone Google process does not inherit the Core's sys.path either.
# Load only host-owned package locations before background_jobs' native imports.
RELEASE=Path(__file__).with_name('native-mail-release-v3.json')
if RELEASE.exists():
    info=RELEASE.stat()
    if RELEASE.is_symlink()or info.st_uid!=os.getuid()or not stat.S_ISREG(info.st_mode)or info.st_mode & 0o077:
        raise ValueError('native_mail_private_release_required')
    _initial=json.loads(RELEASE.read_bytes())
    if _initial.get('contract')!='reviewed-native-mail-process-release-v3':
        raise ValueError('native_mail_release_contract_changed')
    _paths=_initial.get('import_paths',[])
else:
    _paths=[os.environ.get('OLE_GTM_BRAIN_PACKAGE','/home/hermes/hermes-extensions-staging/brain/package/src')]
for _path in reversed(_paths):
    if not isinstance(_path,str)or not Path(_path).is_absolute()or Path(_path).is_symlink():
        raise ValueError('native_mail_import_paths_changed')
    if _path not in sys.path:sys.path.insert(0,_path)

from background_jobs.contracts import ContractError
from .guarded_gmail import GuardedGmail
from .write_scope import NativeWriteScope

CURRENT=ContextVar('outreach_registered_existing_host_runtime_v2',default=None)
ACCOUNTS={'code':'operator@primary.example.invalid','stealth':'operator@secondary.example.invalid'}
_RUNTIMES={}
_BOOT_LOCK=threading.RLock()


def registered_paths():
    """Deployment selection is fixed beside this code, not in a tool payload."""
    from background_jobs.contracts import canonical,digest
    if RELEASE.is_symlink():raise ContractError('native_mail_release_symlink')
    info=RELEASE.stat()
    if info.st_uid!=os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ContractError('native_mail_private_release_required')
    raw=RELEASE.read_bytes();release=json.loads(raw)
    if release.get('contract')!='reviewed-native-mail-process-release-v3':
        raise ContractError('native_mail_release_contract_changed')
    home=Path(os.environ.get('HERMES_HOME',str(Path.home()/'.hermes'))).absolute()
    if str(home.resolve())!=release.get('home') or home.is_symlink():
        raise ContractError('native_mail_profile_changed')
    database=home/'gtm/calls/lifecycle.sqlite3';root=home/'gtm/calls/mail-writes'
    if str(database)!=release.get('database') or str(root)!=release.get('state_root'):
        raise ContractError('native_mail_registered_paths_changed')
    if database.is_symlink() or not database.is_file():
        raise ContractError('existing_registered_native_database_required')
    if digest(release['writers'])!=release.get('writers_sha256'):
        raise ContractError('native_mail_release_manifest_changed')
    paths=release.get('import_paths')
    if not isinstance(paths,list)or not paths or any(not isinstance(p,str)or not Path(p).is_absolute()or
                                                  Path(p).is_symlink()or not Path(p).is_dir()for p in paths):
        raise ContractError('native_mail_import_paths_changed')
    return release,database,root,digest(raw)


def native_bindings(watcher=None):
    """Actual loaded callable bindings used by the fixed release verifier."""
    import email_watcher
    import email_realtime
    from gtm.coordinator import Coordinator
    from gtm.call_flow import followup
    from . import reply_boundary
    watcher=watcher or email_watcher
    return {'watcher.service':lambda:watcher.service,'watcher._make_draft':lambda:watcher._make_draft,
            'runtime.bootstrap':lambda:bootstrap,
            'reply.run':lambda:reply_boundary.run,
            'realtime._draft_one':lambda:email_realtime._draft_one,
            'coordinator._execute_owned':lambda:Coordinator._execute_owned,
            'followup.execute_once':lambda:followup.DraftAdapter.execute_once,'followup.write':lambda:followup.write}


def bootstrap(watcher=None):
    """Called in the executing 3.12 thread/process, including ThreadPool tasks."""
    release,database,root,sha=registered_paths()
    for path in reversed(release['import_paths']):
        if path not in sys.path:sys.path.insert(0,path)
    runtime=CURRENT.get()
    if runtime is not None:
        if getattr(runtime,'release_sha256',None)!=sha or Path(runtime.coordinator.store.db_path)!=database:
            raise ContractError('native_mail_registered_runtime_changed')
        runtime.boundary.check_enforcement()
        return runtime
    with _BOOT_LOCK:
        key=(os.getpid(),sha)
        runtime=_RUNTIMES.get(key)
        if runtime is None:
            from gtm.coordinator import Coordinator
            from .enforcement import PinnedWriterEnforcer
            from .write_boundary import NativeWriteBoundary,save
            ref,manifest_sha=save(root,'reviewed-native-mail-writers-v3/'+release['writers_sha256']+'.json',release['writers'])
            enforcer=PinnedWriterEnforcer(root,manifest_ref=ref,manifest_sha256=manifest_sha,
                                         bindings=native_bindings(watcher))
            boundary=NativeWriteBoundary(root,host_id=release['writers']['host_id'],enforcer=enforcer)
            enforcer.bind_boundary(boundary)
            runtime=HostRuntime(boundary,Coordinator(database),allowed_native_databases=[database])
            runtime.release_sha256=sha
            boundary.check_enforcement()
            _RUNTIMES[key]=runtime
        CURRENT.set(runtime)
        return runtime


def followup_original(identity,value,directory,followup,coordinator):
    """Verify the originals already persisted/validated by native CallRuntime."""
    from background_jobs.contracts import approved_path,digest
    _,database,root,_=registered_paths()
    expected=database.parent/identity.key
    if Path(directory).absolute()!=expected or expected.is_symlink():
        raise ContractError('native_followup_original_directory_changed')
    originals={name:approved_path(expected,name).read_bytes()
               for name in ('binding.json','source.txt','analysis.json','analysis.sha256')}
    binding=json.loads(originals['binding.json']);analysis=json.loads(originals['analysis.json'])
    if (digest(originals['source.txt'])!=identity.source_hash or
        any(binding.get(k)!=identity.fields()[k] for k in ('category','account','source_id','version')) or
        originals['analysis.sha256'].decode()!=digest(originals['analysis.json'])):
        raise ContractError('native_followup_original_changed')
    status=coordinator.status(identity)
    if any(status['phases'].get(p)!='completed' for p in ('source','analysis')):
        raise ContractError('native_followup_original_phase_not_completed')
    # Evidence interpretation was performed by CallRuntime before its immutable
    # analysis phase completed. Here reuse its exact deterministic plan builder.
    planned=followup.plan(analysis,identity,binding,lambda item:None)
    if planned!=value:raise ContractError('native_followup_saved_analysis_plan_changed')
    return {'identity':identity.fields(),'plan':planned,
            'originals':{name:digest(raw) for name,raw in originals.items()}}


def followup_reference(identity,value,directory,followup):
    """Persist only an exact plan pointer. This never creates a write scope."""
    from gtm.coordinator import Coordinator
    from .write_boundary import save
    _,database,root,_=registered_paths();coordinator=Coordinator(database)
    original=followup_original(identity,value,directory,followup,coordinator)
    ref,sha=save(root,'native-followup-plans/'+identity.key+'.json',original)
    return dict(workflow_id=identity.key,native_id=coordinator._job_id(identity,'call_email_followup'),
                plan_ref=ref,plan_sha256=sha)


class ClosedBoundary:
    def execute(self,*args,**kwargs):raise ContractError('native_write_runtime_not_installed')


def guard_service(native,alias):
    if alias not in ACCOUNTS:raise ContractError('native_unknown_mail_account')
    runtime=CURRENT.get()
    return GuardedGmail(native,ACCOUNTS[alias],runtime.boundary if runtime else ClosedBoundary())


def current():
    runtime=CURRENT.get()
    if runtime is None:raise ContractError('native_write_runtime_not_installed')
    return runtime


@contextmanager
def installed(runtime):
    """Private/in-process host bootstrap seam. No remote command can invoke it."""
    if CURRENT.get()is not None:raise ContractError('native_host_runtime_already_registered')
    token=CURRENT.set(runtime)
    try:yield runtime
    finally:CURRENT.reset(token)


class HostRuntime:
    def __init__(self,boundary,coordinator,*,allowed_native_databases):
        self.boundary,self.coordinator=boundary,coordinator
        self.allowed=frozenset(str(Path(p).resolve())for p in allowed_native_databases)
        if str(Path(coordinator.store.db_path).resolve())not in self.allowed:
            raise ContractError('existing_registered_native_database_required')
    def check_scope(self,scope):
        if str(Path(scope.lifecycle.db_path).resolve())not in self.allowed:raise ContractError('unregistered_native_database')
        return scope.check()
    def recovery_scope(self,identity,phase):
        co=self.coordinator;co.status(identity);native=co._job_id(identity,phase)
        with co.store.connect()as c:row=c.execute('SELECT * FROM lifecycle_jobs WHERE job_id=?',(native,)).fetchone()
        return NativeWriteScope(co.store,'gtm',identity.fields(),native,row['owner'],row['lease_generation'],phase,
                                identity.account,co._phase_key(identity,phase),lambda:None)
    def reply(self,watcher,alias,mid,text,**kwargs):
        from .reply_boundary import run
        return run(self,watcher,alias,mid,text,**kwargs)

    def capture_sources(self,scope,plan):
        """Last write gate only. Ordinary replies do not use campaign history."""
        from .central_sources import CanonicalSources,project_evidence
        from .protections import ServerProtectionSource
        from .pool_sources import domain
        from .histories import native_source_history
        from .enforcement import point
        from background_jobs.contracts import approved_path,digest
        from .write_boundary import SESSION
        record=SESSION.get()
        if record is None or record['boundary']is not self.boundary or record['scope'].binding!=scope.binding:
            raise ContractError('central_actual_native_write_session_required')
        self.boundary.check_session(record)
        release,database,_,_=registered_paths()
        source_bindings={'snapshot':point(CanonicalSources.snapshot),'hold':point(CanonicalSources.hold),
                         'capture':point(ServerProtectionSource.capture),'history':point(native_source_history)}
        if release.get('source_bindings')!=source_bindings:
            raise ContractError('central_reviewed_source_helpers_changed')
        project=scope.identity['category']
        authority=CanonicalSources(self.coordinator.store,database.parent/'central-sources',
            host_id=release['source_host'],account_lock_root=self.boundary.locks,
            native_ledgers=[{'root':self.boundary.root,'db':database}])
        source=ServerProtectionSource(self.boundary.root,authority,project=project,account=scope.account)
        binding={'identity':scope.identity,'native_id':scope.native_id,'phase':scope.phase,'plan_sha256':digest(plan)}
        document,ref=source.capture(binding,scope.check)
        raw=approved_path(self.boundary.root,ref).read_bytes()
        if digest(raw)!=document.get('sha256'):raise ContractError('central_source_original_changed')
        packet=json.loads(raw)
        original=packet['original']
        if (packet.get('binding')!=binding or original.get('contract')!='outreach-central-sources-v1'or
            original.get('project')!=project or original.get('account')!=scope.account):
            raise ContractError('central_reply_source_binding_changed')
        # Explicit contact stops still apply. Sent/draft/attempt evidence and
        # incomplete first-contact legacy history are retained, not reply bans.
        stops=project_evidence([v for v in original['legacy_projection']+original['events']
                                if v['state']in {'suppress','release'}])
        recipients=plan.get('to',[])+plan.get('cc',[])
        if any(address.casefold()in stops['mailboxes']or any(
            domain(address)==blocked or domain(address).endswith('.'+blocked)for blocked in stops['domains'])for address in recipients):
            raise ContractError('central_reply_recipient_suppressed')
        self.boundary.check_session(record)
        return {'project':project,'account':scope.account,'revision':original['revision'],
                'entry_sha256':original['entry_sha256'],'source_original_ref':ref,'source_original_sha256':document['sha256']}

    def followup_plan(self,value,native_ref,followup,*,write=False):
        from gtm.coordinator import Identity
        from background_jobs.contracts import approved_path,canonical,digest
        fields={'workflow_id','native_id','plan_ref','plan_sha256'}
        if write:fields|={'owner','generation','caller_pid'}
        if not isinstance(native_ref,dict)or set(native_ref)!=fields:
            raise ContractError('actual_native_followup_reference_required')
        with self.coordinator.store.connect()as c:
            row=c.execute('SELECT identity FROM gtm_sources WHERE workflow_id=?',(native_ref['workflow_id'],)).fetchone()
        if not row:raise ContractError('native_followup_source_missing')
        identity=Identity(**__import__('json').loads(row[0])).checked()
        if (identity.kind!='call'or value.get('workflow_id')!=identity.key or value.get('source_hash')!=identity.source_hash or
            value.get('account')!=identity.account or value.get('category')!=identity.category):
            raise ContractError('native_followup_source_changed')
        expected='native-followup-plans/'+identity.key+'.json'
        if native_ref['plan_ref']!=expected:raise ContractError('native_followup_plan_reference_changed')
        raw=approved_path(self.boundary.root,expected).read_bytes()
        original=followup_original(identity,value,Path(self.coordinator.store.db_path).parent/identity.key,
                                   followup,self.coordinator)
        if digest(raw)!=native_ref['plan_sha256']or raw!=canonical(original).encode():
            raise ContractError('native_followup_plan_changed')
        if native_ref['native_id']!=self.coordinator._job_id(identity,'call_email_followup'):
            raise ContractError('native_followup_phase_changed')
        return identity,original['plan']

    def followup_readback(self,svc,value,native_ref,followup,known=None):
        identity,value=self.followup_plan(value,native_ref,followup)
        scope=self.recovery_scope(identity,'call_email_followup')
        if (self.boundary.root/self.boundary.intent_ref(scope)).exists():
            owned=self.boundary.readback_owned(scope,svc)
            if owned['state']!='verified':return dict(state=owned['state'])
            if known and known!=owned['draft_id']:return dict(state='conflict')
            result=followup.readback(svc,value,owned['draft_id'])
            return result
        return followup.readback(svc,value,known)

    @contextmanager
    def followup_session(self,svc,value,native_ref,followup):
        from email.message import EmailMessage
        from background_jobs.outreach_candidate import content
        identity,value=self.followup_plan(value,native_ref,followup,write=True)
        if type(native_ref['caller_pid'])is not int or native_ref['caller_pid']!=os.getppid():
            raise ContractError('native_followup_caller_changed')
        if type(native_ref['generation'])is not int or not isinstance(native_ref['owner'],str):
            raise ContractError('native_followup_owner_required')
        scope=NativeWriteScope.gtm(self.coordinator,identity,'call_email_followup',native_ref['native_id'],native_ref['owner'],native_ref['generation'])
        self.check_scope(scope)
        before=followup.preflight(svc,value)
        if not before.get('ready'):raise ContractError('native_followup_live_source_not_ready')
        thread=before['thread'];last=thread['messages'][-1]
        headers={h['name'].casefold():h['value']for h in last.get('payload',{}).get('headers',[])}
        parent=headers.get('message-id')
        if not parent:raise ContractError('native_followup_reply_parent_required')
        body=followup.formatted(value);message=EmailMessage();message.set_content(body)
        import mail_policy
        message.add_alternative(mail_policy.body_html(body),subtype='html')
        approved={'operation':'users.drafts.create','to':value['recipients'],'cc':[],'subject':value['subject'],
                  'thread_id':value['thread_id'],'parts':content(message.as_bytes())['parts'],
                  'required_headers':{'in-reply-to':parent,'references':parent,'x-hermes-call':identity.key,
                                      'message-id':'<hermes-call-'+identity.key+'@hermes.local>'}}
        def source_gate(scope,plan):
            current=followup.preflight(svc,value)
            if not current.get('ready')or current.get('thread')!=thread:raise ContractError('native_followup_live_source_changed')
            return self.capture_sources(scope,plan)
        with self.boundary.session(scope,approved,source_gate=source_gate):yield scope
