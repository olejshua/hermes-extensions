"""Thin source-bound handoff to existing native terminal/Codex background jobs.

No new worker, model router, scheduler or coding queue. The private journal only
prevents replay after an uncertain native spawn. Operator-reviewed file maps
replace foreground repository discovery; original installed files stay intact.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import threading
import sys
import time
import subprocess

NAME = 'dialogue_coding_start'
PARAMETERS = {'type': 'object', 'additionalProperties': False, 'properties': {
    'target': {'type': 'string', 'pattern': '^[a-z0-9_-]{1,60}$'},
    'brief': {'type': 'string', 'minLength': 20, 'maxLength': 40000},
    'answer': {'type': 'string', 'maxLength': 40000,
               'description': 'Complete factual answer to the current human question from the sources already read in this SAME turn, including exact requested draft content and source gaps. No reasoning, routine commentary or build status. Use an empty string for coding-only, research, more required reading or if the full answer exceeds this field; never shorten required facts to fit. The adapter streams this answer before starting the independent build. Only when answer_presentation.native_stream_callback_invoked=true, report just the actual start/unknown outcome afterwards; otherwise include the complete factual answer in the final reply.'},
}, 'required': ['target', 'brief', 'answer']}
DESCRIPTION = (
    'Only after the current human explicitly asks for a NEW coding task: hand the concrete brief '
    'to the EXISTING native Codex CLI background process. Select a configured target; '
    'no foreground repo search, write_file, codex --help, or process wait is needed. '
    'Copies exact operator-selected installed source files to a private review workspace, '
    'starts native background=true/notify=true and immediately reads its tracking receipt. '
    'No deployment/restart, source overwrite, commit/push, mail/calendar/CRM action. '
    'answer carries the complete already-source-grounded factual reply, or empty for coding-only/research. '
    'The existing native stream shows that factual reply before this independent build; never include thoughts/status in answer. '
    'Returned accepted means durable handoff plus tracked process, never completed code. '
    'Existing native process status/cancel/notifications retain ownership. '
    'Do not use for mere criticism, correction/status of an existing job, or an unrequested feature.'
)
RULES = '''Work only in this private review workspace. Sources were copied from the actually installed files; source-manifest.json records every origin and hash. Preserve existing behavior, background jobs, resumption, corrections, sources, MEMORY/USER/SOUL, auth/model route and permissions. Do not alter any live installation, service, config or private data. No deployment/restart, git commit/push/merge, mail send, invitation, calendar/CRM mutation, new keys or paid API. Read source rules. Implement the requested change, verify against the real original and relevant counterexamples, and return concise findings/changed files/tests/integration gaps. A missing source is a gap, not permission to replace with an old staging file. No autoactivation.\n\n'''


class CodingHandoff:
    def __init__(self, root, reads, targets, dispatch, *, binary='/home/hermes/.local/bin/codex', inspect_process=None, deadline_binary='/usr/bin/timeout', preflight=None):
        self.root = Path(root)/'coding-handoffs'
        self.reads, self.dispatch, self.binary = reads, dispatch, Path(binary)
        self.targets = targets if isinstance(targets, dict) else {}
        self.inspect_process = inspect_process
        self.deadline_binary = Path(deadline_binary)
        self.preflight = preflight
        self.lock = threading.RLock()

    def capabilities(self):
        return [dict(target=key, description=str(value.get('description', ''))[:240], timeout_seconds=value['timeout_seconds'])
                for key, value in self.targets.items() if self._valid_target(key, value)]

    @staticmethod
    def _valid_target(key, value):
        return (isinstance(key, str) and re.fullmatch(r'[a-z0-9_-]{1,60}', key)
                and isinstance(value, dict) and isinstance(value.get('files'), dict)
                and 1 <= len(value['files']) <= 256
                and type(value.get('timeout_seconds')) is int and 300 <= value['timeout_seconds'] <= 7200)

    def _database(self):
        if self.root.is_symlink(): raise ValueError('private_handoff_directory_required')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        path = self.root/'receipts.sqlite3'
        if path.is_symlink(): raise ValueError('private_handoff_database_required')
        db = sqlite3.connect(path, timeout=10)
        os.chmod(path, 0o600)
        db.execute('CREATE TABLE IF NOT EXISTS handoffs (identity TEXT PRIMARY KEY, brief_hash TEXT, state TEXT, receipt TEXT)')
        db.commit(); return db

    def _bundle(self, identity, target, brief):
        directory = self.root/identity
        directory.mkdir(mode=0o700)
        work = directory/'work'; work.mkdir(mode=0o700)
        manifest = []
        for relative, source_path in target['files'].items():
            rel = Path(relative); source = Path(source_path)
            if (not isinstance(relative, str) or rel.is_absolute() or '..' in rel.parts
                    or '.git' in rel.parts or not source.is_absolute() or source.is_symlink()
                    or not source.is_file() or source.stat().st_size > 4*1024*1024
                    or source.suffix not in ('.py', '.md', '.toml', '.yaml')
                    or source.name in ('config.yaml', 'USER.md', 'MEMORY.md', 'SOUL.md')
                    or any(word in source.name.casefold() for word in ('token', 'secret', 'credential', '.env'))):
                raise ValueError('reviewed_code_source_map_required')
            data = source.read_bytes(); dest = work/rel
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            dest.write_bytes(data); dest.chmod(0o600)
            manifest.append(dict(path=str(rel), origin=str(source), sha256=hashlib.sha256(data).hexdigest()))
        (work/'source-manifest.json').write_text(json.dumps(manifest, indent=2))
        (work/'source-manifest.json').chmod(0o600)
        prompt = directory/'brief.txt'; prompt.write_text(RULES+brief); prompt.chmod(0o600)
        return work, prompt, manifest

    def _result(self, receipt, session_id, actor):
        remember = getattr(self.reads, 'remember_coding_process', None)
        if callable(remember):
            receipt = dict(receipt, foreground_wait_protected=remember(
                session_id, getattr(actor, '_current_turn_id', ''), receipt) is True)
        return json.dumps(receipt, ensure_ascii=False)

    def start(self, args, *, session_id='', **_):
        attempted = False
        receipt = None
        try:
            if (not isinstance(args, dict) or set(args) not in ({'target', 'brief'}, {'target', 'brief', 'answer'})
                    or not isinstance(args['brief'], str) or not 20 <= len(args['brief']) <= 40000
                    or ('answer' in args and (not isinstance(args['answer'], str) or len(args['answer']) > 40000))
                    or not self._valid_target(args['target'], self.targets.get(args['target']))):
                raise ValueError('configured_target_and_concrete_brief_required')
            actor = self.reads._actor(session_id)
            # Origin comes from the admitted human message, never model arguments.
            origin = None
            with self.reads.dialogue.steering.lock:
                routes = list(self.reads.dialogue.steering.routes)
            for key in routes:
                try:
                    origin = self.reads.dialogue.steering.current_inbound(key, actor, session_id)
                    break
                except ValueError:
                    continue
            if not origin: raise ValueError('current_human_origin_required')
            identity = hashlib.sha256(json.dumps([session_id, origin, args['target']]).encode()).hexdigest()
            brief_hash = hashlib.sha256(args['brief'].encode()).hexdigest()
            with self.lock, closing(self._database()) as db:
                row = db.execute('SELECT brief_hash,state,receipt FROM handoffs WHERE identity=?', (identity,)).fetchone()
                if row:
                    saved = json.loads(row[2])
                    fallback = self.root/identity/'receipt-fallback.json'
                    if fallback.is_file() and not fallback.is_symlink():
                        extra = json.loads(fallback.read_text())
                        if extra.get('handoff_id') == identity:
                            saved = extra
                    return self._result(dict(saved, replayed=True,
                                             same_brief=row[0] == brief_hash), session_id, actor)
                with self.reads.dialogue.active_lock:
                    human = self.reads.dialogue.active.get(key)
                    if not human or human.get('finished') or human.get('message') != origin:
                        raise ValueError('active_current_human_turn_required')
                if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
                    raise ValueError('existing_codex_cli_unavailable')
                if not self.deadline_binary.is_file() or not os.access(self.deadline_binary, os.X_OK):
                    raise ValueError('existing_native_deadline_command_required')
                if self.preflight is not None and self.preflight() is not True:
                    raise ValueError('native_scope_or_supervised_owner_unavailable')
                # Only an AI-written factual answer bound to this turn's fully
                # exposed source read may enter the native user-text stream.
                # Generic commentary/reasoning callbacks stay disabled.
                present = getattr(self.reads, 'present_answer', None)
                presentation = (present(args.get('answer', ''), session_id, actor)
                                if callable(present) else None)
                # Validate/copy outside live source; no clean-Git requirement or baseline commit.
                work, prompt, manifest = self._bundle(identity, self.targets[args['target']], args['brief'])
                receipt = dict(accepted=False, state='launch_unknown', handoff_id=identity,
                               target=args['target'], source_files=len(manifest), completed=False,
                               origin_message_id=str(origin), origin_session_id=session_id, origin_session_key=key,
                               request_brief_sha256=brief_hash,
                               do_not_repeat=True, timeout_seconds=self.targets[args['target']]['timeout_seconds'],
                               timeout_exit_code=124, workspace=str(work), events_path=str(work.parent/'events.jsonl'),
                               work_preserved=True, resume_requires_explicit_authorization=True,
                               resume_identity='exact thread.started thread_id from events; never --last')
                receipt['deadline_at'] = time.time()+receipt['timeout_seconds']
                if presentation is not None:
                    receipt['answer_presentation'] = presentation
                db.execute('INSERT INTO handoffs VALUES(?,?,?,?)',
                           (identity, brief_hash, 'launching', json.dumps(receipt))); db.commit()
            codex = [str(self.binary), 'exec', '--json', '--skip-git-repo-check', '--sandbox',
                                  'workspace-write', '--model', 'gpt-6.1-sol', '-c',
                                  'model_reasoning_effort="medium"', '-C', str(work), '-']
            command = ('umask 077; set -o pipefail; '+shlex.join([str(self.deadline_binary), '--signal=TERM',
                '--kill-after=30', str(receipt['timeout_seconds']), *codex])+' < '+shlex.quote(str(prompt))+
                ' 2> '+shlex.quote(str(work.parent/'native.stderr'))+' | /usr/bin/tee '+shlex.quote(receipt['events_path']))
            tool_args = dict(command=command, background=True, notify=True, persist_on_release=True)
            attempted = True
            try:
                raw = self.dispatch('terminal', tool_args, session_id=session_id, actor=actor)
                value = json.loads(raw) if isinstance(raw, str) else raw
                process_id = value.get('session_id') if isinstance(value, dict) else None
                if not isinstance(process_id, str) or not re.fullmatch(r'proc_[A-Za-z0-9_-]+', process_id):
                    raise ValueError('native_spawn_unverified')
                receipt['process_id'] = process_id
                check = self.dispatch('process_manage', {'action': 'poll', 'session_id': process_id},
                                      session_id=session_id, actor=actor)
                checked = json.loads(check) if isinstance(check, str) else check
                if (not isinstance(checked, dict) or checked.get('status') in ('not_found', 'error')
                        or checked.get('error') or checked.get('session_id', process_id) != process_id):
                    raise ValueError('native_process_readback_unverified')
                proof = self.inspect_process(process_id, session_id, actor) if self.inspect_process else {}
                if not all(proof.get(key) is True for key in (
                        'source_binding_verified', 'notification_verified', 'persistence_verified', 'scope_verified')):
                    raise ValueError('native_process_binding_or_persistence_unverified')
                receipt.update(accepted=True, state='started', process_id=process_id,
                               notification='native_process_completion', native_process_proof=proof,
                               review_required=True)
            except Exception:
                # A timeout/error may follow a successful native spawn. Never replay it.
                receipt.update(error='native_launch_or_readback_unknown_do_not_repeat')
            with self.lock, closing(self._database()) as db:
                db.execute('UPDATE handoffs SET state=?,receipt=? WHERE identity=?',
                           (receipt['state'], json.dumps(receipt), identity)); db.commit()
            return self._result(receipt, session_id, actor)
        except Exception:
            if attempted:
                unknown = dict(receipt or {}, accepted=False, completed=False, state='launch_unknown',
                               error='native_receipt_persistence_unknown_do_not_repeat')
                try:
                    directory = self.root/unknown['handoff_id']
                    path = directory/'receipt-fallback.json'
                    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0), 0o600)
                    with os.fdopen(fd, 'w') as stream:
                        json.dump(unknown, stream); stream.flush(); os.fsync(stream.fileno())
                except Exception:
                    pass
                return self._result(unknown, session_id, actor)
            return json.dumps(dict(accepted=False, completed=False, state='not_started',
                                   error='coding_handoff_preflight_unavailable',
                                   available_targets=self.capabilities()), ensure_ascii=False)


def native_process_proof(reads, process_id, session_id, actor):
    """Read actual native owner, watcher, cgroup and recovery checkpoint fields."""
    from tools.process_registry import process_registry, _checkpoint_path
    current = reads._actor(session_id, expected=actor)
    if current is not actor: return {}
    bound = None
    with reads.dialogue.steering.lock:
        routes = list(reads.dialogue.steering.routes.items())
    for key, route in routes:
        try:
            origin = reads.dialogue.steering.current_inbound(key, actor, session_id)
            bound = (key, route['source'], origin); break
        except ValueError:
            continue
    process = process_registry.get(process_id)
    if bound is None or process is None: return {}
    key, source, origin = bound
    source_ok = ((process.owner_task_id or process.task_id) == session_id
                 and process.session_key == key and process.parent_session_id == session_id
                 and process.watcher_platform == 'telegram'
                 and str(process.watcher_chat_id) == str(source.chat_id)
                 and str(process.watcher_user_id) == str(source.user_id)
                 and str(process.watcher_message_id) == str(origin))
    process_registry._write_checkpoint()  # Existing native recovery format/owner.
    saved = next((row for row in json.loads(_checkpoint_path().read_text())
                  if row.get('session_id') == process_id), None)
    persistent = (process.persist_on_release is True and saved is not None
                  and saved.get('owner_task_id') == session_id and saved.get('session_key') == key
                  and saved.get('parent_session_id') == session_id)
    scope = not sys.platform.startswith('linux')
    if sys.platform.startswith('linux') and process.systemd_unit:
        from tools.process_registry import systemd_user_bus_env
        # Observe the actual cgroup, not just the requested native scope name.
        for delay in (0, .1, .2, .4):
            if delay: time.sleep(delay)
            value = subprocess.run(['/usr/bin/systemctl', '--user', 'show', process.systemd_unit,
                '--property=ControlGroup', '--value'], capture_output=True, text=True,
                timeout=2, env=systemd_user_bus_env())
            group = value.stdout.strip()
            if value.returncode == 0 and group.startswith('/') and group.endswith('/'+process.systemd_unit):
                members = Path('/sys/fs/cgroup'+group)/'cgroup.procs'
                if members.is_file() and members.read_text().strip():
                    scope = True; break
    return dict(source_binding_verified=source_ok, notification_verified=process.notify_on_complete is True,
                persistence_verified=persistent, scope_verified=scope)


def register(ctx, root, reads):
    def dispatch(name, args, *, session_id, actor):
        from model_tools import handle_function_call
        # Existing native guards, approval, terminal backend and process registry.
        return handle_function_call(name, args, session_id=session_id, task_id=session_id,
            turn_id=getattr(actor, '_current_turn_id', ''),
            api_request_id=getattr(actor, '_current_api_request_id', ''),
            enabled_toolsets=getattr(actor, 'enabled_toolsets', None))
    def preflight():
        if not sys.platform.startswith('linux'): return True
        from tools.process_registry import _is_supervised_gateway_process, _systemd_run_user_scope_available
        return _is_supervised_gateway_process() and _systemd_run_user_scope_available()
    handoff = CodingHandoff(root, reads, ctx.get_config('coding_targets', {}), dispatch,
                           binary=ctx.get_config('coding_binary', '/home/hermes/.local/bin/codex'),
                           inspect_process=lambda pid, sid, actor: native_process_proof(reads, pid, sid, actor),
                           preflight=preflight)
    configured = json.dumps(handoff.capabilities(), ensure_ascii=False)
    ctx.register_tool(name=NAME, toolset='ole-dialogue', schema=dict(name=NAME,
                      description=DESCRIPTION+' Configured targets: '+configured, parameters=PARAMETERS), handler=handoff.start)
    reads.coding_handoff = handoff
    return handoff
