"""Opt-in first human inference; same model/context, read-only fast boundary.

No language classifier or extra router call. The main model answers normally or
uses native tools; every later API request keeps the configured thorough effort.
"""
from copy import deepcopy
import hashlib
import json
import re
import threading
import weakref

NAME = 'dialogue_thorough'
PARAMETERS = {'type': 'object', 'properties': {}, 'additionalProperties': False}
SAFE = frozenset(('dialogue_read', 'dialogue_read_page', 'skill_view', 'read_file', 'search_files', NAME))
DEFAULT_CASUAL_SKIP_KEYWORDS = ('acribic', 'stealth')
CASUAL_SKIP_MAX_CHARS = 160
# Positive smalltalk whitelist: the absence of project keywords alone never
# makes a message casual. A short question or instruction ("verschieb den
# Termin auf 15 Uhr") keeps the complete preload even without a keyword.
_CASUAL_UNIT = (
    r'(?:guten (?:morgen|tag|abend)|gute nacht|vielen dank|'
    r'danke(?: (?:dir|schön|sehr|vielmals))*|'
    r'alles klar|bis (?:später|bald|morgen|gleich|dann)|wie geht.?s(?: dir)?|'
    r'mach.?s gut|schönen? (?:tag|abend|feierabend)|kein problem|keine sorge|'
    r'passt(?: so)?|einverstanden|verstanden|liebe grüße|'
    r'hallo|hi+|hey+|moin+|servus|na|ok(?:ay)?|ja|jap|nein|nee|gut|super|'
    r'perfekt|toll|klar|bitte|gerne|ciao|tschüss|lg)')
_CASUAL_PHRASES = re.compile(r'^' + _CASUAL_UNIT + r'(?: ' + _CASUAL_UNIT + r')*$')
_CASUAL_EDGE = re.compile(r'[\W_\d]+', re.UNICODE)


def casual_candidate(message, keywords=DEFAULT_CASUAL_SKIP_KEYWORDS):
    """A clearly casual short message needs no router run or skill preload.

    The fast first inference already defers every executing tool to the
    thorough continuation, so skipping the project-context preload for
    smalltalk removes its latency without touching any source or permission
    gate. Casual means a positive whitelist match (greeting, thanks,
    acknowledgement, pure emoji); anything long, multi-line, naming a project
    or carrying a question or instruction keeps the complete preload
    unchanged, even when it contains no keyword.
    """
    if not isinstance(message, str):
        return False
    text = message.strip()
    if not text or len(text) > CASUAL_SKIP_MAX_CHARS or '\n' in text:
        return False
    low = text.casefold()
    if any(isinstance(word, str) and word and word.casefold() in low for word in keywords):
        return False
    normalized = ' '.join(_CASUAL_EDGE.sub(' ', low).split())
    if not normalized:
        return True  # pure emoji/punctuation reaction
    return _CASUAL_PHRASES.match(normalized) is not None


GUIDANCE = '''\n\n[Current human conversation request]
All preceding rules, memory, original history and permissions still apply in full.
Answer the actual question directly and naturally if the available context supports
a complete reliable answer. No greeting/receipt/progress text as a substitute.
If facts require current sources, use the appropriate source tool; subsequent
reasoning runs with the configured thorough setting. If the question requires a
careful multi-step investigation or a deliberately deep
analysis without a source tool yet, use tool_call calls:[{"name":"dialogue_thorough",
"arguments":{}}] first. Do not guess unavailable facts or reduce a requested thorough
task to a superficial reply. Executing tools are deferred until thorough reasoning;
original authorization, source requirements and user corrections remain mandatory.
'''


class FastFirst:
    def __init__(self, reads, *, enabled=False, effort='low', source_prefetch=False,
                 casual_skip=True, casual_keywords=DEFAULT_CASUAL_SKIP_KEYWORDS):
        self.reads, self.enabled, self.effort = reads, enabled is True, effort
        self.source_prefetch = source_prefetch is True
        self.casual_skip = casual_skip is True
        self.casual_keywords = tuple(casual_keywords)
        self.lock = threading.RLock()
        self.rows = weakref.WeakKeyDictionary()
        self.contexts = weakref.WeakKeyDictionary()
        self.metrics = []
        self.context_metrics = []
        self.manager = None

    def capture_message(self, *, session_id='',turn_id='',user_message=None,**_):
        if not self.enabled or not isinstance(user_message,str):
            return None
        try:
            actor=self.actor(session_id,turn_id)
        except ValueError:
            return None
        if self.casual_skip and casual_candidate(user_message, self.casual_keywords):
            source=dict(read_only=True,complete=True,routing_executed=False,casual_skip=True,
                        task_sha256=hashlib.sha256(user_message.encode()).hexdigest(),
                        reason='casual_message_preload_skipped')
        else:
            source=(self.reads.project_context.read(user_message,include_originals=True) if self.source_prefetch
                    else self.reads.project_context.read(user_message))
        from .project_context import preload_text
        with self.lock:
            self.contexts[actor]=dict(turn=turn_id,task=user_message,source=source,text=preload_text(source))
            self.context_metrics.append({k:source.get(k) for k in ('routing_executed','task_sha256',
                'router_sha256','skill_sha256','missing_required_files','elapsed_seconds','error')})
            self.context_metrics=self.context_metrics[-128:]
        return None

    @staticmethod
    def routed_request(request,context):
        items=request.get('input')
        if not isinstance(items,list):
            return None
        for index in range(len(items)-1,-1,-1):
            item=items[index]
            if not isinstance(item,dict) or item.get('role')!='user':
                continue
            content=item.get('content')
            if isinstance(content,str):
                text=content
                revised_content=content+context['text']
            elif isinstance(content,list):
                text=''.join(part.get('text','') for part in content if isinstance(part,dict)
                             and part.get('type') in ('input_text','text') and isinstance(part.get('text'),str))
                revised_content=[*content,dict(type='input_text',text=context['text'])]
            else:
                continue
            if text.startswith(context['task']):
                revised_items=list(items)
                revised_items[index]=dict(item,content=revised_content)
                return dict(request,input=revised_items)
        return None

    def actor(self, session_id, turn_id):
        actor = self.reads._actor(session_id, turn_id)
        from gateway.session_context import get_session_env
        author = getattr(actor, '_turn_author', None)
        if (getattr(actor, '_parent_session_id', None)
                or getattr(actor, 'platform', '') != 'telegram'
                or get_session_env('HERMES_CRON_SESSION') == '1'
                or getattr(actor, '_mute_notification_reply', False)
                or getattr(actor, '_session_transition_silent', False)
                or not isinstance(author, dict) or author.get('is_bot') is not False
                or str(author.get('id')) != str(self.reads.dialogue.policy.user_id)
                or getattr(actor, 'provider', '') != 'openai-codex'
                or getattr(actor, 'api_mode', '') != 'codex_responses'):
            raise ValueError('human_main_codex_route_required')
        barrier = self.reads.dialogue.steering
        with barrier.lock:
            routes = list(barrier.routes.items())
        with self.reads.dialogue.active_lock:
            for key, route in routes:
                active = self.reads.dialogue.active.get(key)
                if (active and not active.get('finished')
                        and active.get('message') == route.get('message_id')
                        and str(getattr(actor, '_persist_user_message_platform_id', '')) == str(active['message'])
                        and barrier.current_inbound(key, actor, session_id) == active['message']):
                    return actor
        raise ValueError('active_human_message_required')

    def request(self, *, request=None, session_id='', turn_id='', api_request_id='', api_call_count=None, **_):
        if (not self.enabled or not session_id or not turn_id
                or not api_request_id or not isinstance(request, dict)):
            return None
        try:
            actor = self.actor(session_id, turn_id)
        except ValueError:
            return None
        if request.get('model') != actor.model or not isinstance(request.get('instructions'),str):
            return None
        with self.lock:
            context=self.contexts.get(actor)
        if not context or context['turn']!=turn_id:
            return None
        # Native middleware callbacks each receive the original request. Compose
        # Reads explicitly before this final replacement rather than overwrite it.
        exposed=self.reads.expose_read_contract(request=request,session_id=session_id,
            turn_id=turn_id,api_request_id=api_request_id,api_call_count=api_call_count,**_)
        if isinstance(exposed,dict) and isinstance(exposed.get('request'),dict):
            request=exposed['request']
        revised=self.routed_request(request,context)
        if revised is None:
            return None
        source=context['source']
        service=getattr(self.manager,'_ole_session_rollover_instance',None)
        control=getattr(service,'control',None)
        if control is not None and control.phase(actor,session_id,turn_id) is not None:
            return dict(request=revised,source='ole-dialogue',reason='native_control_keeps_configured_thorough')
        if (api_call_count != 1 or self.effort != 'low'
                or not isinstance(request.get('reasoning'),dict)
                or request['reasoning'].get('effort')!='high'
                or (source.get('routing_executed') is not True and source.get('casual_skip') is not True)
                or source.get('missing_required_files')):
            with self.lock:
                previous = self.rows.get(actor)
                metadata_read = previous and previous['turn'] == turn_id and previous.get('local_metadata_read')
            if api_call_count != 1 and metadata_read:
                revised['instructions'] += ('\nThe current native request already uses the configured thorough effort. '
                    'The preceding metadata request was permitted only for known static local schemas. Use the needed native '
                    'status/action or answer now; do not call dialogue_thorough merely to switch effort again. '
                    'All original authorization and source checks still apply.\n')
            return dict(request=revised,source='ole-dialogue',reason='native_project_context_only')
        with self.lock:
            row = self.rows.get(actor)
            if row is not None and row['turn'] == turn_id:
                if row['request'] != api_request_id:
                    return None
            else:
                # Plugin reload/eviction cannot make a long active turn fast again.
                if getattr(actor, '_ole_dialogue_first_main_seen', None) == turn_id:
                    return None
                actor._ole_dialogue_first_main_seen = turn_id
                row = {'session': session_id, 'turn': turn_id, 'request': api_request_id}
                self.rows[actor] = row
        revised['reasoning'] = dict(request['reasoning'], effort=self.effort)
        revised['instructions'] += GUIDANCE
        tools = deepcopy(request.get('tools', []))
        for tool in tools:
            function = tool.get('function', tool)
            if function.get('name') == 'tool_call':
                function['description'] = ('For an explicitly thorough/complex investigation you can continue '
                    'with the configured thorough reasoning using calls:[{"name":"dialogue_thorough","arguments":{}}]. '
                    'Ordinary supported questions should be answered directly. '+function.get('description', ''))
        revised['tools'] = tools
        with self.lock:
            self.metrics.append(dict(api_request_id=api_request_id, before_effort='high', after_effort=self.effort,
                                     model_unchanged=True, history_unchanged=True, tools_count=len(tools)))
            self.metrics = self.metrics[-128:]
        return dict(request=revised, source='ole-dialogue', reason='first_human_inference_only')

    def local_metadata(self, args, actor):
        # Native Tool Search contract is exactly {"names": [string, ...]}.
        # Only fixed local names, static registrations, current native actor scope.
        known = {'dialogue_thorough': 'ole-dialogue', **{
            'project_work_' + action: 'ole-project-work'
            for action in ('start', 'status', 'steer', 'pause', 'resume', 'result')}}
        if not isinstance(args, dict) or set(args) != {'names'}:
            return False
        names = args['names']
        if (not isinstance(names, list) or not 1 <= len(names) <= len(known)
                or any(not isinstance(name, str) or name not in known for name in names)
                or len(set(names)) != len(names)):
            return False
        manager = getattr(self, 'manager', None)
        selected = getattr(actor, 'enabled_toolsets', None)
        if manager is None or not isinstance(selected, (list, tuple, set, frozenset)):
            return False
        try:
            from tools.registry import registry
            for name in names:
                entry = registry.get_entry(name, scope=manager.scope_key)
                if (known[name] not in selected or entry is None or entry.toolset != known[name]
                        or getattr(entry, 'dynamic_schema_overrides', None) is not None
                        or not isinstance(entry.schema, dict) or entry.schema.get('name') != name):
                    return False
            return True
        except Exception:
            return False

    def before_dispatch(self, **event):
        return self.before_tool(_metadata_dispatch=True, **event)

    def before_tool(self, *, tool_name='', args=None, session_id='', turn_id='', api_request_id='', tool_call_id='', _metadata_dispatch=False, **_):
        if not self.enabled:
            return None
        request_id = api_request_id
        if not request_id:
            try:
                actor = self.reads._actor(session_id, turn_id)
                request_id = getattr(actor, '_current_api_request_id', '')
            except ValueError:
                pass
        with self.lock:
            fast = any(row['session'] == session_id and row['turn'] == turn_id
                       and row['request'] == request_id for row in self.rows.values())
        if not fast:
            return None
        # A final native control contract has already restored the configured
        # thorough request and bound its exact phase/source. This exception
        # admits only its technical tool, never an ordinary business action.
        service=getattr(self.manager,'_ole_session_rollover_instance',None)
        control=getattr(service,'control',None)
        if control is not None and control.allowed(tool_name,args=args,session_id=session_id,
                turn_id=turn_id,api_request_id=request_id,tool_call_id=tool_call_id):
            return None
        if tool_name == 'tool_describe':
            try:
                actor = self.actor(session_id, turn_id)
            except Exception:
                actor = None
            if actor is not None and isinstance(tool_call_id, str) and tool_call_id:
                with self.lock:
                    row = self.rows.get(actor)
                    if (row is not None and row['session'] == session_id and row['turn'] == turn_id
                            and row['request'] == request_id):
                        seals = row.setdefault('local_metadata_seals', {})
                        stamp = (session_id, turn_id, request_id,
                                 str(getattr(actor, '_persist_user_message_platform_id', '')))
                        if _metadata_dispatch:
                            seal = seals.pop(tool_call_id, None)
                            permitted = (isinstance(seal, dict) and seal.get('stamp') == stamp
                                and self.local_metadata({'names': seal['names']}, actor))
                            if permitted:
                                row['local_metadata_read'] = True
                        else:
                            seals.pop(tool_call_id, None)
                            permitted = len(seals) < 64 and self.local_metadata(args, actor)
                            if permitted:
                                seals[tool_call_id] = {'stamp': stamp, 'names': list(args['names'])}
                        if permitted:
                            return None
        names = [tool_name]
        if tool_name == 'tool_call':
            calls = args.get('calls') if isinstance(args, dict) else None
            if not isinstance(calls, list) or not calls:
                names = ['unknown']
            else:
                names = [c.get('name') if isinstance(c, dict) else None for c in calls]
        if any(not isinstance(name, str) or name not in SAFE for name in names):
            return dict(action='block', message='Die gründliche Fortsetzung ist jetzt erforderlich. '
                'Dieser Werkzeugaufruf wurde nicht ausgeführt. Prüfe den unveränderten ursprünglichen '
                'Auftrag, sämtliche Regeln, Quellen und Freigaben im nächsten Modellschritt; '
                'behaupte keinen Start, gespeicherten Entwurf oder ausgeführte Änderung.')
        return None

    def thorough(self, args, *, session_id='', parent_agent=None, **_):
        actor = self.reads._actor(session_id, expected=parent_agent)
        self.actor(session_id, getattr(actor, '_current_turn_id', ''))
        if args != {}:
            return json.dumps(dict(continued=False, error='empty_arguments_required'))
        return json.dumps(dict(continued=True, current_request_preserved=True,
            next_inference='configured_thorough_effort', sources_and_permissions_unchanged=True))

    def unload(self):
        with self.lock:
            self.rows.clear()
            self.contexts.clear()


def register(ctx, reads):
    if ctx.get_config('fast_first_enabled', False) is not True:
        return None
    keywords = ctx.get_config('fast_first_casual_skip_keywords', None)
    if (not isinstance(keywords, (list, tuple)) or not keywords
            or any(not isinstance(word, str) or not word.strip() for word in keywords)):
        keywords = DEFAULT_CASUAL_SKIP_KEYWORDS
    fast = FastFirst(reads, enabled=True,source_prefetch=ctx.get_config('source_prefetch_enabled',False),
                     casual_skip=ctx.get_config('fast_first_casual_skip',True) is not False,
                     casual_keywords=keywords)
    fast.manager = ctx._manager
    ctx.register_tool(name=NAME, toolset='ole-dialogue', schema=dict(name=NAME,
        description='Continue the original current human request with configured thorough reasoning. '
                    'Use for a deliberately deep or complex investigation; no execution, reset, summary or new worker.',
        parameters=PARAMETERS), handler=fast.thorough)
    ctx.register_middleware('llm_request', fast.request)
    ctx.register_hook('pre_llm_call',fast.capture_message)
    ctx.register_hook('pre_tool_call', fast.before_tool)
    ctx.register_hook('pre_tool_dispatch', fast.before_dispatch)
    ctx.on_unload(fast.unload)
    reads.fast_first = fast
    return fast
