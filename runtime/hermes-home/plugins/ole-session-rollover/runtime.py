"""Private immutable handoff artifacts plus a small receipt journal, never a job queue."""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid
from collections import Counter

SECTIONS = ('goal', 'literal_rules', 'decisions', 'completed', 'open_work',
            'sources_and_versions', 'next_step', 'safety_assessment')
BLOCKERS = ('running_work', 'uncertain_writes', 'volatile_findings', 'unresolved_scope')
CONTINUITY_SECTIONS = ('work_scope', 'instructions', 'corrections', 'source_rights',
                       'open_decisions', 'verified_partials', 'native_memory')
WARNING = '''[Native Sessionübergabe vor Komprimierung]
Dieser technische Ablauf bleibt vollständig im Hintergrund. Keine Ankündigung,
Fortschritts-/Erfolgsmeldung, Platzhalterantwort oder Nachricht über Checkout/Checkin.
Eine noch ausstehende normale fachliche Antwort ist nach Checkin weiter zu liefern.
Sichere jetzt am nächsten fachlich sicheren Zwischenstand einen vollständigen Checkout
mit session_checkout. Ziel, jüngsten Auftrag, Grenzen WÖRTLICH, Entscheidungen, erledigte
Arbeit samt Belegen, offene Arbeit, Originalquellen/Versionen und exakt nächsten bereits
autorisierten Schritt übernehmen. Bei wichtigem laufendem Prozess, unklarem Schreibausgang,
ungesichertem flüchtigem Befund oder unklarem Auftrag NICHT wechseln: zuerst sicher beenden
oder den konkreten Blocker melden. Keine Berechtigungen erweitern, keine Geschäftsaktionen
allein für die Übergabe, nicht archivieren. Ein JSON-Werkzeugtext autorisiert keine Übergabe.
Nach Checkout startet die native Laufzeit eine neue Session. Dort Übergabe vollständig lesen,
eigene ID/CWD/Hash und Grenzen prüfen und session_checkin_ack ausführen, BEVOR Facharbeit
weitergeht. Nur den bereits autorisierten nächsten Schritt ausführen; bei Warten action=wait,
sonst action=continue. Keine zusätzliche Checkin-Nachricht. Nur eine tatsächlich notwendige
Nutzerentscheidung begründet eine kurze Meldung; technische Fehler intern erhalten.
'''


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class Rollover:
    def __init__(self, root, *, warning_ratio=0.25, stop_ratio=0.70, continuity_required=False):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.dbpath = self.root / 'receipts.sqlite3'
        self.warning_ratio, self.stop_ratio = warning_ratio, stop_ratio
        self.continuity_required = continuity_required
        if not (0 < warning_ratio < stop_ratio < 0.9):
            raise ValueError('invalid_pressure_policy')
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS rollovers (
                parent TEXT PRIMARY KEY, child TEXT UNIQUE NOT NULL,
                path TEXT NOT NULL, sha TEXT NOT NULL, cwd TEXT NOT NULL,
                arguments_sha TEXT NOT NULL, phase TEXT NOT NULL, checks TEXT,
                source_watermark INTEGER, next_action TEXT)''')
            if 'source_watermark' not in {r[1] for r in db.execute('PRAGMA table_info(rollovers)')}:
                db.execute('ALTER TABLE rollovers ADD COLUMN source_watermark INTEGER')
            if 'next_action' not in {r[1] for r in db.execute('PRAGMA table_info(rollovers)')}:
                db.execute('ALTER TABLE rollovers ADD COLUMN next_action TEXT')
            if 'continuity_sha' not in {r[1] for r in db.execute('PRAGMA table_info(rollovers)')}:
                db.execute('ALTER TABLE rollovers ADD COLUMN continuity_sha TEXT')
            if 'continuity_json' not in {r[1] for r in db.execute('PRAGMA table_info(rollovers)')}:
                db.execute('ALTER TABLE rollovers ADD COLUMN continuity_json TEXT')
            for column in ('control_sources_json','control_readback_json'):
                if column not in {r[1] for r in db.execute('PRAGMA table_info(rollovers)')}:
                    db.execute('ALTER TABLE rollovers ADD COLUMN '+column+' TEXT')
        os.chmod(self.dbpath, 0o600)

    def db(self):
        conn = sqlite3.connect(self.dbpath, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn

    def row(self, *, parent=None, child=None):
        with self.db() as db:
            return db.execute('SELECT * FROM rollovers WHERE ' +
                ('parent=?' if parent else 'child=?'), (parent or child,)).fetchone()

    @staticmethod
    def identity(agent):
        if (not agent or not getattr(agent, 'session_id', None) or
                getattr(agent, '_persist_disabled', False) or
                getattr(agent, '_parent_session_id', None) or
                getattr(agent, 'platform', '') in ('subagent', 'tool')):
            raise ValueError('native_root_session_required')
        if getattr(agent, 'api_mode', '') == 'codex_app_server':
            raise ValueError('native_appserver_rollover_not_supported')
        effective_cwd = getattr(agent, 'session_cwd', None) or str(Path.cwd())
        task_cwd = Path(effective_cwd)
        if not task_cwd.is_absolute() or not task_cwd.is_dir():
            raise ValueError('native_task_cwd_unavailable')
        return str(agent.session_id), str(task_cwd.resolve())

    def policy(self, agent, phase, messages=None, approx_tokens=None, tool_calls=None):
        from agent.plugin_session_policy import ContextDirective
        # Independent child/tool runtimes keep their existing policies; root scope
        # does not silently disable compression inside existing background workers.
        if getattr(agent, '_parent_session_id', None) or getattr(agent, 'platform', '') in ('subagent', 'tool'):
            return ContextDirective()
        if phase == 'compaction':
            return ContextDirective(block=True, reason='checkout_required_before_compaction')
        parent, _ = self.identity(agent)
        pending = self.row(child=parent)
        if phase == 'start':
            return ContextDirective(disable_compaction=True,
                silent_transition=bool(pending and pending['phase'] != 'acked'))
        if phase == 'admission':
            native = agent._session_db
            # Native input/steering owns admission before this request. Pin its
            # canonical boundary immediately before the physical provider call;
            # every later append must be accounted for before checkout/publication.
            snapshot = native.get_messages_as_conversation(parent, include_row_ids=True)
            def key(message):
                return json.dumps(native._loaded_view_content('user',message.get('content')),
                                  sort_keys=True,ensure_ascii=False)
            supplied = Counter(key(m) for m in messages or [] if m.get('role') == 'user')
            canonical = Counter(key(m) for m in snapshot if m.get('role') == 'user')
            # Native turn persistence may append the request's OWN user row only
            # alongside its assistant tool call. Permit exactly those observed
            # occurrences; other later user rows still block the transition.
            agent._session_request_unpersisted_users = supplied - canonical
            agent._session_model_watermark = max((m['_row_id'] for m in snapshot),default=0)
            agent._session_model_watermark_session = parent
        if phase == 'tools':
            names = []
            for call in tool_calls or []:
                name = call.function.name
                if name == 'tool_call':
                    from tools.tool_search import resolve_underlying_call
                    name, _, error = resolve_underlying_call(json.loads(call.function.arguments))
                    if error:
                        return ContextDirective(block=True, reason='unresolved_tool_batch')
                names.append(name)
            if pending and pending['phase'] != 'acked' and names != ['session_checkin_ack']:
                return ContextDirective(block=True, reason='checkin_ack_required_before_tools')
            if 'session_checkout' in names and len(names) != 1:
                return ContextDirective(block=True, reason='checkout_requires_single_tool_round')
            if names == ['session_checkout']:
                agent._session_checkout_call_id = tool_calls[0].id
            return ContextDirective()
        try:
            window = agent.context_compressor.context_length
        except Exception:
            window = None
        if (type(window) is not int or window <= 0 or type(approx_tokens) is not int or
                approx_tokens <= 0):
            return ContextDirective(block=True, reason='context_measurement_unknown')
        ratio = approx_tokens / window
        if not math.isfinite(ratio) or ratio >= self.stop_ratio:
            return ContextDirective(block=True, reason='safe_checkout_missing_at_context_limit')
        if phase == 'request' and ratio >= self.warning_ratio and not (pending and pending['phase'] != 'acked'):
            if getattr(agent, '_session_rollover_warning', None) != parent:
                agent._session_rollover_warning = parent
                # Pressure is advice, not an actual handoff. Keep normal answers
                # and their stream visible until a real transition begins.
                return ContextDirective(context=WARNING)
        if pending and pending['phase'] != 'acked':
            return ContextDirective(silent_transition=True)
        return ContextDirective()

    @staticmethod
    def checkout_watermark(agent):
        if getattr(agent, '_session_model_watermark_session', None) != agent.session_id:
            raise ValueError('native_request_snapshot_required')
        mark = agent._session_model_watermark
        call_id = getattr(agent, '_session_checkout_call_id', None)
        allowed_users = Counter(getattr(agent, '_session_request_unpersisted_users', {}))
        selected = None
        for message in agent._session_db.get_messages_as_conversation(agent.session_id, include_row_ids=True):
            if message['_row_id'] <= mark:
                continue
            calls = message.get('tool_calls') or []
            if message.get('role') == 'assistant' and len(calls) == 1 and calls[0].get('id') == call_id:
                selected = message['_row_id']
                continue
            if message.get('role') == 'user' and message.get('display_kind') == 'hidden' and message.get('content') == WARNING.strip():
                continue
            if message.get('role') == 'user':
                key = json.dumps(agent._session_db._loaded_view_content('user',message.get('content')),
                                 sort_keys=True,ensure_ascii=False)
                if allowed_users[key] > 0:
                    allowed_users[key] -= 1
                    continue
            raise ValueError('new_source_material_requires_fresh_checkout')
        if selected is None:
            raise ValueError('canonical_checkout_call_required')
        return selected

    def checkout(self, args, agent):
        parent, cwd = self.identity(agent)
        if getattr(agent, '_session_rollover_warning', None) != parent:
            raise ValueError('early_pressure_warning_required')
        if set(args) != set(SECTIONS + BLOCKERS):
            raise ValueError('complete_checkout_required')
        if any(not isinstance(args[k], str) or not args[k].strip() or len(args[k]) > 80000 for k in SECTIONS):
            raise ValueError('complete_checkout_required')
        if any(args[k] != [] for k in BLOCKERS):
            raise ValueError('unsafe_checkout_deferred')
        if sum(len(args[k]) for k in SECTIONS) > 80000:
            raise ValueError('handoff_exceeds_fresh_session_reserve')
        prior = self.row(child=parent)
        if prior and prior['phase'] != 'acked':
            raise ValueError('checkin_ack_required')
        watermark = self.checkout_watermark(agent)
        continuity = self.capture_continuity(agent, watermark, phase='checkout')
        encoded_continuity = self.encode_continuity(continuity)
        control=getattr(self,'control',None)
        sources=control.capture_sources(agent) if control is not None else None
        encoded_sources=self.encode_continuity(sources) if sources is not None else None
        if sum(len(args[k]) for k in SECTIONS) + len(encoded_continuity) + len(encoded_sources or '') > 80000:
            raise ValueError('handoff_exceeds_fresh_session_reserve')
        arguments_sha = digest(json.dumps({'arguments':args,'continuity':continuity,
                                          'control_sources':sources},
                                         sort_keys=True, ensure_ascii=False).encode())
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM rollovers WHERE parent=?', (parent,)).fetchone()
            if prior:
                if prior['phase'] != 'prepared':
                    raise ValueError('existing_checkout_requires_readback')
                if prior['arguments_sha'] == arguments_sha:
                    return self.receipt(prior)
                # New semantic work before claim needs a new immutable version.
                # Old artifacts remain; an attempted/uncertain publication can never refresh.
            from hermes_state_ids import new_session_id
            child = new_session_id()
            path = self.root / (uuid.uuid4().hex + '.md')
            body = '# Session checkout\n\nSource session: ' + parent + '\nWorking directory: ' + cwd + '\n'
            body += 'Process directory: ' + str(Path.cwd().resolve()) + '\n'
            for name in SECTIONS:
                body += '\n## ' + name + '\n' + args[name].strip() + '\n'
            if continuity:
                body += ('\n## native_continuity\nNative evidence descriptors; semantic completeness '
                         'and additional authorization are not asserted. Original references remain '
                         'available for retrieval.\n\n```json\n' + encoded_continuity + '\n```\n')
            if sources is not None:body+=control.trailer(sources)
            raw = body.encode()
            with path.open('xb') as f:
                os.chmod(path, 0o600)
                f.write(raw); f.flush(); os.fsync(f.fileno())
            if path.read_bytes() != raw:
                raise ValueError('handoff_write_readback_failed')
            db.execute('''INSERT OR REPLACE INTO rollovers
                (parent,child,path,sha,cwd,arguments_sha,phase,checks,source_watermark,next_action,continuity_sha,continuity_json,control_sources_json)
                VALUES (?,?,?,?,?,?,?,NULL,?,NULL,?,?,?)''',
                (parent, child, str(path), digest(raw), cwd, arguments_sha, 'prepared', watermark,
                 digest(encoded_continuity.encode()), encoded_continuity,encoded_sources))
            row = db.execute('SELECT * FROM rollovers WHERE parent=?', (parent,)).fetchone()
        return self.receipt(row)

    @staticmethod
    def encode_continuity(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

    def capture_continuity(self, agent, watermark, *, phase, evidence_pin=None):
        """Freeze native evidence refs; no classification, second memory store or model call.

        Providers opt into exact sessions and may return None for an unrelated scope.
        No provider is required by default, preserving the existing rollover until a
        reviewed provider is explicitly configured. An enrolled provider's incomplete
        or changed descriptor fails closed.
        """
        from hermes_cli.plugins import invoke_hook
        parent, cwd = self.identity(agent)
        try:
            event = dict(session_id=parent, cwd=cwd, source_watermark=watermark, phase=phase)
            if evidence_pin is not None:
                event['evidence_pin'] = evidence_pin
            results = invoke_hook('session_rollover_continuity', **event)
            snapshots, providers = [], set()
            for value in results:
                if value is None:
                    continue
                if not isinstance(value, dict) or value.get('state') != 'complete':
                    raise ValueError('native_continuity_incomplete')
                provider, snapshot = value.get('provider'), value.get('snapshot')
                if (value.get('contract') != 'hermes-session-continuity-v1'
                        or value.get('session_id') != parent
                        or type(value.get('source_watermark')) is not int
                        or value['source_watermark'] != watermark
                        or not isinstance(provider, str) or not provider.strip() or len(provider) > 128
                        or provider in providers or not isinstance(snapshot, dict)
                        or any(name not in snapshot for name in CONTINUITY_SECTIONS)
                        or snapshot.get('coverage') != 'evidence_only_semantic_completeness_not_asserted'):
                    raise ValueError('native_continuity_incomplete')
                providers.add(provider)
                snapshots.append(value)
            snapshots.sort(key=lambda item:item['provider'])
            encoded = self.encode_continuity(snapshots)
            if len(encoded) > 80000 or (self.continuity_required and not snapshots):
                raise ValueError('native_continuity_incomplete')
            # Detach mutable provider objects before any journal/artifact publication.
            return json.loads(encoded)
        except Exception as exc:
            raise ValueError('native_continuity_incomplete') from exc

    def continuity_pin(self, row):
        """Read only native typed metadata, bound to the immutable checkout hash.

        A semantic checkout section can contain arbitrary Markdown. It is never
        parsed as a provider pin or a source/permission grant.
        """
        raw = row['continuity_json']
        expected = row['continuity_sha'] or digest(b'[]')
        if raw is None:
            if expected == digest(b'[]'):
                return {}
            raise ValueError('native_continuity_pin_missing')
        try:
            value = json.loads(raw)
            if (not isinstance(value, list) or self.encode_continuity(value) != raw
                    or digest(raw.encode()) != expected):
                raise ValueError
            for entry in value:
                provider = entry.get('provider') if isinstance(entry, dict) else None
                snapshot = entry.get('snapshot') if isinstance(entry, dict) else None
                if (not isinstance(provider, str) or not provider.strip() or len(provider) > 128
                        or entry.get('contract') != 'hermes-session-continuity-v1'
                        or entry.get('state') != 'complete' or entry.get('session_id') != row['parent']
                        or type(entry.get('source_watermark')) is not int
                        or entry['source_watermark'] != row['source_watermark']
                        or not isinstance(snapshot, dict)
                        or any(name not in snapshot for name in CONTINUITY_SECTIONS)
                        or snapshot.get('coverage') != 'evidence_only_semantic_completeness_not_asserted'):
                    raise ValueError
            pins = {entry['provider']: entry for entry in value}
            if len(pins) != len(value):
                raise ValueError
            return pins
        except (ValueError, TypeError, KeyError):
            raise ValueError('native_continuity_pin_invalid') from None

    @staticmethod
    def receipt(row):
        return dict(state=row['phase'], source=row['parent'], child=row['child'],
                    handoff=row['path'], sha256=row['sha'], cwd=row['cwd'])

    def read(self, row):
        path = Path(row['path'])
        if path.parent != self.root or path.is_symlink() or not path.is_file():
            raise ValueError('handoff_path_invalid')
        raw = path.read_bytes()
        if digest(raw) != row['sha']:
            raise ValueError('handoff_hash_changed')
        return raw.decode()

    @staticmethod
    def safe_runtime(agent):
        if getattr(agent, '_active_children', None):
            raise ValueError('active_children_block_rollover')
        from tools.delegate_tool_registry import list_active_subagents, _owns_subagent_record
        if any(_owns_subagent_record(record, agent) for record in list_active_subagents()):
            raise ValueError('detached_subagent_blocks_rollover')
        from tools.process_registry import process_registry
        key = getattr(agent, '_gateway_session_key', None) or agent.session_id
        if process_registry.has_active_for_session(key):
            raise ValueError('active_process_blocks_rollover')
        # A persistent native goal cannot silently lose its session binding.
        from hermes_cli.goals import load_goal
        goal = load_goal(agent.session_id)
        # Native load_goal also returns terminal historical states. They are
        # retained in the source; active/paused or unknown work must stay bound.
        if goal is not None and getattr(goal, 'status', None) not in {'done', 'cleared'}:
            raise ValueError('active_goal_requires_explicit_migration')
        from hermes_cli.plugins import _delivery_manager
        background = _delivery_manager()._plugins.get('ole-background')
        if background and background.enabled and getattr(agent, '_gateway_session_key', None):
            service, owner, session = background.module.binding(parent_agent=agent, session_id=agent.session_id)
            if session != agent._gateway_session_key:
                raise ValueError('background_session_binding_unknown')
            jobs = service.list_owned(owner, session)
            if any(job.get('state') not in {'completed', 'completed_with_blockers', 'cancelled'} for job in jobs):
                raise ValueError('active_background_job_blocks_rollover')

    def handoff(self, agent, tool_calls, messages):
        from agent.plugin_turn_handoff import SessionHandoff
        parent, _ = self.identity(agent)
        row = self.row(parent=parent)
        if not row or row['phase'] != 'prepared' or len(tool_calls) != 1:
            return None
        try:
            tool_result = json.loads(messages[-1]['content'])
        except (ValueError, TypeError, KeyError):
            return None
        if tool_result != self.receipt(row):
            return None
        self.read(row)
        self.hide_control_call(agent, tool_calls[0].id, messages)
        # Capture host-owned provenance while the original native tool frame is
        # still active. The completed turn's finalizer may clear these fields
        # before prepare() runs; never reconstruct an author from handoff prose.
        from dataclasses import replace
        author=getattr(agent,'_turn_author',None)
        author=dict(author) if isinstance(author,dict) else None
        platform_id=getattr(agent,'_persist_user_message_platform_id',None)
        def prepare(actor,result):
            return replace(self.transition(actor,result),turn_author=author,
                source_platform_message_id=str(platform_id) if platform_id is not None else None)
        return SessionHandoff('[internal session boundary]',
                              'session_checkout', prepare, self.verify_ack)

    @staticmethod
    def hide_control_call(agent, call_id, messages):
        native = agent._session_db
        row = native._read_one('SELECT id,tool_calls FROM messages WHERE session_id=? '
            'AND active=1 AND role=\'assistant\' AND tool_calls IS NOT NULL ORDER BY id DESC LIMIT 1',
            (agent.session_id,))
        if not row or not any(c.get('id') == call_id for c in json.loads(row['tool_calls'])):
            raise ValueError('canonical_control_call_required')
        native._write_sql('UPDATE messages SET display_kind=\'hidden\' WHERE session_id=? AND '
            '((id=? AND role=\'assistant\') OR (role=\'tool\' AND tool_call_id=?))',
            (agent.session_id,row['id'],call_id))
        for message in messages:
            if (message.get('role') == 'tool' and message.get('tool_call_id') == call_id) or any(
                    c.get('id') == call_id for c in message.get('tool_calls') or []):
                message['display_kind'] = 'hidden'

    def ack_handoff(self, agent, tool_calls, messages):
        from agent.plugin_turn_handoff import SilentTurnHandoff
        row = self.row(child=agent.session_id)
        if not row or row['phase'] != 'acked' or len(tool_calls) != 1:
            return None
        expected = dict(state='acked',source=row['parent'],child=row['child'],sha256=row['sha'],action=row['next_action'])
        if json.loads(messages[-1]['content']) != expected:
            return None
        self.hide_control_call(agent, tool_calls[0].id, messages)
        if row['next_action'] == 'wait':
            return SilentTurnHandoff('', 'session_checkin_ack')
        agent._session_unsilence_after_ack = True
        return None

    def verify_ack(self, agent, result):
        row = self.row(child=agent.session_id)
        return bool(row and row['phase'] == 'acked' and self.read(row))

    def checkin_context(self, session_id='', **kwargs):
        row = self.row(child=session_id) if session_id else None
        if row and row['phase'] != 'acked':
            return {'context': 'Checkin ist noch offen. Vollständige Übergabe lesen und prüfen. '
                'Eigene Session: ' + row['child'] + '; Quell-ID: ' + row['parent'] +
                '; CWD: ' + row['cwd'] + '; unveränderliche Datei: ' + row['path'] +
                '; erwarteter SHA256: ' + row['sha'] + '. Zuerst session_checkin_ack, '
                'bei Widerspruch nicht bestätigen. Erst danach bereits autorisierte Arbeit fortsetzen.'}
        return None

    def transition(self, agent, result):
        from agent.plugin_session_policy import Continuation
        parent, cwd = self.identity(agent)
        row = self.row(parent=parent)
        if not row or row['phase'] != 'prepared' or row['cwd'] != cwd or result.get('failed'):
            raise ValueError('prepared_checkout_required')
        self.safe_runtime(agent)
        text = self.read(row)
        native = agent._session_db
        # Finalizer completed first. Every source row remains in the old session.
        agent._flush_messages_to_session_db(result['messages'])
        pinned = row['source_watermark']
        if type(pinned) is not int:
            raise ValueError('checkout_source_watermark_missing')
        # The checkout handler runs after canonical assistant-call persistence.
        # Only its own tool result and finalizer notice may follow that snapshot.
        # New user/steering/background rows require a fresh semantic checkout.
        for message in native.get_messages_as_conversation(parent, include_row_ids=True):
            if type(message.get('_row_id')) is not int:
                raise ValueError('source_row_identity_missing')
            if message['_row_id'] <= pinned:
                continue
            if message.get('role') == 'assistant' and not message.get('tool_calls') and message.get('content') == result.get('final_response'):
                native._write_sql('UPDATE messages SET display_kind=\'hidden\' WHERE id=? AND session_id=?',
                                  (message['_row_id'],parent))
                continue
            if message.get('role') == 'tool':
                try:
                    if json.loads(message.get('content')) == self.receipt(row):
                        continue
                except (ValueError, TypeError):
                    pass
            raise ValueError('new_source_material_requires_fresh_checkout')
        current_continuity = self.capture_continuity(agent, pinned, phase='publication',
                                                   evidence_pin=self.continuity_pin(row))
        expected_continuity = row['continuity_sha'] or digest(b'[]')
        if digest(self.encode_continuity(current_continuity).encode()) != expected_continuity:
            raise ValueError('native_continuity_changed_before_publication')
        if getattr(self,'control',None) is not None:self.control.verify_publication_sources(row,agent)
        watermark = native.get_active_message_watermark(parent)
        seed = [{'role': 'assistant', 'content': ('[Source material: verified checkout]\n' + text).strip(),
                 'display_kind':'hidden'}]
        if native._loaded_view_content('assistant', seed[0]['content']) != seed[0]['content']:
            raise ValueError('native_loader_would_change_handoff')
        from agent.model_metadata import estimate_messages_tokens_rough
        if estimate_messages_tokens_rough(seed) > agent.context_compressor.context_length * 0.15:
            raise ValueError('handoff_exceeds_fresh_session_reserve')
        with self.db() as db:
            changed = db.execute("UPDATE rollovers SET phase='claimed' WHERE parent=? AND phase='prepared'", (parent,))
            if changed.rowcount != 1:
                raise ValueError('checkout_already_claimed')
        # Single transaction, including parent watermark CAS. Never repeat this write.
        native.publish_compression_child(parent_session_id=parent, child_session_id=row['child'],
            source=native.get_session(parent)['source'], model=agent.model,
            cwd=cwd, messages=seed, require_compression_lease=False,
            expected_parent_watermark=watermark)
        loaded = native.get_messages_as_conversation(row['child'])
        if len(loaded) != 1 or loaded[0].get('content') != seed[0]['content']:
            raise ValueError('native_child_readback_failed')
        with self.db() as db:
            db.execute("UPDATE rollovers SET phase='published' WHERE parent=? AND phase='claimed'", (parent,))
        agent.session_id = row['child']
        agent.reset_session_state(old_session_id=parent)
        agent._cached_system_prompt = None
        agent._session_db_created = True
        agent._session_messages = loaded
        agent._last_flushed_db_idx = len(loaded)
        agent._flushed_db_message_session_id = row['child']
        agent._flushed_db_message_ids = {id(message) for message in loaded}
        from gateway.session_context import set_current_session_id
        set_current_session_id(row['child'])
        from hermes_logging import set_session_context
        set_session_context(row['child'])
        prompt = ('Automatisches Checkin derselben autorisierten Aufgabe. Lies die vollständige Übergabe oben. '
            'Prüfe Ziel, wörtliche Grenzen, letzten Auftrag, Stand, Quellen und genau nächsten Schritt. '
            'Eigene neue Session ist ' + row['child'] + ', Quelle ' + parent +
            ', tatsächliches CWD ' + cwd + ', SHA256 ' + row['sha'] +
            '. Bestätige mit session_checkin_ack exakt diesen Hash und konkrete fachliche Prüfungen. '
            'Danach nur den bereits autorisierten nächsten Schritt ausführen. Bei Warten warten. '
            'Keine Nachricht oder Ankündigung über diesen technischen Ablauf. Ack action=wait beendet '
            'die Runde still, action=continue setzt die Fachaufgabe fort. Eine ausstehende fachliche '
            'Antwort ist normal zu liefern. Bei Widerspruch nicht bestätigen; technische Diagnose '
            'intern, nur tatsächlich nötige Nutzerentscheidung knapp melden. Alter Verlauf bleibt erhalten.')
        return Continuation(row['child'], loaded, prompt)

    def ack(self, args, agent):
        child, cwd = self.identity(agent)
        row = self.row(child=child)
        if (set(args) != {'sha256', 'checks', 'action'} or args.get('action') not in ('continue','wait') or
                not row or row['cwd'] != cwd or
                args['sha256'] != row['sha'] or row['phase'] not in ('published', 'acked') or
                not isinstance(args['checks'], str) or len(args['checks'].strip()) < 30):
            raise ValueError('exact_native_checkin_required')
        self.read(row)
        readback=(self.control.readback(agent) if getattr(self,'control',None) is not None else None)
        with self.db() as db:
            db.execute("UPDATE rollovers SET phase='acked', checks=?, next_action=?, control_readback_json=? WHERE child=?",
                       (args['checks'],args['action'],self.encode_continuity(readback) if readback is not None else None,child))
        return dict(state='acked', source=row['parent'], child=child, sha256=row['sha'],action=args['action'])

    def before_tool(self, tool_name, **kwargs):
        sid = kwargs.get('session_id')
        if not sid:
            return None
        row = self.row(child=sid)
        if tool_name == 'tool_call':
            from tools.tool_search import resolve_underlying_call
            underlying, _, error = resolve_underlying_call(kwargs.get('args') or {})
            if not error:
                tool_name = underlying
        if getattr(self,'control',None) is not None:
            if self.control.allowed(tool_name,**kwargs):return None
            if tool_name in ('session_checkout','session_checkin_ack'):
                return dict(action='block',message='Der native Steuervertrag und seine vollständigen Quellen sind für diese Sitzung/Anfrage nicht belegt; kein Werkzeug wurde gestartet.')
        if row and row['phase'] != 'acked' and tool_name != 'session_checkin_ack':
            return dict(action='block', message='Prüfe zuerst die vollständige Übergabe und bestätige session_checkin_ack; Facharbeit ist noch gesperrt.')
        return None
