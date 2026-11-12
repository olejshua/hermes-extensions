"""Explicit staging quality child; same launcher/subscription, no new router call."""
import json,os,subprocess,time
from pathlib import Path
from background_jobs.contracts import ContractError,PermanentFailure,digest
from background_jobs.native import command,verified_native_result
from .wire import checked_quality_receipts
from .release import BACKGROUND_INIT_SHA,require_background_init


def quality_budget_packet(store,policy,identity,contract):
    """Same durable root/reservation/fence protocol as run_native."""
    budget=store.budget_state(identity['job_id'],identity['item']['item_id'])
    if budget is None:raise PermanentFailure('native_durable_quality_budget_required')
    remaining=budget['iterations']-budget['used']
    if remaining<=0:raise PermanentFailure('iteration_budget_exhausted')
    native=store.phase_id(identity['job_id'],identity['item']['item_id'],'analysis')
    with store.lifecycle.connect()as c:
        row=c.execute('SELECT owner,lease_generation FROM lifecycle_jobs WHERE job_id=?',(native,)).fetchone()
    if not row or not row['owner']:raise ContractError('native_quality_budget_lease_required')
    return {'iterations':remaining,'brain_package':policy['brain_package'],
            'budget_fence':{'native_id':native,'owner':row['owner'],'lease_generation':row['lease_generation']},
            'budget_route':{'slot_root':policy['slot_root'],'owner':contract['owner'],'session_id':contract['session_id']}}


def run_quality(store,policy,identity,contract,cancelled,active):
    if policy.get('outreach_profile_enabled') is not True:
        raise ContractError('outreach_staging_profile_not_activated')
    if identity.get('phase')!='analysis':raise ContractError('native_quality_analysis_phase_required')
    require_background_init()
    # Parent owns the native lifecycle fence; child has the original reader and
    # private quality commit only, no SaaS/shell/memory/fallback provider.
    packet={'state_root':str(store.root),'identity':identity,'goal':contract['goal'],'source_path':str(store.snapshot_path(identity['job_id'],identity['item']['item_id'])),
            'timeout_seconds':policy.get('per_item_timeout_seconds',1800),'iterations':contract['per_item_iterations'],'core':policy.get('core','/home/hermes/.hermes/hermes-agent'),
            'hermes_home':policy.get('hermes_home','/home/hermes/.hermes'),
            'telemetry_path':policy.get('telemetry_path','/home/hermes/.hermes/plugins/ole-coding/telemetry.py'),
            'telemetry_spool':policy.get('telemetry_spool','/home/hermes/.hermes/ole-observability/external-spool'),'model':'gpt-6.1-sol','effort':'medium','profile':'outreach-drafts-staging-v1','enabled':True,
            'required_background_init_sha256':BACKGROUND_INIT_SHA}
    active();packet.update(quality_budget_packet(store,policy,identity,contract));active()
    env={k:os.environ[k] for k in ('PATH','LANG','LC_ALL','TZ','HOME') if k in os.environ}
    env['HERMES_HOME']=packet['hermes_home'];env['HERMES_DISABLE_LAZY_INSTALLS']='1'
    # Remains in its worker process group: exact group cancellation includes all descendants.
    p=subprocess.Popen(command(policy,Path(__file__).with_name('native_entry.py')),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,text=True,close_fds=True)
    p.stdin.write(json.dumps(packet));p.stdin.close()
    deadline=time.monotonic()+policy.get('per_item_timeout_seconds',1800)
    # Drain output concurrently to avoid pipe deadlock, bounded content is a private artifact.
    import threading
    output=[];too_large=threading.Event()
    def drain():
        size=0
        for chunk in iter(lambda:p.stdout.read(4096),''):
            size+=len(chunk.encode())
            if size>2_000_000:too_large.set();break
            output.append(chunk)
    thread=threading.Thread(target=drain,daemon=True);thread.start()
    try:
        while p.poll() is None:
            active()
            if cancelled():raise InterruptedError()
            if time.monotonic()>deadline or too_large.is_set():raise TimeoutError()
            time.sleep(.1)
        thread.join(timeout=3)
        if p.returncode==78:raise PermanentFailure('iteration_budget_exhausted')
        if p.returncode==79:raise InterruptedError()
        if p.returncode==80:raise PermanentFailure('native_request_binding_required')
        if p.returncode or too_large.is_set():raise ContractError('native_worker_failed')
        result=json.loads(''.join(output))
        verified_native_result(result,identity)
        checked_quality_receipts(result,identity,store.snapshot_path(identity['job_id'],identity['item']['item_id']).read_bytes())
        return result
    finally:
        if p.poll() is None:
            p.terminate()
            try:p.wait(timeout=3)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=3)
        p.stdout.close()
