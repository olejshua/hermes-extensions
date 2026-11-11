"""Immutable owned Monolith input, never a second authoritative task record."""
import json
import math
import uuid
from datetime import datetime
from .safety import Fault, digest

MAX_LEGACY_CONTEXT_BYTES = 4_000_000
MAX_CONTEXT_BYTES = 18 * 1024 * 1024
PROJECT_GROUPS = {'tasks':'tasks:read','documents':'notes:read','decisions':'decisions:read','assignments':'agent:read','planningGoals':'planning:read','openItems':'planning:read'}


def project_context(value, project_id, task=None):
    """Validate the provider contract while preserving every original field."""
    if not isinstance(value,dict) or value.get('version')!='project-context-v1':
        raise Fault('monolith_project_context_contract_invalid')
    if not isinstance(value.get('project'),dict) or value['project'].get('id')!=project_id:
        raise Fault('monolith_project_context_identity_mismatch')
    try:when=datetime.fromisoformat(value['capturedAt'].replace('Z','+00:00'))
    except (KeyError,ValueError,AttributeError):raise Fault('monolith_project_context_version_invalid') from None
    if when.tzinfo is None:raise Fault('monolith_project_context_version_invalid')
    coverage=value.get('coverage')
    if not isinstance(coverage,dict) or type(coverage.get('complete')) is not bool or not isinstance(coverage.get('missingScopes'),list) or not isinstance(coverage.get('unavailable'),list) or not isinstance(coverage.get('counts'),dict) or not isinstance(coverage.get('limits'),dict):
        raise Fault('monolith_project_context_coverage_invalid')
    if coverage['complete'] and (coverage['missingScopes'] or coverage['unavailable']):
        raise Fault('monolith_project_context_coverage_invalid')
    for group,scope in PROJECT_GROUPS.items():
        rows=value.get(group)
        if rows is None:
            if scope not in coverage['missingScopes']:raise Fault('monolith_project_context_coverage_invalid')
        elif not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):
            raise Fault('monolith_project_context_contract_invalid')
        elif scope in coverage['missingScopes'] or (type(coverage['counts'].get(group)) is not int or coverage['counts'][group]!=len(rows)):
            raise Fault('monolith_project_context_coverage_invalid')
    for group in ('links','sources','requiredScopes'):
        if not isinstance(value.get(group),list):raise Fault('monolith_project_context_contract_invalid')
    tasks=value['tasks']
    if tasks is not None:
        ids=[]
        for row in tasks:
            if row.get('project_id')!=project_id:raise Fault('monolith_project_context_identity_mismatch')
            ids.append(exact_id(row.get('id')))
        if len(ids)!=len(set(ids)):raise Fault('monolith_project_context_identity_mismatch')
        if task is not None:
            matches=[row for row in tasks if row['id']==task['id']]
            if len(matches)!=1 or matches[0]!=task:raise Fault('monolith_project_context_task_mismatch')
    elif task is not None:raise Fault('monolith_project_context_task_mismatch')
    if task is not None and value.get('focusTaskId')!=task['id']:
        raise Fault('monolith_project_context_task_mismatch')
    return value


def exact_id(value):
    try:
        parsed = str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if not parsed or value.lower() != parsed:
        raise Fault('exact_monolith_id_required')
    return parsed


def normalize(value, *, project_kind='acribic'):
    if value is None:
        return None
    if not isinstance(value, dict) or not {'source','project','task','captured_at'}<=set(value) or set(value)-{'source','project','task','captured_at','project_context'}:
        raise Fault('monolith_context_invalid')
    if value['source'] != 'monolith' or type(value['captured_at']) not in (int, float) or not math.isfinite(value['captured_at']):
        raise Fault('monolith_context_invalid')
    project, task = value['project'], value['task']
    if (project is not None and not isinstance(project, dict)) or not isinstance(task, dict):
        raise Fault('monolith_context_invalid')
    pid, tid = exact_id(project.get('id')) if project is not None else None, exact_id(task.get('id'))
    if project is not None and (not isinstance(project.get('name'), str) or not project['name'].strip() or project_kind=='acribic' and project['name'].casefold() != 'acribic'):
        raise Fault('monolith_project_not_allowed')
    if task.get('project_id') is not None and task.get('project_id') != pid:
        raise Fault('monolith_task_project_mismatch')
    if not isinstance(task.get('title'), str) or not task['title'].strip():
        raise Fault('monolith_task_title_required')
    if 'project_context' in value:
        if pid is None:raise Fault('monolith_project_context_identity_mismatch')
        project_context(value['project_context'],pid,task)
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()
    except (ValueError, TypeError, OverflowError):
        raise Fault('monolith_context_invalid') from None
    if len(raw) > (MAX_CONTEXT_BYTES if 'project_context' in value else MAX_LEGACY_CONTEXT_BYTES):
        raise Fault('monolith_context_too_large')
    return json.loads(raw)


def fingerprint(value):
    return digest({k: v for k, v in value.items() if k != 'captured_at'})


def reference(value):
    if value is None:
        return None
    result={'source': 'monolith', 'project_id': (value['project'] or {}).get('id'),
            'project_name': (value['project'] or {}).get('name'), 'task_id': value['task']['id'],
            'task_title': value['task']['title'], 'captured_at': value['captured_at'],
            'input_sha256': fingerprint(value), 'input_is_snapshot': True,
            'task_completed_by_worker': False, 'status_published_to_monolith': False}
    if 'project_context' in value:
        packet=value['project_context']
        result['project_context']={'version':packet['version'],'content_sha256':digest(packet),'captured_at':packet['capturedAt'],'coverage':packet['coverage'],'source_counts':{key:(len(packet[key]) if isinstance(packet[key],list) else None) for key in PROJECT_GROUPS}}
    return result
