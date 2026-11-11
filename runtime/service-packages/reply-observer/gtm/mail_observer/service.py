"""Disabled-by-default independent observer; no extra semantic model invocation.

The service runs under the operator's existing account/runtime. All executable
sources and imported active scripts are SHA-bound to an explicitly approved
review manifest. Drift keeps status readable but stops further polling/writes.
"""
from __future__ import annotations
import argparse,configparser,fcntl,hashlib,importlib,json,os,stat,sys,time
from datetime import datetime
from pathlib import Path
from gtm.coordinator import Coordinator
from gtm.reply import ExistingReplyReadClient,ReplyEnvelopeHook
from gtm.mail_observer.consumer import ReplyCaseConsumer
from gtm.mail_observer.intake import MailObserver
from gtm.mail_observer.sources import FullOriginals,SourceBlocked


def private_file(path):
    p=Path(path)
    if not p.is_absolute() or p.is_symlink():raise SourceBlocked('private_config_path_review')
    info=p.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
        raise SourceBlocked('private_config_permissions_review')
    return p


def read_policy(path):
    parser=configparser.ConfigParser(interpolation=None);parser.read_string(private_file(path).read_text())
    cfg=parser['reply_observer']
    cutoff=datetime.fromisoformat(cfg['activation_cutoff'].replace('Z','+00:00'))
    if cutoff.tzinfo is None or cfg.get('account')!='operator@primary.example.invalid' or cfg.get('category')!='acribic':
        raise SourceBlocked('activation_binding_review')
    db=Path(cfg['database']);info=db.parent.stat()
    if not db.is_absolute() or db.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise SourceBlocked('private_state_path_review')
    if db.exists() and (db.stat().st_uid!=os.getuid() or stat.S_IMODE(db.stat().st_mode)!=0o600):
        raise SourceBlocked('private_state_permissions_review')
    return cfg,cutoff,db


def reviewed(cfg):
    if not Path(cfg['review_manifest']).is_file():raise SourceBlocked('review_manifest_missing')
    manifest=private_file(cfg['review_manifest']);raw=manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=cfg['review_manifest_sha256']:raise SourceBlocked('review_manifest_changed')
    review=json.loads(raw)
    if review.get('contract')!='ole-acribic-reply-observer-review-v1' or review.get('revision')!='R2':
        raise SourceBlocked('review_contract_changed')
    root=Path(cfg['package_root']).resolve()
    for row in review['runtime_files']:
        relative=Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts:raise SourceBlocked('review_manifest_path')
        path=root/relative
        if not path.is_file() or path.is_symlink() or path.resolve().parent!=root.joinpath(relative.parent).resolve():
            raise SourceBlocked('runtime_source_changed')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:raise SourceBlocked('runtime_source_changed')
    dependencies=json.loads(cfg['active_dependencies'])
    variant='model_reuse' if cfg.getboolean('model_reuse_enabled',fallback=False) else 'baseline'
    if dependencies!=review['active_dependency_variants'][variant]:raise SourceBlocked('active_dependency_binding_changed')
    for row in dependencies:
        path=Path(row['path'])
        if not path.is_absolute() or not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            raise SourceBlocked('active_dependency_changed')
    return review


def _load_existing(cfg,name):
    # Entire import directory is only the existing protected script directory;
    # all Python files reachable there are in active_dependencies, not a new
    # credential/profile store. Reviewed checks run before each service cycle.
    directory=Path(cfg['existing_scripts_dir']).resolve()
    allowed={Path(r['path']).resolve() for r in json.loads(cfg['active_dependencies'])}
    if name=='cleanup_observer':
        path=Path(cfg['existing_cleanup_source']).resolve()
        if path not in allowed:raise SourceBlocked('active_module_not_reviewed')
        from importlib import util
        spec=util.spec_from_file_location('ole_reply_reviewed_cleanup',path)
        module=util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        return module
    if directory/(name+'.py') not in allowed:raise SourceBlocked('active_module_not_reviewed')
    sys.path.insert(0,str(directory))
    try:
        module=importlib.import_module(name)
        if Path(module.__file__).resolve()!=directory/(name+'.py'):raise SourceBlocked('active_module_import_mismatch')
        return module
    finally:sys.path.pop(0)


def bound_twenty(cfg):
    directory=Path(cfg['existing_helpers_dir']).resolve();source=directory/'notion_twenty_migration.py'
    allowed={Path(r['path']).resolve():r['sha256'] for r in json.loads(cfg['active_dependencies'])}
    if source not in allowed or hashlib.sha256(source.read_bytes()).hexdigest()!=allowed[source]:
        raise SourceBlocked('twenty_helper_not_reviewed')
    client=ExistingReplyReadClient(directory,private_file(cfg['existing_twenty_key']))
    if Path(client.helper.__file__).resolve()!=source or hashlib.sha256(Path(client.helper.__file__).read_bytes()).hexdigest()!=allowed[source]:
        raise SourceBlocked('twenty_helper_import_mismatch')
    if client.helper.TWENTY_URL.rstrip('/')!='https://crm.example.invalid':raise SourceBlocked('twenty_destination_changed')
    return client


def checked_server_version():
    from urllib.request import urlopen
    with urlopen('https://crm.example.invalid/client-config',timeout=20) as response:version=json.load(response)['appVersion']
    if version!='v2.42.6':raise SourceBlocked('twenty_version_changed')
    return version


def build(path):
    cfg,cutoff,db=read_policy(path)
    co=Coordinator(db);os.chmod(db,0o600);envelopes=ReplyEnvelopeHook(co,cutoff=cutoff)
    if not cfg.getboolean('observer_enabled',fallback=False):
        return cfg,None,ReplyCaseConsumer(envelopes,FullOriginals(),None,None)
    if cfg.get('parent_review')!='approved':raise SourceBlocked('parent_review_required')
    reviewed(cfg)
    watcher=_load_existing(cfg,'email_watcher');gmail=watcher.service('code');FullOriginals.profile(gmail)
    observer=MailObserver(envelopes,gmail)
    crm=cfg.getboolean('crm_enabled',fallback=False);invite=cfg.getboolean('invitation_enabled',fallback=False)
    preview=cfg.getboolean('preview_enabled',fallback=False)
    confirmed_cleanup=cfg.getboolean('confirmed_cleanup_enabled',fallback=False)
    observer.calendar_replies_enabled=confirmed_cleanup
    if not crm and not preview and not invite and not confirmed_cleanup:return cfg,observer,ReplyCaseConsumer(envelopes,FullOriginals(),gmail,None)
    frontier=None
    if invite and not crm:
        if not cfg.getboolean('model_reuse_enabled',fallback=False):raise SourceBlocked('invitation_semantics_not_bound')
        try:frontier=datetime.fromisoformat(cfg.get('invitation_activation_cutoff','').replace('Z','+00:00'))
        except ValueError:raise SourceBlocked('invitation_activation_scope_review') from None
        if frontier.tzinfo is None or frontier<cutoff:raise SourceBlocked('invitation_activation_scope_review')
    client=bound_twenty(cfg)if crm or preview or invite else None
    if client is not None:checked_server_version()
    calendar=policy=None
    if invite or confirmed_cleanup:
        from googleapiclient.discovery import build as google_build
        calendar=google_build('calendar','v3',credentials=watcher.credentials_for_account('code'),cache_discovery=False)
        policy=_load_existing(cfg,'mail_policy')
    consumer=ReplyCaseConsumer(envelopes,FullOriginals(),gmail,client,crm_enabled=crm,invitation_enabled=invite,
        calendar=calendar,hold_policy=policy,semantic_wait_seconds=cfg.getint('semantic_wait_seconds',fallback=300),preview_enabled=preview,version_reader=checked_server_version)
    if invite and not crm:consumer.invitation_cutoff=frontier
    if invite:
        from datetime import timedelta,timezone
        from gtm.mail_observer.cleanup import ExactInvitationCleanup
        module=_load_existing(cfg,'cleanup_observer')
        consumer.cleanup=ExactInvitationCleanup(module,co,calendar,policy,consumer.originals,gmail,
            window_start=cutoff-timedelta(days=30),window_end=cutoff+timedelta(days=90))
    if confirmed_cleanup:
        from gtm.mail_observer.cleanup import ExternalInvitationCleanup
        module=_load_existing(cfg,'cleanup_observer')
        consumer.confirmed_cleanup=ExternalInvitationCleanup(module,co,calendar,policy,consumer.originals,gmail,
            writes_authorized=True)
    return cfg,observer,consumer


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--once',action='store_true')
    parser.add_argument('--status');parser.add_argument('--resume');args=parser.parse_args()
    cfg,cutoff,db=read_policy(args.config)
    # Controls read stored state without importing auth/helpers or requiring an
    # unchanged executable review. Resume always needs fresh strict validation.
    if args.status:
        co=Coordinator(db);env=ReplyEnvelopeHook(co,cutoff=cutoff);worker=ReplyCaseConsumer(env,FullOriginals(),None,None)
        print(json.dumps(worker.status(args.status)));return
    if args.resume:
        # Control only: fresh source-bound CAS changes one review case. The
        # resident daemon remains the sole physical phase executor.
        cfg,observer,worker=build(args.config)
        reviewed(cfg)
        if not worker.crm_enabled and not worker.invitation_enabled:raise SourceBlocked('writes_disabled')
        print(json.dumps(worker.resume(args.resume)));return
    with (db.parent/'reply-observer.lock').open('a+') as lock:
        os.chmod(lock.name,0o600)
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'state':'already_running'}));return
        cfg=observer=worker=None
        while True:
            try:
                if cfg is None:cfg,observer,worker=build(args.config)
                if observer is None:result={'state':'disabled'}
                else:
                    current,_,_=read_policy(args.config)
                    if dict(current)!=dict(cfg):raise SourceBlocked('activation_policy_changed')
                    reviewed(cfg)
                    if worker.crm_enabled:checked_server_version()
                    observer.poll();result=worker.run_one() # one native case; no batch model
            except Exception as error:
                reason=error.code if isinstance(error,SourceBlocked) else 'observer_read_unavailable'
                auth=sys.modules.get('google_auth_store')
                if not isinstance(error,SourceBlocked) and auth is not None:
                    code=auth.safe_error(error).code
                    if code in {'invalid_grant','invalid_client','unauthorized_client','invalid_scope','access_denied','invalid_request','temporarily_unavailable','server_error','admin_policy_enforced','provider_unavailable','access_token_rejected','profile_access_unavailable','transport_unavailable','auth_operation_failed'}:
                        reason='auth_'+code
                result={'state':'paused','reason':reason}
            print(json.dumps({k:result[k] for k in ('state','reason') if k in result}),flush=True)
            if args.once or result['state']=='disabled':return
            time.sleep(60) # stable finite loop; systemd restarts only actual crashes


if __name__=='__main__':main()
