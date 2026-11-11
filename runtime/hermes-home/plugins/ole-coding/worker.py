"""One-shot explicit Sol6.1medium worker. No automatic merge, commit, push or send."""
from __future__ import annotations
import hashlib, importlib.util, json, os, queue, signal, subprocess, threading, time, uuid, sys
from pathlib import Path
MODEL='gpt-6.1-sol'; EFFORT='medium'
JOBS={}; LOCK=threading.Lock()
def git(repo,*args):return subprocess.check_output(['git','-C',str(repo),*args],text=True,stderr=subprocess.DEVNULL).strip()
def home_for_spool():
    from hermes_constants import get_hermes_home
    return get_hermes_home()

def native_event(line):
    if len(line)>1_000_000:raise ValueError('native event exceeds limit')
    d=json.loads(line)
    if not isinstance(d,dict) or not isinstance(d.get('type'),str):raise ValueError('invalid native event')
    event=d['type'];safe={'type':event,'at':time.time()}
    if event=='turn.completed':
        u=d.get('usage');safe['usage']=u if isinstance(u,dict) else None
    if event in ('item.started','item.completed'):
        item=d.get('item') or {};safe['item_type']=item.get('type');safe['item_id']=hashlib.sha256(str(item.get('id','')).encode()).hexdigest()[:16]
    if event in ('error','turn.failed'):
        message=str(d.get('message') or d.get('error') or '').lower()
        safe['error']=next((label for pattern,label in [('not supported','unsupported_model'),('model','model_error'),('unauthorized','auth_error'),('sandbox','sandbox_error'),('rate limit','rate_limit'),('refresh','expired_auth'),('network','network_error')] if pattern in message),'native_worker_error')
    return safe

def launch(repo,brief,gate,home,timeout=600,read_only=False,progress=True):
    root=Path(repo).resolve();home=Path(home).resolve()
    if not root.is_dir() or not (root/'.git').exists():raise ValueError('existing repository required')
    if len(brief)>80000 or not brief.strip():raise ValueError('bounded nonempty brief required')
    if not isinstance(gate,list) or not gate or any(not isinstance(a,str) or len(a)>2048 for a in gate):raise ValueError('one concrete argv verification command required')
    executable=Path(gate[0])
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable,os.X_OK):raise ValueError('absolute trusted verification executable required')
    if executable.resolve().is_relative_to(root):raise ValueError('worker-editable verification executable forbidden; external runner can execute clearly labelled repository tests')
    if not 30<=timeout<=1800:raise ValueError('deadline30..1800 seconds required')
    if git(root,'status','--porcelain'):raise ValueError('source dirty; preserve user changes and use clean authorised source')
    if root.name=='Monolith':
        git(root,'fetch','origin')
        if not git(root,'branch','--show-current').startswith('codex/'):raise ValueError('Monolith requires current codex branch')
        try:
            upstream=git(root,'rev-parse','@{u}')
            if git(root,'rev-parse','HEAD')!=upstream:raise ValueError('Monolith source must match freshly fetched upstream')
        except subprocess.CalledProcessError:raise ValueError('Monolith upstream unavailable')
    if root.name=='Monolith' and any(word in brief.lower() for word in ('design','ui','ux','review')):
        raise ValueError('Monolith design jobs require actual apple-design SKILL.md read by parent and explicit skill artifact; VPS capability not ready')
    try:
        from tools.external_progress import capture
        progress_sink=capture() if progress else None
    except ImportError:progress_sink=None
    jobid=uuid.uuid4().hex;jobroot=home/'ole-coding'/jobid;jobroot.mkdir(parents=True,mode=0o700);os.chmod(jobroot,0o700)
    work=jobroot/'work';start=git(root,'rev-parse','HEAD');branch='codex/ole-job-'+jobid[:10]
    git(root,'worktree','add','-b',branch,str(work),start)
    job={'backend':'native-restricted','job_id':jobid,'status':'queued','model':MODEL,'effort':EFFORT,'start_sha':start,'branch':branch,'events':[],'root':jobroot,'work':work,'cancel':threading.Event(),'timeout':timeout,'read_only':read_only,'progress':progress,'gate':gate,'brief':brief}
    job['progress_sink']=progress_sink
    with LOCK:JOBS[jobid]=job
    threading.Thread(target=run,args=(job,),name='ole-coding-'+jobid[:8],daemon=True).start();return public(job)

def public(job):
    r={k:v for k,v in job.items() if k in ('backend','job_id','status','model','effort','start_sha','branch','events','events_dropped','receipt','error','wall_seconds','usage')}
    if r.get('status') in ('completed','cancelled','failed') and not job.get('_finalized'):r['status']='running'
    return r

def safe_emit(job,event):
    sink=job.get('progress_sink')
    if sink:
        try:sink(job['job_id'],event.get('type',''))
        except Exception:pass
    # Stream persisted metadata only. Content output remains private; summary final receipt bounded.
    if len(job['events'])<256:job['events'].append(event)
    else:job['events_dropped']=job.get('events_dropped',0)+1
    try:
        from hermes_cli.lifecycle import invoke_hook
        invoke_hook('ole_coding_progress',job_id=job['job_id'],**event)
    except Exception:pass

def run(job):
    started=time.monotonic();p=None;job['status']='running'
    try:
        # Explicit restricted native route; failed CLI auth is never retried via paid provider.
        core=os.environ.get('OLE_HERMES_CORE','/home/hermes/.hermes/hermes-agent')
        # Isolated source stage requires its own PM selection; production uses original live source.
        sourcehome=os.environ.get('OLE_HERMES_DEPENDENCY_HOME',str(Path.home()/'.hermes'))
        env={k:os.environ[k] for k in ('PATH','LANG','LC_ALL','TZ','TERM') if k in os.environ}
        env.update({'HOME':str(Path.home()),'HERMES_HOME':sourcehome,'HERMES_DISABLE_LAZY_INSTALLS':'1','PYTHONPATH':os.pathsep.join([core,*[p for p in sys.path if 'site-packages' in p]]),'HERMES_RUNTIME_DIR':'/home/hermes/.hermes/tools','TMPDIR':str(job['root'])})
        packet={k:job[k] for k in ('job_id','brief','timeout','read_only')};packet.update(work=str(job['work']),home=str(job['root']/'native-home'),spool=str(Path(home_for_spool())/'ole-observability/external-spool'))
        Path(packet['home']).mkdir(mode=0o700)
        from hermes_cli.auth_codex import resolve_codex_runtime_credentials
        packet['runtime']=resolve_codex_runtime_credentials(refresh_skew_seconds=job['timeout']+60)
        # Parent broker owns refresh; child receives access only and must finish before expiry.
        import base64
        token_payload=packet['runtime']['api_key'].split('.')[1]
        expiry=json.loads(base64.urlsafe_b64decode(token_payload+'='*(-len(token_payload)%4))).get('exp')
        if not isinstance(expiry,(int,float)) or expiry<time.time()+job['timeout']+30:
            raise RuntimeError('subscription access lifetime shorter than coding deadline')
        # Activate existing pinned dependency generation in private child home; no installation.
        import shutil
        key=hashlib.sha256(str(Path(core).resolve()).encode()).hexdigest()[:16]
        old=Path(sourcehome)/'installs'/key;new=Path(packet['home'])/'installs'/key;new.mkdir(parents=True,mode=0o700)
        if not (old/'facts.json').exists():raise RuntimeError('committed host dependency selection unavailable')
        (new/'environments').symlink_to((old/'environments').resolve(),target_is_directory=True)
        (new/'facts.json').write_bytes((old/'facts.json').read_bytes())
        env['HERMES_HOME']=packet['home']
        if os.environ.get('HERMES_CHILD_SLOT_ROOT'):env['HERMES_CHILD_SLOT_ROOT']=os.environ['HERMES_CHILD_SLOT_ROOT']
        cmd=[sys.executable,str(Path(__file__).with_name('native_entry.py'))]
        p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,text=True,bufsize=1,start_new_session=True)
        job['process']=p;lines=queue.Queue(maxsize=512)
        def reader(stream,label):
            try:
                for line in stream:
                    if label=='stdout':lines.put(line,timeout=2)
            finally:
                if label=='stdout':lines.put(None)
        threading.Thread(target=reader,args=(p.stdout,'stdout'),daemon=True).start();threading.Thread(target=reader,args=(p.stderr,'stderr'),daemon=True).start()
        p.stdin.write(json.dumps(packet));p.stdin.close();bytecount=0;completed=False;first=None;physical_usage=[]
        while True:
            if job['cancel'].is_set():raise InterruptedError('cancelled')
            if time.monotonic()-started>job['timeout']:raise TimeoutError('deadline reached')
            try:line=lines.get(timeout=.25)
            except queue.Empty:
                if p.poll() is not None:break
                continue
            if line is None:break
            bytecount+=len(line.encode())
            if bytecount>8_000_000:raise RuntimeError('native stream quota exceeded')
            ev=json.loads(line)
            if ev.get('type')!='ole.native' or not isinstance(ev.get('event'),dict):raise RuntimeError('invalid native worker protocol')
            ev=ev['event']
            if first is None:first=time.monotonic()-started
            safe_emit(job,ev)
            if ev.get('type')=='native.provider_call' and isinstance(ev.get('usage'),dict):physical_usage.append(ev['usage'])
            if ev.get('type')=='native.receipt':
                completed=ev.get('status')=='completed'
                if not completed:raise RuntimeError('native worker '+str(ev.get('error_type','error')))
        rc=p.wait(timeout=10)
        if rc!=0 or not completed:raise RuntimeError('worker did not complete native turn')
        aliases={'input_tokens':('prompt_tokens','input_available'),'cached_input_tokens':('cache_read_tokens','cache_read_available'),'output_tokens':('output_tokens','output_available'),'reasoning_output_tokens':('reasoning_tokens','reasoning_available')}
        job['usage']={out:sum(u[key] for u in physical_usage) for out,(key,flag) in aliases.items() if physical_usage and all(isinstance(u.get(key),(int,float)) and u.get(flag) is not False for u in physical_usage)}
        job['usage']['physical_calls']=len(physical_usage)
        try:
            from hermes_cli.lifecycle import invoke_hook
            invoke_hook('ole_worker_usage',job_id=job['job_id'],usage=job['usage'],model=MODEL,provider='openai-codex',role='worker_aggregate')
        except Exception:pass
        if git(job['work'],'rev-parse','HEAD')!=job['start_sha']:raise RuntimeError('worker made forbidden commit')
        changed=git(job['work'],'status','--porcelain');diff=git(job['work'],'diff','--stat');untracked=git(job['work'],'ls-files','--others','--exclude-standard')
        if not job['read_only'] and not changed:raise RuntimeError('coding task produced no actual changes')
        # Verification executable outside worker write roots: fixed argv, no shell.
        gate_env={k:v for k,v in env.items() if k in ('PATH','LANG','LC_ALL','TZ','TERM','PYTHONPATH','HERMES_DISABLE_LAZY_INSTALLS','HERMES_RUNTIME_DIR')}
        gate_env['HOME']=str(job['root']);gate_env['TMPDIR']=str(job['root']);gate_env['HERMES_HOME']=str(job['root']/'native-home')
        deadline=min(started+job['timeout'],time.monotonic()+120)
        gate_out=job['root']/'verification.stdout';gate_err=job['root']/'verification.stderr'
        with gate_out.open('w') as gout,gate_err.open('w') as gerr:
            gate_out.chmod(0o600);gate_err.chmod(0o600)
            p=subprocess.Popen(job['gate'],cwd=job['work'],env=gate_env,stdout=gout,stderr=gerr,start_new_session=True)
            job['process']=p
            while p.poll() is None:
                if job['cancel'].is_set():raise InterruptedError('cancelled during verification')
                if time.monotonic()>=min(deadline,started+job['timeout']):raise TimeoutError('verification reached job deadline')
                time.sleep(.1)
        if job['cancel'].is_set():raise InterruptedError('cancelled during verification')
        if time.monotonic()>=started+job['timeout']:raise TimeoutError('job deadline reached')
        gate=p
        gate_bytes=gate_out.stat().st_size+gate_err.stat().st_size
        gate_out.unlink();gate_err.unlink()
        receipt={'start_sha':job['start_sha'],'actual_changed_files':changed.splitlines(),'diff_stat':diff,'untracked_files':untracked.splitlines(),'gate_exit':gate.returncode,'gate_output_bytes':gate_bytes,'first_event_seconds':first,'branch':job['branch'],'worktree':str(job['work']),'review_required':True,'model_result_path':str(job['root']/'model-result.md') if (job['root']/'model-result.md').exists() else None,'diff_path':str(job['root']/'changes.patch'),'verification_argv':job['gate'],'verification_scope':'trusted caller command; repository tests may be edited and need review; no hostile-code OS sandbox claim'}
        patch=subprocess.check_output(['git','-C',str(job['work']),'diff','--binary'],text=True)
        for filename in untracked.splitlines():
            target=NATIVE.guarded_path(job,filename)
            extra=subprocess.run(['git','diff','--no-index','--binary','--','/dev/null',str(target)],capture_output=True,text=True)
            if extra.returncode not in (0,1):raise RuntimeError('untracked diff generation failed')
            patch+=extra.stdout
        (job['root']/'changes.patch').write_text(patch);(job['root']/'changes.patch').chmod(0o600)
        if gate.returncode:raise RuntimeError('independent verification failed')
        job['receipt']=receipt;job['status']='completed'
    except InterruptedError:job['status']='cancelled';job['error']='cancelled'
    except Exception as e:job['status']='failed';job['error']=type(e).__name__+': '+str(e)[:180]
    finally:
        if p and p.poll() is None:
            # psutil birth-checked descendant cleanup from native Hermes helper, never global pkill.
            try:
                from agent.transports.codex_app_server import _snapshot_descendants,_reap_snapshotted
                children=_snapshot_descendants(p.pid);p.terminate();p.wait(timeout=3);_reap_snapshotted(children)
            except Exception:
                try:os.killpg(p.pid,signal.SIGKILL)
                except ProcessLookupError:pass
        job.pop('brief',None);job['wall_seconds']=time.monotonic()-started
        safe_emit(job,{'type':'job.'+job['status'],'at':time.time()})
        job.pop('progress_sink',None)
        path=job['root']/'receipt.json';job.pop('process',None)
        final=public(job);final['status']=job['status'];path.write_text(json.dumps(final,ensure_ascii=False));path.chmod(0o600)
        job['_finalized']=True

def status(jobid,wait=0):
    job=JOBS.get(jobid)
    if not job:raise ValueError('unknown job')
    until=time.monotonic()+min(max(wait,0),30)
    while (job['status'] in ('queued','running') or not job.get('_finalized')) and time.monotonic()<until:time.sleep(.2)
    return public(job)
def cancel(jobid):
    if jobid not in JOBS:raise ValueError('unknown job')
    JOBS[jobid]['cancel'].set();return status(jobid)
