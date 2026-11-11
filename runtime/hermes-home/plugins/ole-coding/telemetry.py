"""Worker native hook collector, bounded private content-free spool and stream."""
import json,os,time,uuid
from pathlib import Path
def register_native(ctx,job,emit,spool):
    def handler(event,**p):
        # Only native post/error physical hooks contain provider-completed counts.
        safe={k:p.get(k) for k in ('provider','model','response_model','api_mode','aux_task','retry_count','api_call_count','started_at','ended_at','api_duration','first_chunk_at','usage','error_type')}
        safe.update(event='ole_external_provider_call',event_id=str(uuid.uuid4()),job_id=job['job_id'],role='worker_physical',source_event=event)
        root=Path(spool);root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if root.is_symlink():raise PermissionError('unsafe telemetry spool')
        files=list(root.glob('*.json'))
        if len(files)>=2048:return
        path=root/(safe['event_id']+'.json');tmp=path.with_suffix('.tmp');fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:json.dump(safe,f)
        tmp.replace(path)
        emit(job,{'type':'native.provider_call','at':time.time(),'provider':safe['provider'],'model':safe['model'],'duration':safe['api_duration'],'usage':safe['usage']})
    for event in ('pre_tool_call','post_tool_call'):
        ctx.register_hook(event,lambda _event=event,**p:emit(job,{'type':'native.tool.started' if _event=='pre_tool_call' else 'native.tool.completed','tool_name':p.get('tool_name'),'duration_ms':p.get('duration_ms'),'at':time.time()}))
    for event in ('post_api_request','api_request_error','post_auxiliary_call'):
        ctx.register_hook(event,lambda _event=event,**p:handler(_event,**p))
