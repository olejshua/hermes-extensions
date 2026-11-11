"""A native gateway channel, never a separate agent or model runner."""
import asyncio
import inspect
from pathlib import Path
from sqlite3 import Error as ReceiptError
from uuid import UUID, uuid4

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent

from .tool_scope import restrict_task_tools, restore_task_tools
from .capabilities import capabilities, bind_options, restore_options
from .task_context import task_event_text, validate_task_reply
from . import valid_config
from .store import Receipts, fingerprint
from .transport import Backend, TransportError


def identity(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('monolith_identity_invalid')
    return value


class MonolithAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 100000
    supports_async_delivery = False
    supports_status_text = False
    supports_code_blocks = True
    interactive_resume = False

    def __init__(self, config, *, backend=None, state_root=None, automatic_poll=True):
        super().__init__(config, Platform('monolith'))
        if not valid_config(config):
            raise ValueError('monolith_owner_binding_required')
        self.owner = config.extra['owner_id']
        self.target = config.extra.get('only_turn_id')
        if self.target is not None:
            identity(self.target)
        self.backend = backend
        self.state_root = state_root
        self.automatic_poll = automatic_poll
        self.receipts = None
        self._poll_task = None
        self._live = set()
        self._native_tasks = {}
        self._option_restores = {}
        self._tool_restores = {}
        self._admission_event = None
        self._poll_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()

    async def connect(self, *, is_reconnect=False):
        if self._running:
            return True
        if self._live:
            return False  # The old native task still owns this adapter's receipt lock.
        try:
            # This marker is already supplied by the installed reviewed dialogue overlay.
            # Older SDKs must not mistake a progress/status send for a final answer.
            if '_gateway_text_role' not in inspect.getsource(BasePlatformAdapter):
                raise ValueError('monolith_native_final_marker_missing')
            if self._message_handler is None:
                raise ValueError('monolith_native_handler_missing')
            self.backend = self.backend or Backend.native()
            binding = await self.backend.call('/agent/chat/worker')
            if not isinstance(binding, dict) or binding.get('protocol') != 'chat-v2' or binding.get('agentKind') != 'hermes' or binding.get('userId') != self.owner:
                raise ValueError('monolith_worker_binding_mismatch')
            self.worker_token_id = identity(binding.get('tokenId'))
            if self.state_root is None:
                from hermes_constants import get_hermes_home
                self.state_root = Path(get_hermes_home()) / 'ole-monolith-chat' / self.owner
            self.receipts = Receipts(self.state_root)
            self._running = True
            self._mark_connected()
            if self.automatic_poll:
                self._poll_task = asyncio.create_task(self._poll())
            return True
        except (ValueError, OSError, ReceiptError, TransportError):
            self._fatal_error_code = 'monolith_binding_or_private_state_unavailable'
            self._mark_disconnected()
            if self.receipts is not None:
                self.receipts.close(); self.receipts = None
            return False

    async def disconnect(self):
        self._running = False
        self._mark_disconnected()
        if self._poll_task is not None:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
            self._poll_task = None
        # An old native turn still owns its receipt lock until its final send/cleanup.
        self._close_if_idle()

    async def get_chat_info(self, chat_id):
        prefix = self.owner + ':'
        if not isinstance(chat_id, str) or not chat_id.startswith(prefix):
            raise ValueError('monolith_chat_owner_mismatch')
        identity(chat_id[len(prefix):])
        return {'name': 'Monolith', 'type': 'dm'}

    async def handle_message(self, event):
        # The native gateway may synthesize startup resume/wake events. Only an
        # exact event freshly admitted by our durable claim may invoke its handler.
        # Unknown interrupted work remains blocked in the receipt journal.
        if event is not self._admission_event:
            return
        return await super().handle_message(event)

    def _close_if_idle(self):
        if not self._running and not self._live and self.receipts is not None:
            self.receipts.close()

    async def _poll(self):
        while self._running:
            try:
                await self.poll_once()
            except (TransportError, ValueError, OSError, ReceiptError):
                # No content, credential, backend body or prompt enters observability.
                self._fatal_error_code = 'monolith_poll_unavailable'
            await asyncio.sleep(20)

    def _save(self, row, phase, **fields):
        row.update(fields, phase=phase)
        self.receipts.save(row)

    def _validate_claim(self, claim, row):
        if not isinstance(claim, dict) or claim.get('userId') != self.owner or claim.get('agentKind') != 'hermes' or claim.get('claimId') != row['claim_id']:
            raise ValueError('monolith_claim_binding_mismatch')
        identity(claim.get('id')); identity(claim.get('conversation_id'))
        if not isinstance(claim.get('prompt'), str) or not 0 < len(claim['prompt']) <= 12000:
            raise ValueError('monolith_claim_prompt_invalid')
        return claim

    async def _claim(self, row):
        catalog=await asyncio.to_thread(capabilities)
        body = {'taskReplyProtocol':'task-reply-v1','protocol': 'chat-v3' if catalog else 'chat-v2', 'agentKind': 'hermes', 'claimId': row['claim_id']}
        if catalog: body['capabilities']=catalog
        if self.target:
            body['turnId'] = self.target
        claim = await self.backend.call('/agent/chat/claim', body)
        if claim is None:
            self.receipts.forget_empty_claim(row['id']); return
        claim = self._validate_claim(claim, row)
        if claim['status'] != 'running' or claim.get('leaseExpired'):
            self._save(row, 'blocked', reason='claim_not_running'); return
        chat = self.owner + ':' + claim['conversation_id']
        self._save(row, 'running', turn_id=claim['id'], conversation_id=claim['conversation_id'],
                   chat_id=chat, prompt_hash=fingerprint(claim['prompt']), task_context=bool(claim.get('taskContext')))
        anchor = 'monolith-' + row['id']
        source = self.build_source(chat, user_id=self.owner, message_id=anchor)
        event = MessageEvent(text=task_event_text(claim), source=source, user_id=self.owner,
                             message_id=anchor, allow_gateway_control=False)
        try:
            skey=self._event_session_key(event)
            self._option_restores[row['id']]=(skey,bind_options(self.gateway_runner,skey,claim.get('execution_options'),catalog))
            if claim.get('taskContext') is not None:
                self._tool_restores[row['id']]=restrict_task_tools(self.gateway_runner,event.source)
        except (ValueError,AttributeError):
            self._restore_turn_options(row['id'])
            await self.backend.call('/agent/chat/turns/'+row['turn_id']+'/fail',{'claimId':row['claim_id'],'error':'Hermes kann die gewählte Modell-/Reasoningkombination nicht ausführen. Kein Ersatzmodell gestartet.'})
            self._save(row,'delivered');return
        self._live.add(row['id'])
        try:
            self._admission_event = event
            await self.handle_message(event)
        except BaseException:
            self._restore_turn_options(row['id'])
            self._live.discard(row['id'])
            self._save(row, 'blocked', reason='native_admission_unknown')
            raise
        finally:
            self._admission_event = None
        if not event._gateway_accepted:
            self._restore_turn_options(row['id'])
            self._live.discard(row['id'])
            self._save(row, 'blocked', reason='native_admission_unconfirmed')
            return
        task = self._session_tasks.get(self._event_session_key(event))
        if isinstance(task, asyncio.Task):
            self._native_tasks[row['id']] = task
            task.add_done_callback(lambda _: self._native_finished(row['id']))
        else:
            self._live.discard(row['id'])
            self._restore_turn_options(row['id'])
            self._save(row, 'blocked', reason='native_task_unconfirmed')

    def _restore_turn_options(self, record_id):
        tool_key=self._tool_restores.pop(record_id,None)
        if tool_key is not None:restore_task_tools(self.gateway_runner,tool_key)
        binding=self._option_restores.pop(record_id,None)
        if binding is not None: restore_options(self.gateway_runner,*binding)

    def _native_finished(self, record_id):
        self._restore_turn_options(record_id)
        self._live.discard(record_id)
        self._native_tasks.pop(record_id, None)
        row = self.receipts.get(record_id)
        if row and row['phase'] == 'running':
            self._save(row, 'blocked', reason='native_result_unknown')
        self._close_if_idle()

    async def poll_once(self):
        async with self._poll_lock:
            if getattr(self.gateway_runner, '_startup_restore_in_progress', False):
                return
            pending = self.receipts.pending()
            for row in pending:
                if row['owner'] != self.owner:
                    raise ValueError('monolith_receipt_owner_mismatch')
                if row['phase'] == 'ready':
                    await self._deliver(row)
                elif row['phase'] == 'running':
                    if row['id'] not in self._live:
                        self._save(row, 'blocked', reason='native_execution_unknown_after_restart')
                    else:
                        try:
                            await self.backend.call('/agent/chat/turns/' + row['turn_id'] + '/heartbeat', {'claimId': row['claim_id']})
                        except TransportError as error:
                            if error.status in (403, 409):
                                # Final delivery can commit while this heartbeat is in flight.
                                # Only the still-current running generation may be blocked.
                                current = self.receipts.get(row['id'])
                                if current and current['phase'] == 'running' and current['claim_id'] == row['claim_id']:
                                    self._save(current, 'blocked', reason='claim_no_longer_owned')
                elif row['phase'] == 'claiming':
                    # Same persisted request identity recovers an unknown claim acknowledgement.
                    try:
                        await self._claim(row)
                    except TransportError as error:
                        if error.status in (403, 409):
                            self._save(row, 'blocked', reason='claim_no_longer_owned')
                        else:
                            raise
            if self.receipts.pending():
                if any(row['phase'] == 'blocked' for row in self.receipts.pending()):
                    self._mark_degraded()
                return  # Single model lane; uncertain old work never starts a new inference.
            if self._live:
                return  # Let the admitted native task finish its delivery/cleanup first.
            self._mark_connected()
            claim_id = str(uuid4())
            row = {'id': claim_id, 'claim_id': claim_id, 'owner': self.owner, 'phase': 'claiming'}
            self.receipts.save(row)
            await self._claim(row)

    async def _deliver(self, row):
        async with self._delivery_lock:
            row = self.receipts.get(row['id'])
            if row['phase'] == 'delivered':
                return True
            current = await self.backend.call('/agent/chat/turns/' + row['turn_id'] + '/receipt')
            if current and row.get('recovery_claim_id') and current.get('claimId') == row['recovery_claim_id'] and current.get('claimedByThisWorker') is True:
                self._save(row, 'ready', claim_id=row['recovery_claim_id'], recovery_claim_id=None)
            if not current or current.get('userId') != self.owner or current.get('agentKind') != 'hermes' or current.get('claimedByThisWorker') is not True or current.get('claimId') != row['claim_id'] or fingerprint(current.get('prompt', '')) != row['prompt_hash']:
                self._save(row, 'blocked', reason='receipt_claim_changed'); return False
            outcome=row.get('outcome','completed')
            field='response' if outcome=='completed' else 'error'
            if current['status'] == outcome:
                if current.get(field) != row['response']:
                    self._save(row, 'blocked', reason='receipt_answer_conflict'); return False
                self._save(row, 'delivered'); return True
            if current['status'] != 'running':
                self._save(row, 'blocked', reason='receipt_not_running'); return False
            if current.get('leaseExpired'):
                recovery = row.get('recovery_claim_id') or str(uuid4())
                self._save(row, 'ready', recovery_claim_id=recovery)
                catalog=await asyncio.to_thread(capabilities)
                claim = await self.backend.call('/agent/chat/claim', {'taskReplyProtocol':'task-reply-v1',**({'capabilities':catalog} if catalog else {}), 'protocol': 'chat-v3' if catalog else 'chat-v2', 'agentKind': 'hermes',
                    'turnId': row['turn_id'], 'previousClaimId': row['claim_id'], 'claimId': recovery})
                if not claim or claim.get('claimId') != recovery or claim.get('id') != row['turn_id'] or claim.get('userId') != self.owner:
                    self._save(row, 'blocked', reason='delivery_recovery_conflict'); return False
                self._save(row, 'ready', claim_id=recovery, recovery_claim_id=None)
            else:
                await self.backend.call('/agent/chat/turns/' + row['turn_id'] + '/heartbeat', {'claimId': row['claim_id']})
            route='complete' if outcome=='completed' else 'fail'
            await self.backend.call('/agent/chat/turns/' + row['turn_id'] + '/' + route,
                                    {'claimId': row['claim_id'], field: row['response']})
            self._save(row, 'delivered')
            return True

    def _stage_answer(self, chat_id, content, reply_to):
        # Hono canonicalizes answers with trim(); persist that same representation.
        if not isinstance(content, str) or not content.strip() or len(content) > 100000:
            raise ValueError('monolith_final_invalid')
        content = content.strip()
        if not isinstance(reply_to, str) or not reply_to.startswith('monolith-'):
            raise ValueError('monolith_final_anchor_missing')
        row = self.receipts.get(reply_to[len('monolith-'):])
        if not row or row.get('chat_id') != str(chat_id) or row['owner'] != self.owner:
            raise ValueError('monolith_final_binding_mismatch')
        if row['phase'] not in ('running', 'ready', 'blocked', 'delivered'):
            raise ValueError('monolith_final_invalid')
        if row.get('task_context'):
            try: validate_task_reply(content)
            except (ValueError,TypeError):
                content='Hermes konnte keinen gültigen Taskvorschlag liefern. Es wurde nichts geändert.'
                row['outcome']='failed'
        if 'response' in row and row['response'] != content:
            raise ValueError('monolith_final_changed')
        if row['phase'] != 'delivered':
            self._save(row, 'ready', response=content)
        return row

    async def send_final_ledgered(self, event, session_key, text_content, metadata, *, reply_to, is_ephemeral_response=False):
        # Persist at the native final-text boundary BEFORE the gateway's generic
        # delivery ledger. A crash between that ledger and send() must still leave
        # our exact claim/anchor/answer receipt available for model-free repair.
        try:
            if metadata.get('_gateway_text_role') != 'answer':
                raise ValueError('monolith_final_proof_missing')
            self._stage_answer(event.source.chat_id, text_content, reply_to)
        except (ValueError, OSError, ReceiptError):
            return SendResult(success=False, error='monolith_delivery_unconfirmed'), self
        return await super().send_final_ledgered(event, session_key, text_content, metadata,
                    reply_to=reply_to, is_ephemeral_response=is_ephemeral_response)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        metadata = metadata or {}
        if metadata.get('_interim_send') is True or metadata.get('_gateway_text_role') != 'answer':
            return SendResult(success=True, raw_response={'skipped': True})
        try:
            row = self._stage_answer(chat_id, content, reply_to)
            if row['phase'] == 'delivered':
                return SendResult(success=True, message_id=row['turn_id'])
            delivered = await self._deliver(row)
            return SendResult(success=delivered, message_id=row['turn_id'] if delivered else None,
                              error=None if delivered else 'monolith_delivery_unconfirmed')
        except (TransportError, ValueError, OSError, ReceiptError):
            return SendResult(success=False, error='monolith_delivery_unconfirmed')
