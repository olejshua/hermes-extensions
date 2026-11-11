"""Real isolated Hermes/subscription subprocess; private content only via stdin/stdout."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from .contracts import ContractError,PermanentFailure,digest

def verified_native_result(result,identity):
    if result.get('identity')==identity and result.get('error')=='source_delivery_incomplete':raise PermanentFailure('source_delivery_incomplete')
    proof=result.get('source_delivery')
    if (result.get('identity')!=identity or result.get('complete_original_read') is not True or result.get('budget_exhausted') is True
        or not isinstance(proof,dict) or proof.get('delivery_complete') is not True or proof.get('scope_hash')!=digest(identity)
        or proof.get('source_hash')!=identity['item']['source_hash'] or proof.get('item_id')!=identity['item']['item_id']
        or proof.get('proof')!='successful_serialized_provider_request_and_native_response'
        or type(proof.get('total_characters')) is not int or proof['total_characters']<0
        or type(proof.get('confirmed_source_characters')) is not int or proof.get('confirmed_source_characters')!=proof['total_characters']):
        raise ContractError('native_result_unverified_or_incomplete')
    if not isinstance(result.get('analysis'),str) or not result['analysis'].strip():raise ContractError('native_analysis_missing')
    return result

def command(policy,entry,args=()):
    core=Path(policy.get('core','/home/hermes/.hermes/hermes-agent')).resolve()
    sys.path.insert(0,str(core))
    from hermes_cli._launchers import runtime_command
    return runtime_command(core,args=args,code=f"runpy.run_path({str(Path(entry).resolve())!r}, run_name='__main__')",
                           home=policy.get('hermes_home','/home/hermes/.hermes'))

def run_native(store,policy,identity,contract,cancelled,active):
    # Parent owns the native lifecycle fence; child has exactly one original-read
    # tool and no shell, SaaS, search assembly, ambient memory or fallback provider.
    packet={'state_root':str(store.root),'identity':identity,'goal':contract['goal'],'source_path':str(store.snapshot_path(identity['job_id'],identity['item']['item_id'])),
            'timeout_seconds':policy.get('per_item_timeout_seconds',1800),'iterations':contract['per_item_iterations'],'core':policy.get('core','/home/hermes/.hermes/hermes-agent'),
            'hermes_home':policy.get('hermes_home','/home/hermes/.hermes'),'brain_package':policy['brain_package'],
            'telemetry_path':policy.get('telemetry_path','/home/hermes/.hermes/plugins/ole-coding/telemetry.py'),
            'telemetry_spool':policy.get('telemetry_spool','/home/hermes/.hermes/ole-observability/external-spool'),'model':'gpt-6.1-sol','effort':'medium'}
    budget=store.budget_state(identity['job_id'],identity['item']['item_id'])
    if budget is not None:
        remaining=budget['iterations']-budget['used']
        if remaining<=0:raise PermanentFailure('iteration_budget_exhausted')
        native_id=store.phase_id(identity['job_id'],identity['item']['item_id'],'analysis')
        with store.lifecycle.connect() as c:
            fence=dict(c.execute('SELECT owner,lease_generation FROM lifecycle_jobs WHERE job_id=?',(native_id,)).fetchone())
        packet.update(iterations=remaining,budget_fence={'native_id':native_id,**fence},
                      budget_route={'slot_root':policy['slot_root'],'owner':contract['owner'],'session_id':contract['session_id']})
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
        return verified_native_result(result,identity)
    finally:
        if p.poll() is None:
            p.terminate()
            try:p.wait(timeout=3)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=3)
        p.stdout.close()
