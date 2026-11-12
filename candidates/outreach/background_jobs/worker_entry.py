"""Absolute entry usable under native -I re-exec or explicit private fixtures."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json,os,signal
def main():
    root,policy_path,job_id,item_id=sys.argv[1:5]
    fixture='--fixture' in sys.argv[5:]
    policy=json.loads(Path(policy_path).read_bytes())
    google_worker='--google-worker' in sys.argv[5:]
    if google_worker:
        package=Path(policy.get('outreach_package',''))
        if not package.is_absolute()or package.is_symlink()or not package.is_dir():
            raise RuntimeError('native_outreach_package_required')
        if str(package)not in sys.path:sys.path.insert(0,str(package))
        from outreach_night.host_runtime import registered_paths,background_paths
        release,_,_,_=registered_paths();database,python=background_paths(release)
        if Path(root)!=database.parent or sys.version_info[:2]!=(3,12):
            raise RuntimeError('native_google_worker_role_changed')
    elif not fixture:
        sys.path.insert(0,policy.get('core','/home/hermes/.hermes/hermes-agent'))
        import hermes_bootstrap
    sys.path.insert(0,policy.get('brain_package',str(Path(__file__).resolve().parents[1]/'second-brain/src')))
    from background_jobs.contracts import checked_policy,private_file,canonical,digest
    checked_policy(policy,fixture=fixture)
    if fixture and policy.get('private_provider_fixture'):
        # Private provider responses only. This cannot select a domain factory,
        # boundary/enforcer, native scope, approval, job store or host role.
        from background_jobs.contracts import approved_path,digest
        import importlib.util
        fixture_ref=policy['private_provider_fixture']
        fixture_path=approved_path(policy['private_fixture_root'],fixture_ref['ref'])
        if digest(fixture_path.read_bytes())!=fixture_ref['sha256']:
            raise RuntimeError('private_provider_fixture_changed')
        spec=importlib.util.spec_from_file_location('native_private_provider_responses',fixture_path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.install(policy)
    from background_jobs.store import Store
    from background_jobs.engine import PhaseEngine
    from background_jobs.adapters import factory
    # Exact worker is session/group leader. Parent-death must clean descendants too.
    def terminate(*_):
        if os.getpgid(0)==os.getpid():os.killpg(os.getpid(),signal.SIGKILL)
        raise SystemExit(77)
    signal.signal(signal.SIGTERM,terminate)
    store=Store(root)
    if google_worker:
        from outreach_night.host_runtime import bootstrap
        bootstrap().check_background_store(store,policy)
    slot_fd=int(next(v.split('=',1)[1] for v in sys.argv[5:] if v.startswith('--slot-fd=')))
    slot_index=int(next(v.split('=',1)[1] for v in sys.argv[5:] if v.startswith('--slot-index=')))
    owned=os.fstat(slot_fd);expected=(Path(policy['slot_root'])/('slot-'+str(slot_index)+'.lock')).stat()
    if (owned.st_dev,owned.st_ino)!=(expected.st_dev,expected.st_ino):raise RuntimeError('owned_shared_slot_fd_missing_after_bootstrap')
    proof={'isolated':sys.flags.isolated==1,'fd_inherited':True,'slot_index':slot_index,'native_bootstrap':'hermes_bootstrap' in sys.modules,'python_version':sys.version.split()[0],'google_worker':google_worker,'pid':os.getpid(),'parent_pid':os.getppid()}
    try:private_file(store.root/'bootstrap-proofs'/(digest([job_id,item_id])+'.json'),canonical(proof).encode())
    except FileExistsError:pass
    if store.row(job_id)['profile'] in {'outreach-acquire-staging-v1', 'outreach-drafts-staging-v1'}:
        from background_jobs.draft_runtime import DraftRuntime
        runtime=DraftRuntime(store,policy)
        engine=runtime.engine(job_id)
    else:
        engine=PhaseEngine(store,factory(store,policy),lease_seconds=policy.get('lease_seconds',30))
    if '--reconcile-only' in sys.argv[5:]:
        runtime.reconcile(job_id)
    elif '--recovery-only' in sys.argv[5:]:
        engine.recover_item(job_id,item_id)
    else:
        engine.run_item(job_id,item_id)
    if store.row(job_id)['profile']=='outreach-drafts-staging-v1' and '--reconcile-only' not in sys.argv[5:]:
        runtime.reconcile(job_id)
    elif (store.row(job_id)['profile']=='outreach-acquire-staging-v1'
          and json.loads(store.row(job_id)['contract']).get('pool_continuation_ref')
          and '--reconcile-only' not in sys.argv[5:]
          and all(i['state']=='verified' for i in store.items(job_id))):
        runtime.reconcile(job_id)

if __name__=='__main__':main()
