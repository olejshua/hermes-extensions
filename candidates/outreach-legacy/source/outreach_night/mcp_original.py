"""Complete originals through the existing native MCP transport, before render.

No tool names/schema are invented here. A reviewed export of selected actual
notion_2 metadata and a sealed permitted UUID scope are required before spawn.
The standard trust gate, circuit breaker, RPC lock, cancellation and recovery
ladder remain in force. This helper is unregistered and disabled by default.
"""
import contextvars
import json
import uuid
from functools import partial
from pathlib import Path
from background_jobs.contracts import ContractError,canonical,digest,approved_path
from .clients import reviewed_module
from .sources import persist

DATASOURCE='00000000-0000-4000-8000-00000000000e'


def readonly_operation(metadata,tool_name,args,scope,candidate):
    """Domain authority from exact known UUID/parameters, never names or hints."""
    if (metadata.get('contract')!='root-cached-selected-notion-reader-schema-v1' or
        metadata.get('server_alias')!='notion_2' or scope.get('contract')!='outreach-night-scope-staging-v1' or
        scope.get('category')!='stealth' or scope.get('account')!='operator@secondary.example.invalid' or
        scope.get('source_authority')!={'server':'notion_2','datasource_id':DATASOURCE} or
        candidate not in scope.get('permitted_candidates',[])):
        raise ContractError('stealth_sealed_source_scope_required')
    rows=metadata.get('tools',[]);matched=[r for r in rows if isinstance(r,dict) and r.get('name')==tool_name]
    record=matched[0] if len(matched)==1 else None
    if not isinstance(record,dict) or not isinstance(record.get('inputSchema'),dict):raise ContractError('native_notion_selected_reader_required')
    try:
        if str(uuid.UUID(candidate['source_id']))!=candidate['source_id']:raise ValueError()
    except (ValueError,TypeError,KeyError):raise ContractError('stealth_sealed_source_uuid_required') from None
    if tool_name=='notion-fetch':
        known={DATASOURCE,'collection://'+DATASOURCE,candidate['source_id']}
        if (not isinstance(args,dict) or not {'id'}<=set(args)<= {'id','include_discussions','include_file_urls','include_transcript'} or
            type(args['id']) is not str or args['id'] not in known or any(type(value) is not bool for key,value in args.items() if key!='id')):
            raise ContractError('native_notion_exact_read_operation_required')
    elif tool_name=='notion-query-data-sources':
        # Faithful rows only. No SQL, arbitrary view, linked datasource, dynamic
        # filters/sorts or unknown pagination contract is admitted here.
        data=args.get('data') if isinstance(args,dict) and set(args)=={'data'} else None
        if (not isinstance(data,dict) or set(data)!={'mode','data_source_url','limit'} or data['mode']!='rows' or
            data['data_source_url']!='collection://'+DATASOURCE or type(data['limit']) is not int or not 1<=data['limit']<=100):
            raise ContractError('native_notion_exact_read_operation_required')
    else:raise ContractError('native_notion_exact_read_operation_required')
    # These flags are neither promoted nor used as operation truth. They stay
    # original and determine the native trust/recovery behaviour below.
    return record


class NativeMCPOriginal:
    def __init__(self,policy,root):self.policy,self.root=policy,Path(root)
    def read(self,tool_name,args,binding,active):
        if self.policy.get('outreach_profile_enabled') is not True:raise ContractError('outreach_staging_profile_not_activated')
        # This is a private, credential-free schema export, not server config or
        # an auth probe. Root supplies exact selected reader names and hashes.
        ref=self.policy.get('notion_reader_metadata_ref')
        if not isinstance(ref,str):raise ContractError('native_notion_reader_metadata_missing')
        raw=approved_path(self.root,ref).read_bytes()
        if digest(raw)!=self.policy.get('notion_reader_metadata_sha256'):raise ContractError('native_notion_reader_metadata_changed')
        metadata=json.loads(raw)
        scope_ref=self.policy.get('outreach_scope_ref')
        if not isinstance(scope_ref,str):raise ContractError('stealth_sealed_source_scope_required')
        scope_raw=approved_path(self.root,scope_ref).read_bytes()
        if digest(scope_raw)!=self.policy.get('outreach_scope_sha256'):raise ContractError('stealth_sealed_source_scope_changed')
        scope=json.loads(scope_raw);candidate=binding.get('candidate')
        if binding.get('category')!='stealth' or binding.get('account')!='operator@secondary.example.invalid':raise ContractError('stealth_workspace_binding_missing')
        record=readonly_operation(metadata,tool_name,args,scope,candidate)
        # Pins of actual public modules checked before importing the host core.
        core=Path(self.policy['core']);pins=self.policy['active_dependencies']
        for name in ('mcp_tool_handlers','mcp_tool','mcp_tool_registration','mcp_schema_cache','mcp_tool_transport'):
            reviewed_module(core/'tools'/(name+'.py'),pins)
        from tools import mcp_tool_handlers as handlers
        server_name='notion_2';op='tools/call '+tool_name
        native_read_only=handlers._tool_is_read_only(server_name,tool_name)
        active()
        error=handlers._trust_gate_check(server_name,tool_name) or handlers._check_circuit_breaker(server_name)
        if error is not None:raise ContractError('native_notion_reader_blocked')
        timeout=self.policy.get('native_read_timeout_seconds',120)
        server,error=handlers._acquire_call_server(server_name,timeout)
        if server is None:raise ContractError('native_notion_reader_unavailable')
        async def call():
            active()
            async with server._rpc_lock,handlers._track_inflight_rpc(server,server_name,op,retry_safe=native_read_only):
                server._pending_call_context=contextvars.copy_context()
                try:result=await handlers._call_tool_racing_stdio_death(server,server_name,tool_name,args)
                finally:server._pending_call_context=None
            if getattr(server,'_mark_session_proven',None) is not None:server._mark_session_proven()
            # Actual MCP SDK model dump: all content blocks and structuredContent
            # before _render_call_tool_result caps, transforms or spills anything.
            if not callable(getattr(result,'model_dump',None)):raise ContractError('native_mcp_original_sdk_result_required')
            original=result.model_dump(mode='json',by_alias=True)
            if original.get('isError') is True:return canonical({'error':'native_reader_error','original':original})
            return canonical({'original':original})
        session_expired=partial(handlers._handle_session_expired_and_retry,call_may_have_side_effects=not native_read_only)
        def failure(_):handlers._core._bump_server_error(server_name)
        response=handlers._dispatch(server_name,server,op,call,timeout,
            (handlers._handle_stdio_child_exited_and_retry,handlers._handle_auth_error_and_retry,session_expired),failure,record_outcome=True)
        active();packet=json.loads(response)
        if 'error' in packet or not isinstance(packet.get('original'),dict):raise ContractError('native_notion_original_read_failed')
        return persist(self.root,'notion_original','notion_2:'+tool_name+':'+digest(args),
            {'arguments':args,'reader_schema':record,'sdk_result':packet['original']},binding,op)
