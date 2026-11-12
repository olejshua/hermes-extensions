"""Actual managed mail_policy transaction around create AND exact readback.

Only existing private30-minute holds are possible. Every SDK call is fenced and
its complete result retained privately. No invitation/delete/update methods.
"""
from background_jobs.contracts import ContractError,PermanentFailure
from background_jobs.outreach_candidate import ACCOUNTS,GmailDraftAdapter
from gtm.calendar.routing import calendar_id_for
from .sources import persist


class CalendarRecorder:
    def __init__(self,calendar,root,identity,active):
        self.calendar,self.root,self.identity,self.active=calendar,root,identity,active
        self.originals=[]
    def calendarList(self):return _Resource(self,self.calendar.calendarList(),'calendar.calendarList',{'list'})
    def freebusy(self):return _Resource(self,self.calendar.freebusy(),'calendar.freebusy',{'query'})
    def events(self):return _Resource(self,self.calendar.events(),'calendar.events',{'get','list','insert'})


class _Resource:
    def __init__(self,owner,resource,prefix,allowed):self.owner,self.resource,self.prefix,self.allowed=owner,resource,prefix,allowed
    def __getattr__(self,name):
        if name not in self.allowed:raise ContractError('calendar_operation_not_permitted')
        def request(**kwargs):
            if name=='insert':
                body=kwargs.get('body',{})
                alias=ACCOUNTS[self.owner.identity['item']['category']][0]
                if (kwargs.get('calendarId')!=calendar_id_for(alias,project=self.owner.identity['item']['category']) or kwargs.get('sendUpdates')!='none' or
                    body.get('visibility')!='private' or body.get('transparency')!='opaque' or body.get('attendees')):
                    raise ContractError('only_private_managed_hold_permitted')
            native=getattr(self.resource,name)(**kwargs);owner=self.owner;operation=self.prefix+'.'+name
            class Request:
                def execute(self):
                    owner.active()
                    result=native.execute()
                    owner.originals.append(persist(owner.root,'calendar_original',operation+':'+str(len(owner.originals)),
                        {'request':kwargs,'response':result},owner.identity,operation))
                    owner.active()
                    return result
            return Request()
        return request


class ManagedDraftAdapter(GmailDraftAdapter):
    def managed(self,mail_policy,calendar):self.mail_policy,self.calendar=mail_policy,calendar;return self
    def execute_once(self,identity,active):
        if not self.plan['proposed_slots']:return super().execute_once(identity,active)
        gate=getattr(self.live_check,'pre_reservation',None)
        if not callable(gate):raise PermanentFailure('native_pre_reservation_gates_missing')
        active();gate(identity,self.plan)
        # The R3 engine has already marked the durable Draft Attempt. The
        # existing mail_policy additionally journals each hold BEFORE insert.
        recorded=CalendarRecorder(self.calendar,self.store.root,identity,active)
        self.calendar_originals=recorded.originals
        owner_thread='outreach:'+identity['job_id']+':'+identity['item']['item_id']
        active()
        alias=ACCOUNTS[identity['item']['category']][0]
        with self.mail_policy.scheduling(recorded,alias,owner_thread,
                                          [self.plan['candidate']['to']],self.plan['proposed_slots'],
                                          project=identity['item']['category']):
            super().execute_once(identity,active)
            proof=self.readback(identity)
            if proof.get('state')!='verified':raise ContractError('managed_draft_readback_unverified')
