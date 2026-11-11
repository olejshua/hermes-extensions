"""Exact-source native gateway tool selection; never changes global platform tools.

The installed SDK ignores an empty adapter.toolsets_for_source override. Its
native _resolve_turn_toolsets seam does accept enabled_toolsets=[] and includes
that list in the cached-agent fingerprint. Scope this seam to admitted task
sources so normal sessions retain their existing MCP and other tools.
"""

def source_key(source):
    return tuple(str(getattr(source, field, '') or '') for field in ('platform', 'chat_id', 'user_id', 'thread_id'))


def restrict_task_tools(runner, source):
    import os
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise ValueError('monolith_task_tools_forced_by_dispatcher')
    scope = getattr(runner, '_monolith_task_tool_scope', None)
    if scope is None:
        original = getattr(runner, '_resolve_turn_toolsets', None)
        if not callable(original):
            raise ValueError('monolith_native_tool_scope_unavailable')
        scope = {'original': original, 'sources': {}}

        def resolve(user_config, current_source, platform_key):
            if source_key(current_source) in scope['sources']:
                return [], None  # [] is explicit no-tools at native AIAgent construction/dispatch.
            return original(user_config, current_source, platform_key)

        scope['resolver'] = resolve
        runner._monolith_task_tool_scope = scope
        runner._resolve_turn_toolsets = resolve
    key = source_key(source)
    scope['sources'][key] = scope['sources'].get(key, 0) + 1
    return key


def restore_task_tools(runner, key):
    scope = getattr(runner, '_monolith_task_tool_scope', None)
    if scope is None or key not in scope['sources']:
        return
    scope['sources'][key] -= 1
    if not scope['sources'][key]:
        del scope['sources'][key]
    if not scope['sources']:
        if runner._resolve_turn_toolsets is scope['resolver']:
            runner._resolve_turn_toolsets = scope['original']
        del runner._monolith_task_tool_scope
