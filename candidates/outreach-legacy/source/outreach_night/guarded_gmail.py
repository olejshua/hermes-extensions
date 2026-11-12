"""Enforce native capabilities on the existing SDK client; preserve all GETs."""
from background_jobs.contracts import ContractError
from .write_boundary import MUTATIONS


class GuardedGmail:
    def __init__(self,native,account,boundary):self._native,self.account,self.boundary=native,account,boundary
    def users(self):return Resource(self,self._native.users(),'users')
    def new_batch_http_request(self,*args,**kwargs):return ReadBatch(self._native.new_batch_http_request(*args,**kwargs))


class ReadBatch:
    def __init__(self,native):self.native=native
    def add(self,request,*args,**kwargs):
        if isinstance(request,MutationRequest)or getattr(request,'method',None)!='GET':
            raise ContractError('native_mutation_batch_not_permitted')
        return self.native.add(request,*args,**kwargs)
    def execute(self,*args,**kwargs):return self.native.execute(*args,**kwargs)


class Resource:
    def __init__(self,owner,native,path):self.owner,self.native,self.path=owner,native,path
    def __getattr__(self,name):
        if name.startswith('_'):raise AttributeError(name)
        operation=self.path+'.'+name
        def call(*args,**kwargs):
            method=getattr(self.native,name)
            if operation in MUTATIONS:
                if args:raise ContractError('native_named_mutation_parameters_required')
                return MutationRequest(self.owner,operation,kwargs,method)
            result=method(*args,**kwargs)
            if callable(getattr(result,'execute',None)):
                if getattr(result,'method',None)!='GET':raise ContractError('native_unbound_mutation_not_permitted')
                return result
            return Resource(self.owner,result,operation)
        return call


class MutationRequest:
    def __init__(self,owner,operation,kwargs,method):self.owner,self.operation,self.kwargs,self.method=owner,operation,kwargs,method
    def execute(self,**kwargs):
        if kwargs and kwargs!={'num_retries':0}:raise ContractError('native_mutation_retry_not_permitted')
        return self.owner.boundary.execute(self.owner.account,self.operation,self.kwargs,
            lambda approved:self.method(**approved).execute(num_retries=0))
