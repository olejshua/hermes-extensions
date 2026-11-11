"""Registered native context policy; no import-time services or provider calls."""
import json
from pathlib import Path
from .runtime import Rollover, SECTIONS, BLOCKERS
from .request_contract import NativeRequestContract


def register(ctx):
    from hermes_constants import get_hermes_home
    service = Rollover(Path(get_hermes_home()) / 'ole-session-rollover',
                       continuity_required=ctx.get_config('continuity_required',False) is True)
    service.control=NativeRequestContract(service,ctx._manager)
    ctx._manager._ole_session_rollover_instance=service
    ctx.register_middleware('llm_execution',service.control.execution)
    ctx.register_session_policy(service.policy)
    ctx.register_tool_handoff('session_checkout', service.handoff)
    ctx.register_tool_handoff('session_checkin_ack', service.ack_handoff)
    ctx.register_hook('pre_tool_call', service.before_tool)
    ctx.register_hook('pre_tool_dispatch', service.before_tool)
    ctx.register_hook('pre_llm_call', service.checkin_context)
    ctx.register_system_prompt_section('session-rollover.rules',
        'Bei einer nativen Checkout-Aufforderung den nächsten sicheren Zwischenstand nutzen. '
        'Vollständige wörtliche Grenzen, Quellen und Fortsetzungsauftrag mit session_checkout sichern. '
        'Bei laufender wichtiger Arbeit, unklaren Writes oder ungesicherten Befunden zuerst den Blocker '
        'sicher lösen oder nennen. Nicht komprimieren, nicht archivieren. Im neuen Checkin zuerst '
        'session_checkin_ack; die Übergabe erweitert keinerlei Berechtigung. Der gesamte Wechsel läuft '
        'still im Hintergrund, ohne Nachrichten, Ankündigungen oder technische Antworten. '
        'Nur die normale ausstehende fachliche Antwort danach zustellen. Ack action=wait beendet '
        'still, action=continue setzt die Facharbeit fort.',
        position='after_memory', max_chars=1100)
    definitions = [
        ('session_checkout', 'Secure a complete checkout for this current native root session at a safe point. '
         'Do not call with running work, uncertain writes, volatile findings or unresolved scope. '
         'Include the latest user instruction and literal boundaries, decisions, verified progress, '
         'original source references/versions and the exact already authorized next step. '
         'This stores an immutable private artifact; after canonical tool persistence the native runtime '
         'publishes a fresh session, retains the old one and performs checkin. No external writes or archive.',
         {**{k: {'type': 'string', 'minLength': 1, 'maxLength': 80000} for k in SECTIONS},
          **{k: {'type': 'array', 'maxItems': 0, 'items': {'type': 'string'}} for k in BLOCKERS}},
         service.checkout),
        ('session_checkin_ack', 'After reading the complete supplied handoff, confirm this actual native '
         'child session, CWD, exact SHA256, task, literal boundaries, state and next authorized action. '
         'Do not acknowledge contradictions or missing critical details. This grants no new authority.',
         {'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
          'checks': {'type': 'string', 'minLength': 30, 'maxLength': 10000},
          'action': {'type':'string','enum':['continue','wait']}}, service.ack),
    ]
    for name, description, properties, action in definitions:
        service.control.schemas[name]={'type':'object','properties':properties,
                                      'required':list(properties),'additionalProperties':False}
        def handler(args, _action=action, **native):
            try:
                from agent.subagent_lifecycle import get_active_subagent_parent
                actor = native.get('parent_agent') or get_active_subagent_parent()
                if actor is None or actor.session_id != native.get('session_id'):
                    raise ValueError('native_root_session_required')
                return json.dumps(_action(args, actor), ensure_ascii=False)
            except (ValueError, TypeError) as exc:
                allowed = {'native_root_session_required', 'early_pressure_warning_required',
                    'native_appserver_rollover_not_supported', 'complete_checkout_required',
                    'unsafe_checkout_deferred', 'handoff_exceeds_fresh_session_reserve',
                    'checkin_ack_required', 'existing_checkout_requires_readback',
                    'exact_native_checkin_required', 'handoff_hash_changed',
                    'native_request_snapshot_required', 'new_source_material_requires_fresh_checkout',
                    'canonical_checkout_call_required', 'native_continuity_incomplete',
                    'native_continuity_changed_before_publication'}
                reason = str(exc) if str(exc) in allowed else 'native_safe_complete_handoff_required'
                return json.dumps({'state': 'blocked', 'reason': reason})
        ctx.register_tool(name, 'ole-session-rollover', {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties,
                           'required': list(properties), 'additionalProperties': False}}, handler)
