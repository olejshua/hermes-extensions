"""Verify reviewed private writer code and the actual mutation-denial boundary.

This verifier proves only its explicitly bound candidate processes. It cannot
turn untouched production Mac/VPS credential holders into an installed single
writer. An installed-authority claim is rejected until real isolation exists.
"""
import inspect
import json
import types
from pathlib import Path
from background_jobs.contracts import ContractError,approved_path,digest,canonical
from .write_boundary import SESSION,save
from .guarded_gmail import GuardedGmail

REQUIRED={
    'reply':{'watcher.service','watcher._make_draft','realtime._draft_one','coordinator._execute_owned','runtime.bootstrap','reply.run'},
    'call_followup':{'watcher.service','followup.execute_once','followup.write','coordinator._execute_owned','runtime.bootstrap'},
    'outreach':{'watcher.service','outreach.run','outreach.authority','outreach.factory','outreach.scope',
                'outreach.readiness','background.worker','background.dispatch','background.policy',
                'outreach.pool_refresh','outreach.successor','outreach.factual_readiness','outreach.gmail_route',
                'outreach.native_plan','outreach.source_graph','writer.execute','writer.session'},
    'smtp_native':{'smtp.native','smtp.budget'},
    'smtp':{'smtp.native','smtp.mac_local','smtp.mac_remote','smtp.mac_command','smtp.vps_legacy'},
}


def point(function):
    return {'path':str(Path(inspect.getfile(function)).resolve()),'source_sha256':digest(Path(inspect.getfile(function)).read_bytes()),
            'code_sha256':digest(code_shape(function.__code__))}


def code_shape(value):
    """Stable across CPython execution/quickening and marshal string interning.

    CPython 3.12 changes marshal reference sharing when a code object starts
    executing. Immutable executable fields and typed constants do not change.
    """
    if isinstance(value,types.CodeType):
        return {'code':value.co_code.hex(),'constants':[code_shape(v)for v in value.co_consts],
                'names':value.co_names,'vars':value.co_varnames,'free':value.co_freevars,'cells':value.co_cellvars,
                'args':(value.co_argcount,value.co_posonlyargcount,value.co_kwonlyargcount),
                'locals':value.co_nlocals,'stack':value.co_stacksize,'flags':value.co_flags,
                'name':value.co_name,'qualname':value.co_qualname,'firstline':value.co_firstlineno,
                'lines':value.co_linetable.hex(),'exceptions':value.co_exceptiontable.hex()}
    if isinstance(value,bytes):return ['bytes',value.hex()]
    if isinstance(value,tuple):return ['tuple',[code_shape(v)for v in value]]
    if isinstance(value,frozenset):return ['frozenset',sorted((code_shape(v)for v in value),key=canonical)]
    if isinstance(value,complex):return ['complex',repr(value)]
    if value is Ellipsis:return ['ellipsis']
    if value is None or type(value)in (str,int,float,bool):return [type(value).__name__,value]
    raise ContractError('native_code_constant_not_supported')


def manifest(host_id,routes,bindings):
    routes=sorted(routes);required=set().union(*(REQUIRED[r]for r in routes))
    if set(bindings)!=required:raise ContractError('native_writer_set_incomplete')
    return {'contract':'reviewed-private-native-writer-code-v2','coverage_scope':'private_candidate_runtime','host_id':host_id,
            'supported_routes':routes,'bindings':{key:point(bindings[key]())for key in sorted(bindings)}}


class PinnedWriterEnforcer:
    def __init__(self,root,*,manifest_ref,manifest_sha256,bindings):
        self.root=Path(root);self.ref,self.sha=manifest_ref,manifest_sha256;self.bindings=bindings;self.boundary=None
    def bind_boundary(self,boundary):
        if self.boundary is not None and self.boundary is not boundary:raise ContractError('native_enforcer_boundary_rebound')
        self.boundary=boundary
    def verify(self):
        raw=approved_path(self.root,self.ref).read_bytes()
        if digest(raw)!=self.sha:raise ContractError('native_reviewed_writer_set_changed')
        expected=json.loads(raw)
        if expected.get('coverage_scope')!='private_candidate_runtime':raise ContractError('installed_writer_isolation_not_proved')
        actual=manifest(expected['host_id'],expected['supported_routes'],self.bindings)
        if actual!=expected:raise ContractError('native_loaded_writer_code_changed')
        if self.boundary is None:raise ContractError('native_enforcer_boundary_unbound')
        # Exercise the actual guarded client + boundary while clearing only this
        # temporary canary's capability. No SDK constructor or network request.
        calls=[]
        class Canary:
            def users(self):return self
            def drafts(self):return self
            def create(self,**kwargs):calls.append('SDK');raise AssertionError('canary SDK mutation reached')
        token=SESSION.set(None)
        try:
            try:GuardedGmail(Canary(),'canary@fixture.invalid',self.boundary).users().drafts().create(userId='me',body={}).execute()
            except ContractError as error:
                if str(error)!='native_write_scope_required':raise
            else:raise ContractError('native_unscoped_mutation_not_blocked')
        finally:SESSION.reset(token)
        if calls:raise ContractError('native_unscoped_sdk_constructor_reached')
        evidence={'contract':'native-candidate-enforcement-observation-v2','coverage_scope':'private_candidate_runtime',
                  'manifest_ref':self.ref,'manifest_sha256':self.sha,'loaded_code':actual['bindings'],
                  'unscoped_actual_boundary_outcome':'native_write_scope_required','sdk_mutations_observed':len(calls)}
        ref,sha=save(self.root,'native-enforcement/'+digest(evidence)+'.json',evidence)
        return {'contract':'outreach-enforced-writer-set-v2','coverage_scope':'private_candidate_runtime','host_id':actual['host_id'],
                'fingerprint':digest(actual),'supported_routes':actual['supported_routes'],'native_evidence_ref':ref,'native_evidence_sha256':sha}
    def check_original(self,proof):
        raw=approved_path(self.root,proof['native_evidence_ref']).read_bytes()
        if digest(raw)!=proof['native_evidence_sha256']:raise ContractError('native_enforcement_original_changed')
        value=json.loads(raw)
        if value.get('manifest_ref')!=self.ref or value.get('manifest_sha256')!=self.sha:
            raise ContractError('native_enforcement_manifest_changed')
