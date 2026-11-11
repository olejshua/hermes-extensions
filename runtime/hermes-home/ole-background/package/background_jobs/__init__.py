"""Durable background work; leaf workers do not load the lifecycle service."""

__all__ = ["BackgroundService"]

def __getattr__(name):
    if name == "BackgroundService":
        from .service import BackgroundService
        return BackgroundService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
