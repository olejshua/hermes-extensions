"""Explicit restricted native Hermes subscription coding backend; no subprocess agent sandbox claim."""
from __future__ import annotations
import json,os,time
from pathlib import Path
from contextvars import ContextVar
_ACTIVE=ContextVar('ole_coding_active_job',default=None)
def guarded_path(job,relative):
    if not isinstance(relative,str) or len(relative)>2048:raise ValueError('relative repository path required')
    rel=Path(relative)
    if rel.is_absolute() or '..' in rel.parts:raise PermissionError('relative nonescaping repository path required')
    candidate=job['work']/relative
    for item in (candidate,*candidate.parents):
        if item==job['work'].parent:break
        if item.is_symlink():raise PermissionError('symlink source paths excluded')
    p=candidate.resolve()
    if not p.is_relative_to(job['work'].resolve()) or '.git' in p.parts:raise PermissionError('outside worktree or Git metadata')
    if p.name in ('.env','auth.json','credentials.json') or (p.name.startswith('.env.') and p.name!='.env.example'):raise PermissionError('credential-like source file excluded')
    return p

def operation(args,**_):
    job=_ACTIVE.get()
    if not job:raise PermissionError('coding file tools only inside owned native job')
    if args.get('job_id')!=job['job_id']:raise PermissionError('foreign job')
    if job['cancel'].is_set():raise InterruptedError('job cancelled')
    if time.monotonic()-job['_native_start']>job['timeout']:raise TimeoutError('job deadline')
    mode=args['operation'];p=guarded_path(job,args.get('path','.'))
    if mode=='list':
        files=[]
        for v in p.rglob('*'):
            try:allowed=guarded_path(job,str(v.relative_to(job['work'].resolve())))
            except PermissionError:continue
            if allowed.is_file():files.append(str(allowed.relative_to(job['work'].resolve())))
            if len(files)>=2000:break
        return json.dumps({'files':files})
    if mode=='read':
        if p.stat().st_size>200000:raise ValueError('bounded file read required')
        return p.read_text()
    if mode=='write':
        if job['read_only']:raise PermissionError('read-only job')
        body=args.get('content','')
        if len(body)>500000:raise ValueError('bounded source file required')
        p.parent.mkdir(parents=True,exist_ok=True);p.write_text(body);return json.dumps({'written':str(p.relative_to(job['work'].resolve())),'bytes':len(body.encode())})
    if mode=='search':
        needle=args.get('needle','');hits=[]
        if not isinstance(needle,str) or not needle or len(needle)>500:raise ValueError('bounded literal search required')
        for f in p.rglob('*'):
            try:f=guarded_path(job,str(f.relative_to(job['work'].resolve())))
            except PermissionError:continue
            if f.is_file() and '.git' not in f.parts and f.stat().st_size<200000:
                try: lines=f.read_text().splitlines()
                except (UnicodeError,OSError):continue
                for i,line in enumerate(lines):
                    if needle in line:hits.append({'file':str(f.relative_to(job['work'].resolve())),'line':i+1,'text':line[:500]})
                    if len(hits)>=100:return json.dumps(hits)
        return json.dumps(hits)
    raise ValueError('unknown operation')

def run_native(job,emit,runtime=None):
    from hermes_cli.auth_codex import resolve_codex_runtime_credentials
    from run_agent import AIAgent
    from agent.aux_provider_policy import allowed_auxiliary_providers
    from tools.child_slot_limiter import child_slot
    r=runtime or resolve_codex_runtime_credentials()
    if r.get('provider')!='openai-codex' or r.get('auth_mode')!='chatgpt' or r.get('base_url','').rstrip('/')!='https://chatgpt.com/backend-api/codex':raise RuntimeError('existing ChatGPT subscription route required')
    job['_native_start']=time.monotonic();token=_ACTIVE.set(job)
    def progress(tool_name=None,**kw):emit(job,{'type':'native.tool','tool_name':str(tool_name or 'ole_coding_file'),'at':time.time()})
    try:
        with child_slot(), allowed_auxiliary_providers({'openai-codex'}):
            a=AIAgent(model='gpt-6.1-sol',provider='openai-codex',api_mode='codex_responses',api_key=r['api_key'],base_url=r['base_url'],reasoning_config={'effort':'medium'},enabled_toolsets=['ole-coding-files'],disabled_toolsets=['terminal','email','calendar','browser','web','memory','skills','delegation'],max_iterations=300,quiet_mode=True,save_trajectories=False,cwd=str(job['work']),session_id='olecoding-'+job['job_id'],fallback_model=None,credential_pool=None,tool_start_callback=progress,ephemeral_system_prompt='You are a coding worker. Only own file tools are allowed. No shell, network, credentials, mail/calendar/CRM, git commit/push/merge or external writes. Explicit Sol6.1medium. This is a restricted native backend; never claim CLI sandbox. Actual source changes and independent verification determine acceptance.')
            import model_tools
            a.tools=model_tools.get_tool_definitions(enabled_toolsets=['ole-coding-files'],quiet_mode=True,skip_tool_search_assembly=True)
            toolnames=[t.get('function',{}).get('name') for t in a.tools]
            emit(job,{'type':'native.tools','names':toolnames,'at':time.time()})
            if 'ole_coding_file' not in toolnames:raise RuntimeError('owned coding file tool missing from actual primary schemas')
            a.valid_tool_names=set(toolnames)
            if set(toolnames)!={'ole_coding_file'}:raise RuntimeError('unexpected native coding tool surface')
            # Verify real dispatch synchronously before spending model usage.
            probe=model_tools.handle_function_call('ole_coding_file',{'job_id':job['job_id'],'operation':'list','path':'.'},enabled_tools=['ole_coding_file'],enabled_toolsets=['ole-coding-files'])
            if not isinstance(probe,str) or not isinstance(json.loads(probe).get('files'),list):raise RuntimeError('owned coding tool dispatch failed')
            a._persist_disabled=True
            emit(job,{'type':'native.started','at':time.time(),'api_mode':a.api_mode})
            prompt='Job ID '+job['job_id']+'\nWorktree contains repository source, read its AGENTS/CLAUDE rules if present. Task:\n'+job['brief']+'\nUse ole_coding_file for list/read/literalsearch/write, including exact job_id. Verification is executed independently after you finish; no shell available. Return concise outcome and changed files.'
            result=a.run_conversation(prompt)
            if job['cancel'].is_set():raise InterruptedError('cancelled')
            if time.monotonic()-job['_native_start']>job['timeout']:raise TimeoutError('deadline')
            if isinstance(result,dict) and result.get('error'):raise RuntimeError('native Hermes model failed')
            final=result.get('final_response') or result.get('response') if isinstance(result,dict) else result
            # Task result is intentionally reviewable artifact, never telemetry/event body.
            if isinstance(final,str):
                path=Path(job['work']).parent/'model-result.md';path.write_text(final[:20000]);path.chmod(0o600)
            emit(job,{'type':'native.completed','at':time.time()})
    finally:_ACTIVE.reset(token)
