"""Fence existing private hold operations; existing routing/policy stays authoritative."""
from datetime import datetime,timedelta
from background_jobs.contracts import ContractError
from .write_boundary import SESSION


def calendar_in_session(native):
    record=SESSION.get()
    if record is None:raise ContractError('native_calendar_write_scope_required')
    return Resource(native,'calendar',record)


class Resource:
    def __init__(self,native,path,record):self.native,self.path,self.record=native,path,record
    def __getattr__(self,name):
        if name.startswith('_'):raise AttributeError(name)
        def call(*args,**kwargs):
            result=getattr(self.native,name)(*args,**kwargs);operation=self.path+'.'+name
            if callable(getattr(result,'execute',None)):return Request(result,operation,kwargs,self.record)
            return Resource(result,operation,self.record)
        return call


class Request:
    def __init__(self,native,operation,kwargs,record):self.native,self.operation,self.kwargs,self.record=native,operation,kwargs,record
    def execute(self,**kwargs):
        self.record['boundary'].check_session(self.record)
        if getattr(self.native,'method',None)!='GET'and self.operation!='calendar.freebusy.query':
            body=self.kwargs.get('body',{})
            if self.operation!='calendar.events.insert' or kwargs or self.kwargs.get('sendUpdates')!='none':
                raise ContractError('native_calendar_only_private_proposal_holds')
            if (body.get('attendees')or body.get('visibility')!='private'or body.get('transparency')!='opaque'or
                body.get('extendedProperties',{}).get('private',{}).get('hermesSlotHold')!='1'):
                raise ContractError('native_calendar_only_private_proposal_holds')
            try:duration=datetime.fromisoformat(body['end']['dateTime'])-datetime.fromisoformat(body['start']['dateTime'])
            except (KeyError,TypeError,ValueError):raise ContractError('native_calendar_exact_hold_duration_required')from None
            if duration!=timedelta(minutes=30):raise ContractError('native_calendar_exact_hold_duration_required')
        result=self.native.execute(**kwargs)
        self.record['boundary'].check_session(self.record)
        return result
