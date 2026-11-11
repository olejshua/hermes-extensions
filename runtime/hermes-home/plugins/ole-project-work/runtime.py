"""Native, owner-bound project controls. No worker loop or SaaS transport."""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading

TERMINAL = {'completed', 'failed', 'interrupted', 'paused', 'blocked'}
MUTATIONS = {'start', 'steer', 'pause', 'resume'}


def private_dir(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('private_absolute_root_required')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('symlink_refused')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('private_root_required')
    return path


class Ledger:
    """Reservation precedes send. A crashed/uncertain send is never auto-replayed."""
    def __init__(self, root):
        self.root = private_dir(root)
        self.path = self.root / 'delivery.sqlite'
        if self.path.exists() and (self.path.is_symlink() or self.path.stat().st_mode & 0o077):
            raise ValueError('private_ledger_required')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS delivery (job TEXT, revision INTEGER, artifact TEXT, state TEXT, receipt TEXT, PRIMARY KEY(job,revision,artifact))')
            db.execute('CREATE TABLE IF NOT EXISTS job_titles (job TEXT PRIMARY KEY, title TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS delivery_manifests (job TEXT, revision INTEGER, document TEXT NOT NULL, PRIMARY KEY(job,revision))')
    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()
    def reserve(self, job, revision, artifact):
        with self.db() as db:
            cur = db.execute('INSERT OR IGNORE INTO delivery VALUES (?,?,?,\'unknown\',NULL)', (job, revision, artifact))
            if cur.rowcount == 1:
                return True
            # Only an explicit trusted pre-network skip can release a reservation.
            # Unknown and delivered rows are never eligible for this transition.
            return db.execute("UPDATE delivery SET state='unknown',receipt=NULL WHERE job=? AND revision=? AND artifact=? AND state='deferred'", (job, revision, artifact)).rowcount == 1
    def settle(self, job, revision, artifact, receipt, *, deferred=False):
        with self.db() as db:
            db.execute('UPDATE delivery SET state=?,receipt=? WHERE job=? AND revision=? AND artifact=?',
                       ('delivered' if receipt else 'deferred' if deferred else 'unknown', str(receipt) if receipt else None, job, revision, artifact))
    def manifest(self, job, revision):
        with self.db() as db:
            row = db.execute('SELECT document FROM delivery_manifests WHERE job=? AND revision=?', (job, revision)).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        if (not isinstance(value, dict) or not isinstance(value.get('artifacts'), list)
                or not value['artifacts'] or not isinstance(value.get('caption'), str)):
            raise ValueError('delivery_manifest_invalid')
        return value
    def pin_manifest(self, job, revision, document):
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        with self.db() as db:
            row = db.execute('SELECT document FROM delivery_manifests WHERE job=? AND revision=?', (job, revision)).fetchone()
            if row is not None:
                if row[0] != encoded:
                    raise ValueError('delivery_manifest_changed')
                return
            db.execute('INSERT INTO delivery_manifests VALUES (?,?,?)', (job, revision, encoded))
    def handled(self, job, revision, artifact):
        with self.db() as db:
            row = db.execute('SELECT state FROM delivery WHERE job=? AND revision=? AND artifact=?', (job, revision, artifact)).fetchone()
        return row is not None and row[0] in ('delivered', 'unknown')
    def revision_handled(self, job, revision):
        # Terminal receipt decisions precede ALL Engine result/artifact payload reads.
        # A manifest is necessary to distinguish a partial multi-file send from
        # a fully handled revision, including a crash before its next reservation.
        document = self.manifest(job, revision)
        with self.db() as db:
            states = dict(db.execute('SELECT artifact,state FROM delivery WHERE job=? AND revision=?', (job, revision)))
        if document is not None:
            return all(states.get(item['handle']) in ('delivered', 'unknown') for item in document['artifacts'])
        # Legacy receipts have no complete manifest. Preserve terminal rows and
        # fail closed rather than infer permission to resend an unknown result.
        # Explicit known-unsent deferred rows remain eligible for reconciliation.
        legacy = [state for artifact, state in states.items() if not artifact.startswith('state:')]
        return bool(legacy) and all(state in ('delivered', 'unknown') for state in legacy)
    def remember(self, job, goal):
        title = ' '.join(goal.split())[:240]
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO job_titles VALUES (?,?)', (job, title))
    def titled(self, row):
        if not isinstance(row, dict):
            return row
        with self.db() as db:
            title = db.execute('SELECT title FROM job_titles WHERE job=?', (row.get('job_id'),)).fetchone()
        return {**row, 'title': title[0] if title else row.get('title', 'Acribic-Projektauftrag')}
    def rows(self, job):
        with self.db() as db:
            return [dict(revision=r[0], artifact=r[1], state=r[2], receipt=r[3]) for r in db.execute('SELECT revision,artifact,state,receipt FROM delivery WHERE job=?', (job,))]


def status_text(row):
    state = row.get('state')
    return {
        'queued': 'Der Acribic-Auftrag ist gespeichert und wartet auf den Arbeitslauf.',
        'working': 'Der Acribic-Auftrag wird bearbeitet. Du kannst währenddessen normal weiterschreiben.',
        'reviewing': 'Das Ergebnis wird gegen Auftrag und Quellen geprüft.',
        'recovering': 'Die gespeicherte Arbeit wird nach einem Abbruch geprüft.',
        'uncertain': 'Ein unklarer Vorgang muss zuerst abgeglichen werden.',
        'completed': 'Die Acribic-Ergebnisse sind geprüft und liegen als Dateien vor.',
        'interrupted': 'Die Arbeit wurde unterbrochen. Sie kann mit dem gespeicherten Stand fortgesetzt werden.',
        'paused': 'Die Arbeit ist pausiert. Der gespeicherte Stand bleibt erhalten.',
        'pausing': 'Die Unterbrechung ist angefordert; der laufende Schritt ist noch nicht bestätigt beendet.',
        'failed': 'Der Arbeitslauf ist fehlgeschlagen. Der gespeicherte Stand und die Fehlerursache bleiben prüfbar.',
        'blocked': 'Die Arbeit wartet auf eine noch fehlende Voraussetzung.',
    }.get(state, 'Der aktuelle Arbeitsstand ist noch nicht abschließend bestätigt.')


def completion_text(result):
    summary = result.get('summary')
    if (isinstance(summary, str) and 0 < len(summary.strip()) <= 400
            and '\n' not in summary and '@' not in summary
            and not re.search(r'[a-f0-9]{20,}|[\U0001f000-\U0001ffff]', summary)
            and len(re.split(r'[.!?]\s+', summary.strip())) <= 2):
        return summary.strip()
    return 'Die Acribic-Ergebnisse liegen als geprüfte Dateien vor.'


class ProjectPlugin:
    def __init__(self, ctx, service, root):
        self.ctx, self.service = ctx, service
        self.ledger = Ledger(root)
        self.inbounds, self.observed = {}, {}
        self.lock = threading.RLock()
        self.task = None
        self.sync_task = None
        from .monolith_bridge import Bridge
        from .monolith_wire import Wire
        self.bridge = Bridge(self.ledger,service,wire_factory=lambda: Wire.native(ctx))

    @property
    def dialogue(self):
        return self.ctx._manager._ole_dialogue_instance

    def identity(self, native):
        from gateway.session_context import get_session_env as env, session_context_engaged
        d = self.dialogue
        key = env('HERMES_SESSION_KEY', '')
        if (not session_context_engaged() or env('HERMES_SESSION_PLATFORM') != 'telegram'
                or env('HERMES_SESSION_CHAT_TYPE') not in ('dm', 'private')
                or 'telegram:' + str(env('HERMES_SESSION_USER_ID')) != self.service.policy['owner']
                or (env('HERMES_SESSION_PROFILE', '') or None) != self.service.policy.get('gateway_profile')
                or env('HERMES_CRON_SESSION') == '1' or not key
                or key != self.service.policy.get('session_id')):
            raise ValueError('current_private_human_required')
        actor = native.get('parent_agent')
        if actor is None:
            # Native model_tools dispatch supplies session_id, not parent_agent.
            # TurnFacade binds the real execution actor in this host ContextVar.
            from agent.subagent_lifecycle import get_active_subagent_parent
            actor = get_active_subagent_parent()
        if (actor is None or getattr(actor, '_parent_session_id', None)
                or getattr(actor, '_turn_author', {}).get('is_bot') is not False
                or str(getattr(actor, '_turn_author', {}).get('id')) != d.policy.user_id):
            raise ValueError('current_private_human_required')
        inbound = d.steering.current_inbound(key, actor, native.get('session_id'))
        if str(getattr(actor, '_persist_user_message_platform_id', '')) != str(inbound):
            raise ValueError('current_private_human_required')
        with self.lock:
            recorded = self.inbounds.get(key)
        if recorded is None or recorded['message'] != inbound:
            raise ValueError('current_private_human_required')
        return self.service.policy['owner'], key, inbound, recorded['text']

    async def admitted(self, event, gateway, session_key, **_):
        d = self.dialogue
        if (getattr(event, 'internal', False) or not d.policy.bound(event.source)
                or not gateway._is_user_authorized_for_source(event.source)):
            return None
        with self.lock:
            self.inbounds[session_key] = dict(message=str(event.message_id), text=event.text)
            self.observed.pop(session_key, None)
        # No text classifier, no mutation, no swallowed ordinary conversation.
        return None

    def call(self, action, args, **native):
        owner, session, inbound, text = self.identity(native)
        if not isinstance(args, dict):
            raise ValueError('arguments_required')
        allowed = {'start': {'project', 'goal', 'task_id'}, 'status': {'job_id'}, 'steer': {'job_id', 'instruction', 'expected_revision'},
                   'pause': {'job_id'}, 'resume': {'job_id'}, 'result': {'job_id'}}[action]
        if set(args) - allowed:
            raise ValueError('unknown_arguments')
        job = args.get('job_id')
        link = self.bridge.find(job) if job else None
        if link is not None: self.bridge.owned(link,owner,session)
        if action not in ('start', 'status') and not isinstance(job, str):
            raise ValueError('concrete_job_required')
        if action == 'status':
            if link is not None:
                result=self.bridge.public(link)
                result['original_goal']=link['goal']
                result['goal_is_reference_not_new_instruction']=True
            else:
                result = self.service.status(job, owner, session) if job else self.service.list_owned(owner, session)
                if not job and result.get('ok'):
                    links=[self.bridge.owned(row,owner,session) for row in self.bridge.rows() if row['owner']==owner and row['session']==session]
                    linked_jobs={row.get('engine_job_id') for row in links}
                    result['jobs']=[row for row in result['jobs'] if row['job_id'] not in linked_jobs]+[self.bridge.public(row) for row in links]
            if job:
                result = self.ledger.titled(result)
            elif isinstance(result, dict) and isinstance(result.get('jobs'), list):
                result = {**result, 'jobs': [self.ledger.titled(row) for row in result['jobs']]}
            if job:
                with self.lock:
                    self.observed[session] = (inbound, job, result.get('revision'))
            result = dict(result)
            if job:
                result['delivery'] = self.ledger.rows(link['engine_job_id']) if link is not None and link.get('engine_job_id') else [] if link is not None else self.ledger.rows(job)
                source = self.dialogue.sources.get(session)
                gateway = self.dialogue.gateway
                adapter = gateway._delivery_adapter_for(source) if gateway is not None and source is not None else None
                result['document_delivery_capability'] = ('verified_native_v1' if getattr(adapter, 'native_file_receipt_version', 0) == 1 else 'native_receipt_upgrade_required')
            return result
        if action == 'result':
            if link is not None:
                if not link.get('engine_job_id'):return {**self.bridge.public(link),'ok':False,'error':'linked_work_not_started'}
                return {**self.service.result(link['engine_job_id'],owner,session),'job_id':link['id'],
                        'monolith_publication':self.bridge.public(link)['monolith_publication']}
            return self.service.result(job, owner, session)
        # Caller cannot choose request id, native author, profile, session, or file path.
        request = hashlib.sha256(f'{owner}\0{session}\0{inbound}\0{action}\0{job or ""}'.encode()).hexdigest()
        if action == 'start':
            if args.get('project') not in ('acribic','monolith') or not isinstance(args.get('goal'), str) or not args['goal'].strip():
                raise ValueError('acribic_goal_required')
            if args['project']=='monolith' and 'task_id' not in args:raise ValueError('exact_monolith_task_required')
            goal = ('Vollständige aktuelle Nutzernachricht, unverändert. '
                    'Ihre Vorgaben und Verbote haben bei Konflikten Vorrang vor dem Arbeitsrahmen.\n'
                    '<originalauftrag>\n' + text + '\n</originalauftrag>\n\n'
                    'Arbeitsrahmen des Hauptdialogs zur Ausführung; keine Erweiterung der Freigaben:\n'
                    '<arbeitsrahmen>\n' + args['goal'] + '\n</arbeitsrahmen>')
            if len(goal) > 16000:
                raise ValueError('complete_original_exceeds_engine_limit')
            if 'task_id' in args:
                result=self.bridge.create(goal,args['task_id'],request,owner,session,title=args['goal'],project_kind=args['project'])
                self.ledger.remember(result['job_id'],args['goal'])
                return result
            receipt=self.service.submission_receipt('acribic',goal,request,owner,session)
            if receipt.get('ok') is not True:return {**receipt,'accepted':False}
            if receipt.get('found'):return {**self.ledger.titled(receipt),'accepted':True}
            result = self.service.submit('acribic', goal, request, owner, session)
            if result.get('ok') is True and result.get('job_id'):
                self.ledger.remember(result['job_id'], args['goal'])
            return {**self.ledger.titled(result), 'accepted': result.get('ok') is True and bool(result.get('job_id'))}
        if action == 'steer':
            instruction, revision = args.get('instruction'), args.get('expected_revision')
            if not isinstance(instruction, str) or instruction.strip() != text.strip():
                raise ValueError('verbatim_current_instruction_required')
            # Meaning is interpreted by main, never a second classifier. Pure question
            # forms fail closed; genuine imperative questions may need clarification.
            if re.match(r'^\s*(wie|was|warum|wieso|wann|wer|wo|welch|ist|sind|hast)\b', instruction, re.I):
                raise ValueError('explicit_correction_required')
            with self.lock:
                observed = self.observed.get(session)
            if type(revision) is not int or observed != (inbound, job, revision):
                raise ValueError('read_current_job_revision_first')
            # Main resolves natural references from owned readable titles/goals.
            # The just-read exact job binding is authoritative; no technical ID
            # is required in the user's natural sentence.
            if link is not None:return self.bridge.command(link,'steer',request,owner,session,instruction=instruction,expected_revision=revision)
            return self.service.steer(job, instruction, revision, request, owner, session)
        with self.lock:
            observed = self.observed.get(session)
        if observed is None or observed[:2] != (inbound, job):
            raise ValueError('read_current_job_revision_first')
        if link is not None:return self.bridge.command(link,action,request,owner,session)
        method = self.service.interrupt if action == 'pause' else self.service.resume
        return method(job, request, owner, session)

    def safe_call(self, action, args, **native):
        try:
            return json.dumps(self.call(action, args, **native), ensure_ascii=False)
        except Exception as exc:
            known = {'current_private_human_required', 'arguments_required', 'unknown_arguments', 'concrete_job_required',
                     'acribic_goal_required', 'complete_original_exceeds_engine_limit', 'verbatim_current_instruction_required', 'explicit_correction_required',
                     'read_current_job_revision_first', 'ambiguous_job_reference', 'exact_monolith_id_required',
                     'monolith_reader_unavailable', 'linked_instruction_too_large', 'monolith_identity_invalid', 'request_id_conflict', 'monolith_project_ambiguous', 'monolith_exact_context_required', 'monolith_task_project_mismatch', 'exact_monolith_task_required'}
            code = str(exc) if isinstance(exc, ValueError) and str(exc) in known else 'project_operation_unavailable'
            return json.dumps(dict(state='unavailable', error=code, success_claimed=False))

    def idle(self, session, source):
        d = self.dialogue
        g = d.gateway
        if (g is None or not d.policy.bound(source) or not g._is_user_authorized_for_source(source)
                or g._session_key_for_source(source) != session
                or session in getattr(g, '_pending_approvals', {})):
            return None
        adapter = g._delivery_adapter_for(source)
        if adapter is None or not d._delivery_idle(session, adapter, 'project-no-ack'):
            return None
        return adapter

    async def deliver(self, row, session, source):
        job, revision = row.get('job_id'), row.get('revision')
        owner = self.service.policy['owner']
        if session != self.service.policy.get('session_id') or type(revision) is not int:
            return
        if self.ledger.revision_handled(job, revision):
            return
        adapter = self.idle(session, source)
        if adapter is None or getattr(adapter, 'native_file_receipt_version', 0) != 1:
            return
        # Result descriptor/caption metadata is pinned once; artifact bytes remain
        # Engine-owned and are only read for unattempted or known-unsent handles.
        document = self.ledger.manifest(job, revision)
        if document is None:
            result = await asyncio.to_thread(self.service.result, job, owner, session)
            if result.get('ok') is not True or result.get('job_id') != job or result.get('revision') != revision:
                return
            artifacts = result.get('artifacts', [])
            if not artifacts or not callable(getattr(self.service, 'artifact_read', None)):
                return
            pinned = []
            for artifact in artifacts:
                handle, digest = artifact.get('handle'), artifact.get('sha256')
                name = artifact.get('name', 'Acribic-Ergebnis.md')
                if (not isinstance(handle, str) or not handle or not re.fullmatch('[a-f0-9]{64}', str(digest))
                        or not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9ÄÖÜäöüß_ -][A-Za-z0-9ÄÖÜäöüß_. -]{0,99}\.(md|txt|csv|html|png|jpg|jpeg|webp|svg)', name)):
                    return
                pinned.append(dict(handle=handle, sha256=digest, name=name))
            if len({item['handle'] for item in pinned}) != len(pinned):
                return
            document = dict(artifacts=pinned, caption=completion_text(result))
            self.ledger.pin_manifest(job, revision, document)
        for artifact in document['artifacts']:
            handle, digest, name = artifact['handle'], artifact['sha256'], artifact['name']
            if self.ledger.handled(job, revision, handle):
                continue
            data = await asyncio.to_thread(self.service.artifact_read, handle, owner, session)
            # Contract is deliberately data/handle based, not arbitrary path access.
            if data.get('ok') is not True or data.get('sha256') != digest:
                return
            try:
                content = base64.b64decode(data['base64'], validate=True)
            except (KeyError, ValueError, TypeError):
                return
            if hashlib.sha256(content).hexdigest() != digest or len(content) != data.get('size'):
                return
            export = self.ledger.root / (hashlib.sha256(handle.encode()).hexdigest() + Path(name).suffix)
            fd = os.open(export, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600) if not export.exists() else None
            if fd is not None:
                with os.fdopen(fd, 'wb') as f:
                    f.write(content)
            info = export.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or info.st_nlink != 1 or hashlib.sha256(export.read_bytes()).hexdigest() != digest):
                return
            latest = await asyncio.to_thread(self.service.status, job, owner, session)
            if latest.get('revision') != revision or latest.get('state') != 'completed' or self.idle(session, source) is not adapter:
                return
            if not self.ledger.reserve(job, revision, handle):
                continue
            receipt, deferred = None, False
            try:
                metadata = self.dialogue.gateway._thread_metadata_for_source(source, getattr(source, 'message_id', None)) or {}
                sent = await adapter.send_document(source.chat_id, str(export), caption=document['caption'], file_name=name,
                    reply_to=getattr(source, 'message_id', None), metadata={**metadata, '_interim_send': True, '_ole_dialogue_job_final': True})
                raw = getattr(sent, 'raw_response', None)
                deferred = isinstance(raw, dict) and raw.get('skipped') is True
                if (getattr(sent, 'success', False) is True and isinstance(raw, dict)
                        and raw.get('native_media') == 'document' and raw.get('native_file_id')
                        and raw.get('skipped') is not True):
                    receipt = getattr(sent, 'message_id', None)
            finally:
                self.ledger.settle(job, revision, handle, receipt, deferred=deferred)

    async def notify_state(self, row, session, source):
        job, revision, state = row.get('job_id'), row.get('revision'), row.get('state')
        if type(revision) is not int or session != self.service.policy.get('session_id'):
            return
        key = 'state:' + state
        if self.ledger.handled(job, revision, key):
            return
        adapter = self.idle(session, source)
        if adapter is None or not callable(getattr(adapter, 'send', None)):
            return
        latest = await asyncio.to_thread(self.service.status, job, self.service.policy['owner'], session)
        if (latest.get('ok') is not True or latest.get('revision') != revision
                or latest.get('state') != state or self.idle(session, source) is not adapter):
            return
        if not self.ledger.reserve(job, revision, key):
            return
        receipt, deferred = None, False
        try:
            metadata = self.dialogue.gateway._thread_metadata_for_source(source, getattr(source, 'message_id', None)) or {}
            sent = await adapter.send(source.chat_id, status_text(row), reply_to=getattr(source, 'message_id', None),
                metadata={**metadata, '_interim_send': True, '_ole_dialogue_job_final': True})
            raw = getattr(sent, 'raw_response', None)
            deferred = isinstance(raw, dict) and raw.get('skipped') is True
            if getattr(sent, 'success', False) is True and not (isinstance(raw, dict) and raw.get('skipped')):
                receipt = getattr(sent, 'message_id', None)
        finally:
            self.ledger.settle(job, revision, key, receipt, deferred=deferred)

    async def poll_once(self):
        d = self.dialogue
        for session, source in list(d.sources.items()):
            if self.idle(session, source) is None:
                continue
            rows = await asyncio.to_thread(self.service.list_owned, self.service.policy['owner'], session)
            rows = rows.get('jobs', []) if isinstance(rows, dict) else rows
            for row in rows:
                link=self.bridge.find(row.get('job_id'))
                if link is not None and link.get('origin')=='monolith_task':continue
                if row.get('state') == 'completed':
                    if self.ledger.revision_handled(row.get('job_id'), row.get('revision')):
                        continue
                    await self.deliver(row, session, source)
                elif row.get('state') in ('failed', 'blocked', 'interrupted', 'uncertain'):
                    await self.notify_state(row, session, source)

    async def ready(self, gateway, **_):
        if self.task is None or self.task.done():
            self.task = self.ctx.spawn_task(self.poll(), name='ole-project-work-delivery')

        if self.sync_task is None or self.sync_task.done():
            self.sync_task = self.ctx.spawn_task(self.sync(), name='ole-project-work-monolith')

    async def sync(self):
        while True:
            try: await asyncio.to_thread(self.bridge.poll_once)
            except asyncio.CancelledError: raise
            except Exception: pass
            await asyncio.sleep(3)

    async def poll(self):
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # No raw exception/content in telemetry. Status remains inspectable.
            await asyncio.sleep(3)
