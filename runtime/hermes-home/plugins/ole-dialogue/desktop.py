"""Native Desktop job controls and receipts, separate from Messaging adapters."""
import asyncio
import contextvars
import json
import threading
import time
from pathlib import Path

from .policy import accepted_text, control, status_text
from .runtime import TERMINAL
from .store import Store


class DesktopDialogue:
    def __init__(self, root, backend, ctx):
        self.store = Store(Path(root) / 'desktop')
        self.backend, self.ctx = backend, ctx
        self.active, self.loop, self.task = {}, None, None
        self.lock = threading.RLock()
        self.closed = False
        with self.store.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS desktop_origins(session TEXT PRIMARY KEY, owner TEXT, descriptor TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS desktop_jobs(job TEXT PRIMARY KEY, session TEXT, owner TEXT, descriptor TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS desktop_controls(session TEXT, message TEXT, action TEXT, job TEXT, state TEXT, PRIMARY KEY(session,message,action,job))')

    def state_action(self,identity,action,job,deadline=None):
        self._identity(identity.frame)
        if action not in ('cancel','resume'):
            raise ValueError('native_desktop_control_action_required')
        key=(identity.session,identity.frame.message_id,action,job)
        with self.store.connection() as db:
            db.execute('INSERT OR IGNORE INTO desktop_controls VALUES(?,?,?,?,?)',(*key,'attempting'))
            fresh=db.execute('SELECT changes()').fetchone()[0]==1
        if not fresh:
            # A native hook timeout can leave an action in flight. Same-turn tool
            # fallback may read state, but never blindly repeat that mutation.
            return self.backend.status(job,identity.owner,identity.session)
        try:
            # Reservation and native binding reads can outlive the admission
            # callback. Recheck after those blocking steps, before starting a
            # mutation; an already reserved turn only permits later readback.
            self._identity(identity.frame)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('desktop_dialogue_deadline')
            row=getattr(self.backend,action)(job,identity.owner,identity.session)
            state='settled'
        except BaseException:
            with self.store.connection() as db:
                db.execute('UPDATE desktop_controls SET state=? WHERE session=? AND message=? AND action=? AND job=?',('unknown',*key))
            raise
        with self.store.connection() as db:
            db.execute('UPDATE desktop_controls SET state=? WHERE session=? AND message=? AND action=? AND job=?',(state,*key))
        return row

    def accepted_job(self, identity, row):
        self._identity(identity.frame)
        if row.get('owner') != identity.owner or row.get('session_id') != identity.session or row.get('accepted') is not True:
            return False
        descriptor = json.dumps(identity.descriptor(),sort_keys=True)
        with self.store.connection() as db:
            db.execute('INSERT OR IGNORE INTO desktop_jobs VALUES(?,?,?,?)',
                       (row['job_id'],identity.session,identity.owner,descriptor))
            existing = db.execute('SELECT session,owner,descriptor FROM desktop_jobs WHERE job=?',(row['job_id'],)).fetchone()
        return existing == (identity.session,identity.owner,descriptor)

    def delivery_binding(self,descriptor,row):
        binding,owner,session=self.backend.desktop_delivery_binding(descriptor)
        if row.get('owner')!=owner or row.get('session_id')!=session:
            raise ValueError('native_private_session_required')
        return binding,owner,session

    def _identity(self, frame):
        identity = self.backend.desktop_identity(frame.agent, frame.session_id)
        if identity.frame is not frame:
            raise ValueError('native_private_session_required')
        return identity

    def _priority(self,identity,frame):
        key=identity.session+':'+frame.message_id
        with self.lock:
            active=self.active.get(key)
            if (active is None or active['frame'] is not frame or active['finished']
                    or frame.record.get('_hermes_desktop_dialogue_frame') is not frame):
                return
            self._identity(frame)
            active['priority']=True
            self.backend.foreground_started(identity.owner,key)

    def admitted_native(self, frame, text, deadline=None, **_):
        def timely():
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('desktop_dialogue_deadline')
        timely()
        identity = self._identity(frame)
        descriptor = identity.descriptor()
        with self.store.connection() as db:
            db.execute('INSERT OR REPLACE INTO desktop_origins VALUES(?,?,?)',
                       (identity.session, identity.owner, json.dumps(descriptor)))
        with self.lock:
            self.active[identity.session+':'+frame.message_id] = {'identity': identity, 'frame': frame, 'finished': False, 'priority': False}
        if self not in frame.handlers:
            frame.handlers.append(self)
        self._start(frame.envelope.transport)
        decision = control(text)
        if decision is None:
            self._priority(identity,frame)
            return None
        action, job = decision
        try:
            rows = [self.backend.status(job, identity.owner, identity.session)] if job else self.backend.list_owned(identity.owner, identity.session)
        except ValueError:
            rows = []
        if isinstance(rows, dict):
            rows = rows.get('jobs', [])
        if not job:
            active = [r for r in rows if r.get('state') not in TERMINAL]
            rows = active or rows[:1]
        timely()
        if not rows:
            if job or text.strip().startswith('/job '):
                return 'Für diesen Chat finde ich keinen passenden Hintergrundauftrag.'
            self._priority(identity,frame)
            return None
        if len(rows) != 1:
            return 'Welchen Auftrag meinst du? ' + ', '.join(r['job_id'] for r in rows[:4]) + '.'
        row = rows[0]
        if action != 'status':
            timely()
            self._identity(frame)
            row = self.state_action(identity,action,row['job_id'],deadline=deadline)
        return status_text(row)

    def finished_native(self, frame):
        # Exact object generation: an old finally cannot release a newer turn.
        with self.lock:
            for session, active in list(self.active.items()):
                if active['frame'] is frame and not active['finished']:
                    active['finished'] = True
                    try:
                        if active['priority']:
                            self.backend.foreground_finished(active['identity'].owner, session)
                    finally:
                        if self.active.get(session) is active:
                            self.active.pop(session,None)

    def _start(self, transport):
        loop = getattr(transport, '_loop', None)
        if self.closed or loop is None or loop.is_closed():
            return
        self.loop = loop
        from tui_gateway.plugin_continuation import CURRENT
        from tui_gateway import server
        from tui_gateway.transport import bind_transport
        context = contextvars.copy_context()
        context.run(CURRENT.set, None)
        context.run(server._current_runtime_session_record.set, None)
        context.run(bind_transport, None)
        def start():
            if not self.closed and (self.task is None or self.task.done()):
                self.task = self.ctx.spawn_task(self._poll(), name='ole-dialogue-desktop-receipts')
        loop.call_soon_threadsafe(start, context=context)

    def desktop_connected(self, transport):
        # Liveness only. Reconciliation still requires the exact current binding/session.
        if self.backend.desktop_transport_allowed(transport):
            self._start(transport)

    def unload(self):
        self.closed = True
        if self.loop is not None and self.task is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.task.cancel)

    def handoff(self, agent, tool_calls, messages, **_):
        if len(tool_calls) != 1 or tool_calls[0].function.name != 'background_job_submit' or not messages:
            return None
        try:
            identity = self.backend.desktop_identity(agent, agent.session_id)
            tail = messages[-1]
            if tail.get('role') != 'tool' or tail.get('tool_call_id') != tool_calls[0].id:
                return None
            receipt = json.loads(tail.get('content', ''))
            if receipt.get('accepted') is not True or receipt.get('native_dialogue_receipt_saved') is not True:
                return None
            row = self.backend.status(receipt['job_id'], identity.owner, identity.session)
            proof = self.backend.accepted_proof(receipt['job_id'], identity.owner, identity.session)
            if (row.get('state') not in ('queued', 'running') or row.get('cancel_requested')
                or row.get('revision', -1) < receipt.get('revision', 0)
                or not receipt.get('scope_hash') or row.get('scope_hash') != receipt['scope_hash']
                or not receipt.get('submission_ref') or row.get('submission_ref') != receipt['submission_ref']
                or proof.get('accepted') is not True or proof.get('scope_hash') != receipt['scope_hash']
                or proof.get('submission_ref') != receipt['submission_ref']
                or proof.get('owner') != identity.owner or proof.get('session_id') != identity.session):
                return None
            latest = self.backend.status(receipt['job_id'], identity.owner, identity.session)
            if (latest.get('state') not in ('queued', 'running') or latest.get('cancel_requested')
                or latest.get('scope_hash') != proof['scope_hash'] or latest.get('submission_ref') != proof['submission_ref']):
                return None
            self._identity(identity.frame)  # Owner/lease/generation must still be current after DB reads.
            from agent.plugin_turn_handoff import TurnHandoff
            return TurnHandoff(accepted_text(row), tool_name='background_job_submit')
        except Exception:
            return None

    def _renew(self, session, active):
        from tui_gateway import server
        frame=active['frame']
        with server._sessions_lock,self.lock:
            if self.active.get(session) is not active:
                return
            if active['finished']:
                self.active.pop(session,None)
                return
            live=(server._sessions.get(frame.sid) is frame.record
                  and frame.record.get('_hermes_desktop_dialogue_frame') is frame
                  and frame.record.get('running') is True and frame.record.get('agent') is frame.agent
                  and not frame.record.get('_closing')
                  and frame.record.get('active_session_lease') is frame.lease
                  and not getattr(frame.lease,'released',True)
                  and int(frame.record.get('_queued_prompt_generation',0))==frame.generation)
            if not live:
                active['finished']=True
                try:
                    if active['priority']:
                        self.backend.foreground_finished(active['identity'].owner,session)
                finally:
                    self.active.pop(session,None)
            elif active['priority']:
                self.backend.foreground_started(active['identity'].owner,session)

    async def poll_once(self):
        # No finished-turn CURRENT grant is inherited by the poll task. Liveness is
        # checked against the real native record/agent/lease/generation each tick.
        with self.lock:
            active_rows = list(self.active.items())
        for session, active in active_rows:
            try:
                await asyncio.to_thread(self._renew,session,active)
            except Exception:
                continue
        with self.store.connection() as db:
            origins = list(db.execute('''SELECT j.job,j.session,j.owner,j.descriptor FROM desktop_jobs j
                LEFT JOIN outbox o ON o.job=j.job AND o.phase='final'
                WHERE o.status IS NULL OR o.status='pending' ORDER BY j.rowid LIMIT 100'''))
        for job,session, owner, payload in origins:
            try:
                descriptor = json.loads(payload)
                rows = [await asyncio.to_thread(self.backend.status,job,owner,session)]
                for row in rows:
                    if row.get('state') not in TERMINAL or row.get('owner') != owner or row.get('session_id') != session:
                        continue
                    from tui_gateway.plugin_dialogue import deliver
                    result = await deliver(self, descriptor, row, status_text(row))
                    if result == 'delivered':
                        self.store.metric('job_final_visible', session, job=row['job_id'])
                    elif result == 'unknown':
                        self.store.metric('delivery_unknown', session, job=row['job_id'])
            except Exception:
                continue

    async def _poll(self):
        while not self.closed:
            await self.poll_once()
            await asyncio.sleep(2)
