"""Hermes Dreaming: background reflection across Hermes profiles and Second Brain context.

The package implements a managed Light -> REM -> Deep sweep that ingests redacted
conversational evidence read-only, reflects over it with a subscription-backed
structured model, scores candidates deterministically, and classifies only grounded
conclusions into a private Dream store plus a dedicated OpenViking namespace.

Durable output never touches USER.md or MEMORY.md; those remain critical
always-on context owned by the user.
"""

from __future__ import annotations

from .config import DreamingConfig, ProfileConfig, load_dreaming_config
from .runner import (
    SweepOutcome,
    acknowledge_dream_report,
    claim_dream_report,
    dream_report,
    dream_status,
    release_dream_report,
    run_extended,
    run_sweep,
)

__all__ = [
    "DreamingConfig",
    "ProfileConfig",
    "SweepOutcome",
    "load_dreaming_config",
    "claim_dream_report",
    "acknowledge_dream_report",
    "release_dream_report",
    "dream_report",
    "dream_status",
    "run_extended",
    "run_sweep",
]
