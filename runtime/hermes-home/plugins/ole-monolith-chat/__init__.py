"""Registration only. The existing gateway owns activation and the agent lifecycle."""
from uuid import UUID


def valid_config(config):
    extra = getattr(config, 'extra', {}) or {}
    owner = extra.get('owner_id')
    try:
        if str(UUID(owner)) != owner:
            return False
    except (ValueError, TypeError, AttributeError):
        return False
    return extra.get('allowed_users') == [owner]


def register(ctx):
    def factory(config):
        from .adapter import MonolithAdapter
        return MonolithAdapter(config)
    ctx.register_platform('monolith', 'Monolith', factory, lambda: True,
                          validate_config=valid_config, allowed_users_env='MONOLITH_ALLOWED_USERS')
