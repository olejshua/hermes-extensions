"""Provider-bound metadata only: no inference, auth refresh, static or synthetic models."""
import datetime
import time

_cache = None
_attempt = None


def capabilities():
    global _cache, _attempt
    now = time.monotonic()
    if _attempt is not None and now - _attempt < 900:
        return _cache
    _attempt = now
    _cache = None
    try:
        from hermes_cli.config import load_config
        from hermes_cli.auth import resolve_codex_runtime_credentials, _codex_access_token_is_expiring
        from hermes_cli.auth_codex import _codex_base_url
        from agent.model_metadata import fetch_codex_catalog_entries, _codex_catalog_probe_allowed
        from agent.codex_headers import codex_account_headers
        from agent.reasoning_effort import codex_supported_efforts
        import httpx
        config=load_config()
        if config.get('fallback_model') or config.get('fallback_providers'): return None
        model_config = config.get('model') or {}
        if model_config.get('provider') != 'openai-codex':
            return None  # This adapter intentionally supports only the existing subscription route.
        creds = resolve_codex_runtime_credentials(read_only=True)
        token, base = creds.get('api_key'), (creds.get('base_url') or _codex_base_url()).rstrip('/')
        if not token or _codex_access_token_is_expiring(token, 0) or not _codex_catalog_probe_allowed(token, base):
            return None
        headers = {'Authorization': 'Bearer ' + token, **codex_account_headers(token)}
        entries, status = fetch_codex_catalog_entries(lambda url: httpx.get(url, headers=headers, timeout=10), base_url=base)
        if not isinstance(entries, list):
            return None
        models = []
        for entry in entries:
            model = entry.get('slug')
            if entry.get('visibility') != 'list' or not isinstance(model, str):
                continue
            supported = codex_supported_efforts(model)
            efforts = [r.get('effort') for r in entry.get('supported_reasoning_levels', []) if r.get('effort') in supported]
            if efforts:
                models.append({'model': model, 'efforts': list(dict.fromkeys(efforts))})
        if not models or len(models) > 40:
            return None
        _cache = {'protocol': 'chat-v3', 'catalogAt': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'models': models}
        return _cache
    except Exception:
        # Never turn an auth/catalog failure into another provider, model, or invented list.
        return None


def bind_options(runner, session_key, options, catalog):
    if not options:
        return
    if not catalog or options.get('protocol') != 'chat-v3' or not any(row['model'] == options.get('model') and options.get('effort') in row['efforts'] for row in catalog['models']):
        raise ValueError('monolith_selected_options_unavailable')
    from hermes_cli.config import load_config
    config=load_config()
    if (config.get('model') or {}).get('provider')!='openai-codex' or config.get('fallback_model') or config.get('fallback_providers'):
        raise ValueError('monolith_subscription_route_changed')
    state = runner._session_state(session_key).conversation
    # Same native per-session mechanism used by Hermes /model and /reasoning.
    # Credentials are resolved by the existing provider; no keys enter this record.
    previous=(state.model_override,state.reasoning_override)
    state.model_override = {'model': options['model'], 'provider': 'openai-codex'}
    runner._set_session_reasoning_override(session_key, {'enabled': True, 'effort': options['effort']})

    return previous


def restore_options(runner, session_key, previous):
    if previous is not None:
        state=runner._session_state(session_key).conversation
        state.model_override,state.reasoning_override=previous
