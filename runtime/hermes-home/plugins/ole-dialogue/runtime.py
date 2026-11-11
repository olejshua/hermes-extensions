"""Native control/receipt adapter. Worker execution is owned by background_jobs."""
from __future__ import annotations
import asyncio
import json
import time
from types import SimpleNamespace
import threading

from .policy import accepted_text, control, status_text
from .store import Store
from .result_export import export_result
from .steering import SteeringBarrier

TERMINAL = {'completed','completed_with_blockers','cancelled','failed','revision_blocked'}

class BindingPolicy:
    def __init__(self, user_id, chat_id, profile=None):
        self.user_id, self.chat_id = str(user_id or ''), str(chat_id or '')
        self.profile = profile or None
        if not self.user_id or not self.chat_id:
            raise ValueError('verified_private_route_required')

    def bound(self, source):
        platform = getattr(getattr(source,'platform',None),'value',None)
        return (platform == 'telegram' and str(getattr(source,'user_id','')) == self.user_id
                and str(getattr(source,'chat_id','')) == self.chat_id
                and getattr(source,'chat_type',None) in ('dm','private')
                and (getattr(source,'profile',None) or None) == self.profile
                and getattr(source,'is_bot',False) is not True
                and getattr(source,'profile_route_rejected',False) is not True)

    @property
    def owner(self):
        return 'telegram:' + self.user_id

class Dialogue:
    def __init__(self, root, policy, backend):
        self.store, self.policy, self.backend = Store(root), policy, backend
        self.gateway = self.loop = self.poller = None
        self.sources = {}
        self.active = {}
        self.active_lock = threading.RLock()
        self.result_failures = {}
        self.steering = SteeringBarrier(policy)

    def unload(self):
        # Native unload may run off the gateway loop; cancel only this plugin's poller.
        if self.loop is not None and self.poller is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.poller.cancel)

    async def ready(self, gateway, **_):
        # No target is accepted from a caller; every restored source passes current native auth.
        if self.gateway is gateway and self.poller is not None and not self.poller.done():
            return
        if self.poller is not None:
            self.poller.cancel()
        self.gateway, self.loop = gateway, asyncio.get_running_loop()
        for session, owner, data in self.store.origins():
            try:
                from gateway.session import SessionSource
                from gateway.config import Platform
                data = dict(data)
                transport = data.pop('transport_profile',None)
                data['platform'] = Platform(data['platform'])
                source = gateway._restored_source(SimpleNamespace(origin=SessionSource(**data), transport_profile=transport))
                if owner == self.policy.owner and self.policy.bound(source) and gateway._is_user_authorized_for_source(source):
                    if gateway._session_key_for_source(source) == session:
                        self.sources[session] = source
            except Exception:
                continue
        self.poller = asyncio.create_task(self._poll(), name='ole-dialogue-receipts')

    async def shutdown(self, gateway, **_):
        if self.gateway is not gateway:
            return
        if self.poller is not None:
            self.poller.cancel()
            try:
                await self.poller
            except asyncio.CancelledError:
                pass
        self.gateway = self.loop = self.poller = None

    async def admitted(self, event, gateway, session_key, busy=False, turn_task=None, **_):
        source = event.source
        if getattr(event,'internal',False) or not self.policy.bound(source):
            return None
        if not gateway._is_user_authorized_for_source(source):
            return None
        if control(event.text) is None:
            self.steering.observe(event, gateway, session_key, busy=busy)
        if self.gateway is not gateway:
            await self.ready(gateway)
        self.sources[session_key] = source
        from gateway.session_identity import transport_profile_of
        fields = ('chat_id','user_id','chat_type','thread_id','profile','scope_id','message_id')
        origin = {k:getattr(source,k,None) for k in fields}
        origin['message_id']=event.message_id
        origin['platform']='telegram'
        origin['transport_profile']=transport_profile_of(source)
        now=time.monotonic()
        received=getattr(event,'_gateway_received_monotonic',None)
        queued=getattr(event,'_gateway_queued_monotonic',None)
        self.store.admission(session_key,self.policy.owner,origin,event.message_id,
            elapsed_ms=(now-received)*1000 if received is not None else None,
            queue_wait_ms=(now-queued)*1000 if queued is not None else None,
            new_inbound=not getattr(event,'_ole_dialogue_inbound_recorded',False),start_turn=not busy)
        event._ole_dialogue_inbound_recorded=True
        if not busy:
            adapter=gateway._delivery_adapter_for(source)
            with self.active_lock:
                self.active[session_key]={'message':event.message_id,'received':received,'started':now,'visible':False,'answer':False,
                    'turn_task':turn_task,'adapter':adapter,
                    'tracked':adapter is not None and getattr(adapter,'_session_tasks',{}).get(session_key) is turn_task and turn_task is not None,
                    'guard':getattr(adapter,'_active_sessions',{}).get(session_key) if adapter is not None else None}
        decision=control(event.text)
        if decision is None:
            if not busy:
                await asyncio.to_thread(self.backend.foreground_started,self.policy.owner,session_key)
                with self.active_lock:self.active[session_key]['priority']=True
            return None
        action, job_id = decision
        if job_id:
            try:
                rows=[await asyncio.to_thread(self.backend.status,job_id,self.policy.owner,session_key)]
            except ValueError:
                rows=[]
        else:
            rows = await asyncio.to_thread(self.backend.list_owned,self.policy.owner,session_key)
            if isinstance(rows,dict):rows=rows.get('jobs',[])
            active=[r for r in rows if r.get('state') not in TERMINAL]
            rows=active or rows[:1]
        if not rows:
            if job_id or event.text.strip().startswith('/job '):
                return {'action':'reply','text':'Für diesen Chat finde ich keinen passenden Hintergrundauftrag.'}
            if not busy:
                await asyncio.to_thread(self.backend.foreground_started,self.policy.owner,session_key)
                with self.active_lock:self.active[session_key]['priority']=True
            return None  # A natural question without an owned job belongs to the normal conversation.
        if len(rows) != 1:
            choices=', '.join(r['job_id'] for r in rows[:4])
            return {'action':'reply','text':f'Welchen Auftrag meinst du? {choices}.'}
        row=rows[0]
        if action != 'status':
            row = await asyncio.to_thread(getattr(self.backend,action),row['job_id'],self.policy.owner,session_key)
        row=dict(row)
        row['result_delivery_failed']=self.result_failures.get(row['job_id']) == (row.get('scope_hash'),row.get('revision'))
        return {'action':'reply','text':status_text(row)}

    def delivered(self, **payload):
        if payload.get('platform') != 'telegram' or str(payload.get('chat_id')) != self.policy.chat_id:
            return
        if payload.get('success') is not True or payload.get('visible') is not True or payload.get('job_final') is True:
            return
        # Exact reply anchor when available; a draft belongs only to the one admitted active lane.
        candidates=[]
        with self.active_lock:
            for session, active in self.active.items():
                source=self.sources.get(session)
                if source is None or str(getattr(source,'thread_id',None) or '') != str(payload.get('thread_id') or ''):
                    continue
                anchor=payload.get('reply_to')
                if anchor is not None and str(anchor) != str(active['message']):
                    continue
                candidates.append((session,active))
            if len(candidates) != 1:
                return  # Ambiguous/outbox visibility must not be attributed to another turn.
            session,active=candidates[0]
            received=active['received']
            elapsed=((payload.get('at_monotonic') or time.monotonic())-received)*1000 if received is not None else None
            if not active['visible']:
                active['visible']=True
                self.store.metric('first_visible',session,active['message'],elapsed_ms=elapsed)
            # Only positive native final-text provenance is answer proof. Unmarked messages
            # and drafts may be notices; never infer an answer from time, content or non-interim.
            if payload.get('answer') is True and payload.get('interim') is not True and not active.get('answer'):
                active['answer']=True
                self.store.metric('answer_accepted',session,active['message'],elapsed_ms=elapsed)
                if active.get('handoff_job'):
                    self.store.settled(active['handoff_job'],'ack',success=True)
                    self.store.metric('ack_visible',session,active['message'],job=active['handoff_job'],elapsed_ms=elapsed)

    def _finish_locked(self, session, active, *, reconciled=False):
        if self.active.get(session) is not active or active.get('finished'):
            return
        # Stop renewal before any fallible DB/metrics operation. Failure retains only the
        # original finite lease; it can never produce a permanent heartbeat after turn end.
        active['finished']=time.monotonic()
        try:
            if active.get('priority'):
                self.backend.foreground_finished(self.policy.owner,session)
        finally:
            self.store.metric('turn_reconciled' if reconciled else 'turn_finished',session,active['message'],
                elapsed_ms=None if reconciled or active['received'] is None else (time.monotonic()-active['received'])*1000)

    def finished(self, event, turn_task=None, **_):
        with self.active_lock:
            matches=[(s,a) for s,a in self.active.items() if a['message'] == event.message_id
                     and (a.get('turn_task') is turn_task)]
            for session, active in matches:
                self._finish_locked(session,active)

    def _native_liveness(self, session, active):
        task=active.get('turn_task')
        if task is None or not callable(getattr(task,'done',None)):
            return None
        if task.done():
            return False
        if active.get('tracked'):
            adapter=active.get('adapter')
            if (getattr(adapter,'_session_tasks',{}).get(session) is not task
                    or getattr(adapter,'_active_sessions',{}).get(session) is not active.get('guard')):
                return False
        return True

    def _renew_or_reconcile(self, session, expected):
        with self.active_lock:
            if self.active.get(session) is not expected or expected.get('finished'):
                return
            alive=self._native_liveness(session,expected)
            if alive is False:
                self._finish_locked(session,expected,reconciled=True)
            elif alive is True:
                self.backend.foreground_started(self.policy.owner,session)
            # Unknown native liveness never renews indefinitely or invents an end timestamp.

    def _delivery_idle(self, session, adapter, job_id):
        # The native adapter owns active and queued conversation lanes.
        if session in getattr(adapter,'_active_sessions',{}):
            return False
        if session in getattr(adapter,'_pending_messages',{}):
            return False
        with self.active_lock:
            active=self.active.get(session)
            if active and not active.get('finished'):
                return False
            if active and self._native_liveness(session,active) is True:
                return False
        # Pre-existing jobs have no ack record. Newly handed-off jobs require
        # positive native answer provenance, never a timer or inferred visibility.
        return self.store.phase(job_id,'ack') in (None,'delivered')

    async def _send_final(self, row, session, source):
        gateway=self.gateway
        if (gateway is None or not self.policy.bound(source)
                or row.get('owner') != self.policy.owner or row.get('session_id') != session
                or not gateway._is_user_authorized_for_source(source)
                or gateway._session_key_for_source(source) != session):
            return
        adapter=gateway._delivery_adapter_for(source)
        if (adapter is None or self.store.phase(row['job_id'],'final') is not None
                or not self._delivery_idle(session,adapter,row['job_id'])):
            return
        document=None
        if row.get('state') in ('completed','completed_with_blockers') and row.get('profile') == 'readonly-research':
            version=(row.get('scope_hash'),row.get('revision'))
            if self.result_failures.get(row['job_id']) == version:
                return  # No repeated full export of the same known corrupt/unsupported result.
            if not callable(getattr(adapter,'send_document',None)):
                return
            try:
                document=await asyncio.to_thread(export_result,self.store.root,self.backend,row,self.policy.owner,session)
            except (ValueError,OSError):
                self.result_failures[row['job_id']]=version
                raise
        # Export can yield while a foreground message is admitted. Revalidate
        # both authorization and the native lanes before reserving any send.
        if (self.gateway is not gateway or not self.policy.bound(source)
                or not gateway._is_user_authorized_for_source(source)
                or gateway._session_key_for_source(source) != session
                or gateway._delivery_adapter_for(source) is not adapter
                or not self._delivery_idle(session,adapter,row['job_id'])):
            return
        if not self.store.reserve(row['job_id'],'final',session,self.policy.owner):
            return
        metadata=gateway._thread_metadata_for_source(source, getattr(source,'message_id',None))
        metadata={**(metadata or {}),'_interim_send':True,'_ole_dialogue_job_final':True}
        try:
            if document is not None:
                result=await adapter.send_document(source.chat_id,str(document),caption=status_text(row),file_name='Hintergrund-Ergebnisse.md',
                    reply_to=getattr(source,'message_id',None),metadata=metadata)
            else:
                result=await adapter.send(source.chat_id,status_text(row),reply_to=getattr(source,'message_id',None),metadata=metadata)
            raw=getattr(result,'raw_response',None)
            success=(getattr(result,'success',False) is True and bool(getattr(result,'message_id',None))
                     and not (isinstance(raw,dict) and raw.get('skipped')))
            # retryable is not proof of non-delivery. Every attempted send without an exact
            # native message receipt remains unknown, including retryable timeouts.
            self.store.settled(row['job_id'],'final',success=success)
            self.store.metric('job_final_visible' if success else 'delivery_unknown',session,job=row['job_id'])
        except BaseException:
            # A timeout/shutdown after API acceptance has no safe blind retry.
            self.store.settled(row['job_id'],'final',success=False)
            raise

    async def poll_once(self):
        renew=[]
        with self.active_lock:
            for session,active in list(self.active.items()):
                if active.get('finished') and time.monotonic()-active['finished']>2:
                    self.active.pop(session,None)
                elif active.get('priority') and not active.get('finished'):
                    renew.append((session,active))
        for session,active in renew:
            try:
                await asyncio.to_thread(self._renew_or_reconcile,session,active)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue  # A failed lease DB operation cannot terminate the receipt poller.
        for session,source in list(self.sources.items()):
            if self.gateway is None:
                return
            try:
                rows=await asyncio.to_thread(self.backend.list_owned,self.policy.owner,session)
                if isinstance(rows,dict):rows=rows.get('jobs',[])
                for row in rows:
                    if row.get('state') in TERMINAL:
                        await self._send_final(row,session,source)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def _poll(self):
        while True:
            await self.poll_once()
            await asyncio.sleep(2)

    def handoff(self, tool_calls, messages, **_):
        if len(tool_calls) != 1 or tool_calls[0].function.name != 'background_job_submit':
            return None
        from gateway.session_context import get_session_env
        session=get_session_env('HERMES_SESSION_KEY','')
        if (get_session_env('HERMES_SESSION_PLATFORM','') != 'telegram'
                or get_session_env('HERMES_SESSION_CHAT_TYPE','') not in ('dm','private')
                or str(get_session_env('HERMES_SESSION_USER_ID','')) != self.policy.user_id
                or (get_session_env('HERMES_SESSION_PROFILE','') or None) != self.policy.profile):
            return None
        source=self.sources.get(session)
        if source is None or not self.policy.bound(source) or self.gateway is None:
            return None
        if not self.gateway._is_user_authorized_for_source(source):
            return None
        tail=messages[-1]
        if tail.get('role') != 'tool' or tail.get('tool_call_id') != tool_calls[0].id:
            return None
        try:
            receipt=json.loads(tail.get('content',''))
            if receipt.get('accepted') is not True:
                return None
            row=self.backend.status(receipt['job_id'],self.policy.owner,session)
            proof=self.backend.accepted_proof(receipt['job_id'],self.policy.owner,session)
            # The service confirms the same persisted submission/version, never an arbitrary tool phrase.
            if (row.get('state') not in ('queued','running') or row.get('cancel_requested')
                    or row.get('revision',-1) < receipt.get('revision',0)
                    or not receipt.get('scope_hash') or row.get('scope_hash') != receipt.get('scope_hash')
                    or not receipt.get('submission_ref') or row.get('submission_ref') != receipt.get('submission_ref')):
                return None
            if (proof.get('accepted') is not True or proof.get('scope_hash') != receipt['scope_hash']
                    or proof.get('submission_ref') != receipt['submission_ref']
                    or proof.get('owner') != self.policy.owner or proof.get('session_id') != session):
                return None
            # Recheck state after the proof read; a concurrently requested cancellation wins.
            latest=self.backend.status(receipt['job_id'],self.policy.owner,session)
            if (latest.get('state') not in ('queued','running') or latest.get('cancel_requested')
                    or latest.get('scope_hash') != proof['scope_hash']
                    or latest.get('submission_ref') != proof['submission_ref']):
                return None
            self.store.reserve(receipt['job_id'],'ack',session,self.policy.owner)
            with self.active_lock:
                if session in self.active:self.active[session]['handoff_job']=receipt['job_id']
            from agent.plugin_turn_handoff import TurnHandoff
            return TurnHandoff(accepted_text(row),tool_name='background_job_submit')
        except Exception:
            return None
