"""Local-first, observation-only context source scanner."""

from .base import SourceAdapter
from .models import (
    Derivative,
    RawItem,
    ScanBatch,
    ScanRequest,
    ScanResult,
    Sensitivity,
    SourceCursor,
    SourceHealth,
    SourceStatus,
)
from .registry import SourceRegistry, default_registry
from .runner import SourceRunner
from .store import DEFAULT_SOURCE_DB, SourceStore
from .adapters.safe_import import DEFAULT_INGRESS_DIR

__all__ = [
    "DEFAULT_SOURCE_DB",
    "DEFAULT_INGRESS_DIR",
    "Derivative",
    "RawItem",
    "ScanBatch",
    "ScanRequest",
    "ScanResult",
    "Sensitivity",
    "SourceAdapter",
    "SourceCursor",
    "SourceHealth",
    "SourceRegistry",
    "SourceRunner",
    "SourceStatus",
    "SourceStore",
    "default_registry",
]
