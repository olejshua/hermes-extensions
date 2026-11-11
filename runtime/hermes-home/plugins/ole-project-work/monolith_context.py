"""Capture the exact current task through the already configured native reader."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import time
import uuid


def capture(task_id, *, client=None, client_factory=None, clock=time.time):
    try:
        tid = str(uuid.UUID(task_id))
        if not isinstance(task_id, str) or tid != task_id.lower():
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('exact_monolith_id_required') from None
    if client is None:
        client = client_factory()
    # The client is resolved in the admitted native profile before threads start.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='monolith-job-context') as pool:
        task = pool.submit(client.call, 'monolith_get_task', {'taskId': tid})
        projects = pool.submit(client.call, 'monolith_list_projects', {})
        task, projects = task.result(), projects.result()
    row = task.get('task') if isinstance(task, dict) else None
    rows = projects.get('projects') if isinstance(projects, dict) else None
    if not isinstance(row, dict) or row.get('id') != tid or not isinstance(rows, list):
        raise ValueError('monolith_exact_context_required')
    choices = [p for p in rows if isinstance(p, dict) and str(p.get('name', '')).casefold() == 'acribic']
    if len(choices) != 1:
        raise ValueError('monolith_project_ambiguous')
    project = choices[0]
    if row.get('project_id') is not None and row.get('project_id') != project.get('id'):
        raise ValueError('monolith_task_project_mismatch')
    return {'source': 'monolith', 'project': project, 'task': row, 'captured_at': clock()}


def native_client(ctx):
    # Reuse the installed scoped reader, rather than create a second credential
    # store, connection, tool catalogue or source parser.
    from tools.registry import registry
    entry = registry.get_entry('dialogue_read', scope=ctx._manager.scope_key)
    reader = getattr(entry.handler, '__self__', None) if entry is not None else None
    if reader is None:
        raise ValueError('monolith_reader_unavailable')
    package = reader.__class__.__module__.rsplit('.', 1)[0]
    module = importlib.import_module('.monolith_reads', package)
    return module.NativeMCPReads.native()
