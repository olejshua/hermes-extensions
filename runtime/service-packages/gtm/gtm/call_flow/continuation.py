"""Resume explicitly requested calls through the existing native Telegram wake path.

Waiting for Wispr uses reads, never model polls. Native admission and source
identity are retained across restart; target writes remain in the normal Main
turn and its existing steering/approval boundaries.
"""
import asyncio
import json
import re
import time
from types import SimpleNamespace


class Continuation:
    def __init__(self,runtime):
        self.runtime=runtime;self.gateway=None;self.task=None;self.sources={};self.enabled=False

    async def ready(self,gateway,**_):
        if self.gateway is gateway and self.task and not self.task.done():return
        if self.task:self.task.cancel()
        self.gateway=gateway
        from gateway.session import SessionSource
        from gateway.config import Platform
        for row in self.runtime.requests.origins():
            try:
                data=json.loads(row['source']);transport=data.pop('transport_profile',None)
                data['platform']=Platform(data['platform'])
                source=gateway._restored_source(SimpleNamespace(origin=SessionSource(**data),transport_profile=transport))
                if self.authorized(source,row['session_key']):
                    self.sources[row['session_key']]=source
                    self.runtime.main_binding.routes[row['session_key']]=source
            except Exception:continue
        if self.enabled:self.task=asyncio.create_task(self.run(),name='gtm-requested-call-continuation')

    def authorized(self,source,key):
        return (self.gateway is not None and self.runtime.main_binding.bound(source)
                and self.gateway._is_user_authorized_for_source(source)
                and self.gateway._session_key_for_source(source)==key)

    async def admitted(self,event,gateway,session_key,**_):
        if getattr(event,'internal',False):return
        if self.gateway is not gateway:await self.ready(gateway)
        if not self.authorized(event.source,session_key):return
        self.runtime.main_binding.admitted(event,gateway,session_key)
        self.sources[session_key]=event.source
        from gateway.session_identity import transport_profile_of
        fields=('chat_id','user_id','chat_type','thread_id','profile','scope_id')
        data={k:getattr(event.source,k,None)for k in fields}
        data.update(platform='telegram',transport_profile=transport_profile_of(event.source),message_id=None)
        self.runtime.requests.save_origin(session_key,data)

    def prepare(self,args,*,session_id=None,actor=None):
        if args.get('manual_completed')is not True or not isinstance(args.get('manual_request_id'),str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',args['manual_request_id']):
            raise ValueError('manual_completed_request_required')
        key=self.runtime.requests.accept(args)
        bound=self.runtime.main_binding.verified(session_id,actor)
        if bound:
            from gateway.session_context import get_session_env
            origin=get_session_env('HERMES_SESSION_KEY','')
            if origin in self.sources:self.runtime.requests.bind_origin(key,origin)
        try:
            from gateway.session_context import get_session_env
            telegram=get_session_env('HERMES_SESSION_PLATFORM','')=='telegram'
        except ImportError:telegram=False
        result=self.runtime.prepare(args,session_id=session_id,require_model_proof=telegram)
        row=self.runtime.requests.get(key)
        if row['state']=='source_pending' and row['origin_key']:
            self.runtime.requests.wake(key,'waiting')
            result['automatic_retry_scheduled']=bool(self.task and not self.task.done())
        elif row['state']=='source_ready' and row['origin_key'] and row['wake_state'] is None:
            self.runtime.requests.wake(key,'foreground')
        return result

    async def tick(self,now=None):
        now=time.time() if now is None else now
        for row in self.runtime.requests.due(now):
            source=self.sources.get(row['origin_key'])
            if source is None or not self.authorized(source,row['origin_key']):continue
            await asyncio.to_thread(self.runtime.resume_source,row['id'])
        for row in self.runtime.requests.alerts():
            source=self.sources.get(row['origin_key'])
            if source is None or not self.authorized(source,row['origin_key']):continue
            adapter=self.gateway._delivery_adapter_for(source)
            if adapter is None:continue
            message=('Das Transkript für die beauftragte Gesprächsnachbereitung fehlt weiterhin. Der Auftrag bleibt gespeichert.'
                     if row['reason']=='transcript_still_missing_after_one_day' else
                     'Die Gesprächsnachbereitung konnte noch nicht abgeschlossen werden. Der Auftrag und bereits erledigte Schritte sind gespeichert.')
            self.runtime.requests.wake(row['id'],'notice_attempted',now)
            try:
                result=await adapter.send(source.chat_id,message,metadata={'thread_id':source.thread_id})
                if getattr(result,'success',False)is True:self.runtime.requests.wake(row['id'],'notice_delivered',now)
            except Exception:pass  # Unclear send is not blindly repeated.
        for row in self.runtime.requests.waiting_wakes():
            source=self.sources.get(row['origin_key'])
            if source is None or not self.authorized(source,row['origin_key']):continue
            adapter=self.gateway._delivery_adapter_for(source)
            if adapter is None or row['origin_key'] in getattr(adapter,'_active_sessions',{}):continue
            if row['origin_key'] in getattr(self.gateway,'_pending_approvals',{}):continue
            from tools.clarify_gateway import get_pending_for_session
            if get_pending_for_session(row['origin_key'],include_choice_prompts=True) is not None:continue
            identity,binding,text,directory=self.runtime.identity(row['workflow_id'])
            corrected=self.runtime.superseding_workflow(identity)
            if corrected:
                if 'version'not in json.loads(row['request']):self.runtime.requests.finish(row['id'],corrected)
                self.runtime.requests.wake(row['id'],'completed',now)
                continue
            saved=(directory/'analysis.json').exists()
            if saved:
                phases=self.runtime.active_phases(identity)
                self.runtime.requests.progress(row['id'],phases)
                row=self.runtime.requests.get(row['id'])
                if phases and all(s=='completed' for s in phases.values()):
                    self.runtime.requests.wake(row['id'],'completed',now);continue
            if row['wake_attempts']>=2:
                self.runtime.requests.review(row['id'],'continuation_did_not_finish');continue
            if row['wake_state']in ('accepted','foreground'):
                # A crash may lose an in-memory adapter queue. Allow one recovery
                # only after its session has stayed idle for ten minutes.
                if now-(row['wake_at']or now)<600:continue
            if row['wake_state']=='attempted' and now-(row['wake_at']or now)<120:continue
            self.runtime.requests.wake(row['id'],'attempted',now)
            event=dict(platform='telegram',chat_id=source.chat_id,user_id=source.user_id,
                       chat_type=source.chat_type,thread_id=source.thread_id,profile=source.profile,
                       session_key=row['origin_key'])
            instructions=(
                'The full analysis is already saved. Call gtm_call_finalize without analysis to finish only the remaining target phases. '
                'Do not re-read or re-analyse the transcript and do not replace any saved content. '
                if saved else
                'Read every original source page with gtm_call_source_read, including available mail_context. '
                'Use one thorough source-grounded analysis for archive, all applicable live CRM fields and Monolith review. '
                'Include the quality_review coverage contract from the first source page. '
                'Explicitly review needs, tools, scale, requirements, objections, commercial intent, permissions and both parties\' next steps. ')
            text=('Internal continuation of Alex\'s previously requested completed business call. '
                  'This is a system continuation, not a new message from Alex. '
                  'The exact transcript is now finalized. Continue only this request: '
                  'source_request_id='+row['id']+'; workflow_id='+row['workflow_id']+'. '
                  'Check gtm_call_status. '+instructions+
                  'Follow-up email drafts are currently not requested. '
                  'Respect any newer cancellation or correction in the conversation. Never send email '
                  'or create/change invitations. Report only verified results briefly in German.')
            try:accepted=await self.gateway._inject_watch_notification(text,event,raise_not_accepted=True)
            except Exception:accepted=False
            if accepted is True:self.runtime.requests.wake(row['id'],'accepted',now)
            else:self.runtime.requests.wake(row['id'],'waiting',now)

    async def run(self):
        while True:
            try:await self.tick()
            except asyncio.CancelledError:raise
            except Exception:pass  # Durable queue and per-phase receipts retain the work.
            await asyncio.sleep(30)

    async def shutdown(self,gateway,**_):
        if self.gateway is not gateway:return
        if self.task:
            self.task.cancel()
            try:await self.task
            except asyncio.CancelledError:pass
        self.task=None;self.gateway=None
