"""Subscription-only auxiliary boundary: no provider auto-discovery fallback."""
import logging
import time
logger = logging.getLogger(__name__)
def call_subscription_llm(**kwargs):
    from agent.auxiliary_client import call_llm
    from agent.aux_provider_policy import allowed_auxiliary_providers
    runtime = _RUNTIME.get()
    if runtime:
        kwargs["main_runtime"] = {**runtime, "model": runtime.get("model", "gpt-6-sol")}
        for key in ("api_key", "base_url", "api_mode"):
            kwargs[key] = runtime[key]
    route = {}
    kwargs["provider"] = "openai-codex"
    kwargs["route_info"] = route
    started = time.monotonic()
    try:
        with allowed_auxiliary_providers({"openai-codex"}):
            response = call_llm(**kwargs)
    except Exception as exc:
        _LAST_ERROR.set(type(exc).__name__)
        raise
    logger.info("LCM auxiliary provider=%s model=%s duration_ms=%d", route.get("provider", "openai-codex"), route.get("model", "task-default"), int((time.monotonic()-started)*1000))
    return response

# Isolated integration canaries inject the existing access credential in memory,
# without copying a rotating OAuth store or changing production config.
from contextlib import contextmanager
from contextvars import ContextVar
_RUNTIME = ContextVar('lcm_subscription_runtime', default=None)
_LAST_ERROR = ContextVar('lcm_auxiliary_last_error', default=None)
@contextmanager
def existing_subscription_credentials(runtime):
    if runtime.get('provider') != 'openai-codex' or runtime.get('auth_mode') != 'chatgpt':
        raise ValueError('Existing ChatGPT subscription credentials required')
    token = _RUNTIME.set(runtime)
    try: yield
    finally: _RUNTIME.reset(token)
