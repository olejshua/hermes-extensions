"""Reuse existing physical native telemetry through a content-free typed bridge."""
import importlib.util
import re
from pathlib import Path
from .contracts import digest,private_dir
USAGE_FIELDS={'input_tokens','prompt_tokens','output_tokens','cache_read_tokens','cache_write_tokens','reasoning_tokens',
    'input_available','output_available','cache_read_available','cache_write_available','reasoning_available',
    'input_tokens_available','prompt_tokens_available','output_tokens_available','cache_read_tokens_available',
    'cache_write_tokens_available','reasoning_tokens_available','cached_input_tokens'}

def register_native(ctx,identity,spool,*,telemetry_path=None,telemetry=None):
    spool=private_dir(spool)
    if telemetry is None:
        path=Path(telemetry_path or '/home/hermes/.hermes/plugins/ole-coding/telemetry.py')
        if path.is_symlink() or not path.is_absolute() or not path.is_file():raise ValueError('existing_native_telemetry_required')
        spec=importlib.util.spec_from_file_location('ole_background_physical_telemetry',path)
        telemetry=importlib.util.module_from_spec(spec);spec.loader.exec_module(telemetry)
    counts={'physical_hook_attempts':0,'auxiliary_hook_attempts':0,'events_dropped':0}
    class PrivacyBridge:
        def register_hook(self,event,callback):
            def handle(**payload):
                safe={}
                for key in ('provider','model','response_model','api_mode','error_type','tool_name'):
                    value=payload.get(key)
                    safe[key]=value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',value) else None
                for key in ('retry_count','api_call_count','started_at','ended_at','api_duration','first_chunk_at','duration_ms'):
                    value=payload.get(key);safe[key]=value if type(value) in (int,float) and value>=0 else None
                usage=payload.get('usage')
                safe['usage']={key:value for key,value in usage.items() if key in USAGE_FIELDS and (value is None or type(value) in (int,float,bool))} if isinstance(usage,dict) else None
                safe['aux_task']='background_job'
                if event in ('post_api_request','api_request_error'):
                    counts['physical_hook_attempts']+=1
                    if len(list(spool.glob('*.json')))>=2048:counts['events_dropped']+=1
                if event=='post_auxiliary_call':counts['auxiliary_hook_attempts']+=1
                return callback(**safe)
            ctx.register_hook(event,handle)
    # Hash full source/actor/scope binding. Public telemetry never stores source ID,
    # owner, session, goal, output, tool arguments or original content.
    telemetry.register_native(PrivacyBridge(),{'job_id':'bgworker-'+digest(identity)},lambda *_:None,str(spool))
    return counts
