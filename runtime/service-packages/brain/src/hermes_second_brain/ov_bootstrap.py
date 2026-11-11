"""Pinned OV process adapter: Hermes alone owns OAuth refresh; metadata only telemetry."""
from __future__ import annotations
import contextvars, importlib.metadata, json, os, sys, time, uuid,threading
from pathlib import Path
from .ole_policy import atomic

ALLOWED_BASE='https://chatgpt.com/backend-api/codex'
span=contextvars.ContextVar('ov_usage',default=None)
ADAPTER_PROTOCOL='ole-openviking-0.4.16-v4-reserved'
physical_lane=threading.BoundedSemaphore(1)

def credentials(**kwargs):
    from hermes_cli.auth_codex import resolve_codex_runtime_credentials
    c=resolve_codex_runtime_credentials(read_only=kwargs.get('read_only',False),refresh_if_expiring=kwargs.get('refresh_if_expiring',True))
    if c.get('provider')!='openai-codex' or c.get('auth_mode')!='chatgpt' or c.get('base_url','').rstrip('/')!=ALLOWED_BASE or not c.get('api_key'):
        raise RuntimeError('Subscription-only auth policy rejected credential route')
    return {'api_key':c['api_key'],'base_url':ALLOWED_BASE}

def available():
    try:credentials(read_only=True,refresh_if_expiring=False);return True
    except Exception:return False

def field(obj,name,default=0):return obj.get(name,default) if isinstance(obj,dict) else getattr(obj,name,default)

def canonical_usage(raw):
    if raw is None:return None
    prompt=field(raw,'input_tokens',None);output=field(raw,'output_tokens',None)
    cache=field(field(raw,'input_tokens_details',None),'cached_tokens',None)
    reasoning=field(field(raw,'output_tokens_details',None),'reasoning_tokens',None)
    prompt_known=isinstance(prompt,int);cache_known=isinstance(cache,int);output_known=isinstance(output,int)
    return {'input_tokens':max(0,prompt-cache) if prompt_known and cache_known else 0,'input_tokens_available':prompt_known and cache_known,'input_available':prompt_known and cache_known,
        'prompt_tokens':prompt if prompt_known else 0,'prompt_tokens_available':prompt_known,
        'cache_read_tokens':cache if cache_known else 0,'cache_read_tokens_available':cache_known,'cache_read_available':cache_known,
        'output_tokens':output if output_known else 0,'output_tokens_available':output_known,'output_available':output_known,
        'reasoning_tokens':reasoning if isinstance(reasoning,int) else 0,'reasoning_available':isinstance(reasoning,int)}

def emit(event):
    root=Path(os.getenv('HERMES_HOME','/home/hermes/.hermes'))/'ole-observability/external-spool'
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Never fill disk if the importer is stopped. Report explicit metadata backpressure.
    if len(list(root.glob('*.json')))>=10000: raise RuntimeError('External telemetry spool full')
    atomic(root/(event['api_request_id']+'.json'),json.dumps(event,separators=(',',':')).encode())

def install():
    if importlib.metadata.version('openviking')!='0.4.16':raise RuntimeError('OV adapter requires pinned0.4.16')
    sys.path.insert(0,os.environ.get('OLE_HERMES_CORE','/home/hermes/.hermes/hermes-agent'))
    from openviking.models.vlm.backends import codex_vlm, codex_auth, codex_responses_adapter as adapter
    codex_vlm.resolve_codex_runtime_credentials=credentials
    codex_auth.has_codex_auth_available=available
    # Prevent static credentials or a foreign base overriding our subscription broker.
    def resolve(self):
        if self.config.get('api_key') or self.config.get('api_base',ALLOWED_BASE).rstrip('/')!=ALLOWED_BASE:raise RuntimeError('Explicit foreign credential forbidden')
        c=credentials();return c['api_key'],c['base_url']
    codex_vlm.CodexVLM._resolve_runtime_credentials=resolve
    build=codex_vlm.CodexVLM._build_responses_client
    def build_no_retry(self,*args,**kwargs):
        client=build(self,*args,**kwargs).with_options(max_retries=0)
        create=client.responses.create
        def physical(**request_kwargs):
            expected_model=os.environ.get('OLE_BRAIN_MODEL','gpt-6-sol')
            effort=os.environ.get('OLE_BRAIN_REASONING','high')
            if request_kwargs.get('model')!=expected_model or effort not in ('low','medium','high','xhigh'):
                raise RuntimeError('Unexpected subscription model or reasoning route')
            request_kwargs['reasoning']={'effort':effort}
            from .deep_budget import update
            current=span.get()
            budget_control=Path(os.getenv('OLE_BRAIN_BUDGET_DIR','/home/hermes/hermes-extensions-staging/brain/state/deep-budget'))/'control.json'
            if current and budget_control.exists():current['job_id']=json.loads(budget_control.read_text()).get('active_job_id')or current['job_id']
            update(reserve=True,reservation_id=current['api_request_id']if current else None)
            if current is not None:current['physical_started']=True;current['started_at']=time.time()
            result=create(**request_kwargs)
            headers=getattr(getattr(result,'response',None),'headers',{})
            limits={}
            for key in ('x-codex-primary-used-percent','x-codex-secondary-used-percent','x-codex-primary-reset-after-seconds','x-codex-secondary-reset-after-seconds'):
                try:
                    value=float(headers[key])
                    if value>=0 and value<float('inf'):limits[key]=value
                except (KeyError,ValueError,TypeError):pass
            if current and limits:current['subscription_rate_limits']=limits
            return result
        client.responses.create=physical
        return client
    codex_vlm.CodexVLM._build_responses_client=build_no_retry
    convert=adapter._build_chat_completion_like_response
    def capture(raw,model):
        current=span.get()
        if current is not None:current['usage']=canonical_usage(field(raw,'usage',None))
        return convert(raw,model)
    adapter._build_chat_completion_like_response=capture
    request=adapter.CodexCompletionsAdapter._create_response
    def measured(self,**kwargs):
        start=time.time();identity=str(uuid.uuid4());event={'event':'ole_external_provider_call','role':'auxiliary','adapter_protocol':ADAPTER_PROTOCOL,'job_id':os.getenv('OLE_BRAIN_JOB_ID','openviking'),'aux_task':'brain_ingest','provider':'openai-codex','model':kwargs.get('model') or self._model,'api_request_id':identity,'started_at':start,'usage':None};token=span.set(event)
        quota_error=False
        physical_lane.acquire()
        try:return request(self,**kwargs)
        except Exception as exc:
            quota_error=getattr(exc,'status_code',None)==429
            raise
        finally:
            end=time.time();span.reset(token)
            if event.pop("physical_started",False):
                event.update(ended_at=end,api_duration=end-event['started_at'])
                from .deep_budget import finish
                try:finish(event,quota_error,emit)
                finally:physical_lane.release()
            else:physical_lane.release()
    adapter.CodexCompletionsAdapter._create_response=measured
    from .ole_policy import digest
    stamp={'protocol':ADAPTER_PROTOCOL,'pid':os.getpid(),'started_at':time.time(),'ov_version':importlib.metadata.version('openviking'),'adapter_sha256':digest(Path(__file__).read_bytes()),'budget_sha256':digest((Path(__file__).parent/'deep_budget.py').read_bytes()),'provider':'openai-codex','model':os.getenv('OLE_BRAIN_MODEL','gpt-6-sol'),'reasoning_effort':os.getenv('OLE_BRAIN_REASONING','high')}
    atomic(Path(os.getenv('OLE_BRAIN_ADAPTER_STAMP','/home/hermes/hermes-extensions-staging/brain/state/adapter-startup.json')),json.dumps(stamp).encode())

def main():
    # Native package import eagerly initializes configuration via module loggers.
    for i,arg in enumerate(sys.argv):
        if arg=='--config' and i+1<len(sys.argv):os.environ['OPENVIKING_CONFIG_FILE']=sys.argv[i+1];break
        if arg.startswith('--config='):os.environ['OPENVIKING_CONFIG_FILE']=arg.split('=',1)[1];break
    install()
    from openviking_cli.server_bootstrap import main as serve
    serve()
if __name__=='__main__':main()
