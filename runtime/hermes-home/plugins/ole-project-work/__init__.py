"""Native project tools; reviewed private policy and packages required."""
import json
import os
from pathlib import Path
import sys
from .runtime import ProjectPlugin


def load_service(policy_path):
    path = Path(policy_path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('reviewed_policy_required')
    if any(p.is_symlink() for p in path.parents) or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('private_policy_required')
    policy = json.loads(path.read_bytes())
    package = Path(policy['engine_package'])
    if not package.is_absolute() or not package.is_dir() or any(p.is_symlink() for p in (package, *package.parents)):
        raise ValueError('trusted_package_required')
    sys.path.insert(0, str(package))
    from ole_project_work import ProjectService
    return ProjectService(policy), policy


def register(ctx):
    service, policy = load_service(ctx.get_config('policy_path', '/home/hermes/.hermes/ole-project-work/policy.json'))
    instance = ProjectPlugin(ctx, service, policy['telegram_state_root'])
    identifier = {'type': 'string', 'maxLength': 128}
    schemas = {
        'start': ({'project': {'type': 'string', 'enum': ['acribic', 'monolith']}, 'goal': {'type': 'string', 'minLength': 1, 'maxLength': 16000}, 'task_id': {'type': 'string', 'format': 'uuid', 'description': 'Optional exact existing Monolith task ID from a current read; proposes a linked Monolith assignment. Work starts only after human approval and execution in Monolith, using its frozen full snapshot. Does not change or complete that task.'}}, ['project', 'goal']),
        'status': ({'job_id': identifier}, []),
        'steer': ({'job_id': identifier, 'instruction': {'type': 'string', 'minLength': 1, 'maxLength': 16000}, 'expected_revision': {'type': 'integer', 'minimum': 0}}, ['job_id', 'instruction', 'expected_revision']),
        'pause': ({'job_id': identifier}, ['job_id']),
        'resume': ({'job_id': identifier}, ['job_id']),
        'result': ({'job_id': identifier}, ['job_id']),
    }
    descriptions = {
        'start': 'Start an explicitly authorized file-based project goal durably. project=acribic preserves all Acribic source rules; project=monolith requires task_id and the real approved Monolith task/project snapshot. No unlinked generic job. First read ole-project-work:project-work. Current approved project sources are engine-pinned. No mail/calendar/CRM writes. For work on an existing Monolith task, first resolve its exact task_id and pass it here. The linked intent is stored immediately without network/inference. Monolith must approve AND execute it before the worker starts; report awaiting approval honestly. The approved snapshot includes full notes/description/subtasks. Lost HTTP or restarts reuse saved request, claim, Engine job and report identities; never silently start unlinked or invent another ID. File/status publication is independently read back; acceptance is not completion. Return after acceptance, do not poll or wait. Ordinary questions are not work orders.',
        'status': 'Read current owned project job metadata directly; omitting job_id lists this exact private session. No worker/model call. Before a correction read this exact job revision in the current turn.',
        'steer': 'Apply only an explicit correction from the exact current authenticated human message to the concrete job just read. instruction must equal the whole current human message. Questions/explanation requests are not corrections. If job reference is unclear ask first; never redirect another job or contact. expected_revision must match current read. Preserves original scope and permissions.',
        'pause': 'Request interruption of this exact owned project. Does not claim process has stopped until status proves interruption.',
        'resume': 'Resume this same owned project with saved verified work after interruption; preserves scope, budget and revisions.',
        'result': 'Read verified current owned project result with engine-bound artifacts. Document delivery occurs only after current hashes/revision/auth checks; status includes per-artifact delivery receipts. Never treat a server path as a delivered file.',
    }
    for action, (properties, required) in schemas.items():
        name = 'project_work_' + action
        def handler(args, _action=action, **native):
            return instance.safe_call(_action, args, **native)
        ctx.register_tool(name, 'ole-project-work', dict(name=name, description=descriptions[action], parameters=dict(type='object', properties=properties, required=required, additionalProperties=False)), handler)
    ctx.register_skill('project-work', Path(__file__).parent / 'skills/project-work/SKILL.md', 'Acribic-Projektarbeit dauerhaft auf dem Server starten, korrigieren und als geprüfte Dateien erhalten; normal weiterreden.')
    ctx.register_system_prompt_section('ole-project-work.entry',
        'Für ausdrücklich beauftragte dateibasierte Konzept-/Analyse-/Projektarbeit im Hintergrund nutze ole-project-work:project-work und project_work_start. Für vorhandene Monolith-Aufgaben project=monolith und echte task_id verwenden; Acribic nutzt seine vollständigen Regeln. Auch natürliche Aufträge wie „arbeite daran während ich unterwegs bin“ gelten; normale Fragen starten oder ändern keinen Auftrag. Status: project_work_status. Normale Gespräche bleiben unabhängig. Keine zusätzlichen Klassifizierer oder Warteaufrufe.', position='after_memory', max_chars=650)
    ctx.register_hook('gateway_message_admitted', instance.admitted)
    ctx.register_hook('gateway_ready', instance.ready)
    ctx._manager._ole_project_work_instance = instance
