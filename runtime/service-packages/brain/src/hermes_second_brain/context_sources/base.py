from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import ScanBatch, ScanRequest, Sensitivity, SourceCursor, SourceHealth


@runtime_checkable
class SourceAdapter(Protocol):
    """Deliberately read-only adapter contract.

    There are no create/update/delete/send/acknowledge methods on this boundary.
    """

    source_name: str
    sensitivity: Sensitivity

    def health(self, request: ScanRequest) -> SourceHealth: ...

    def scan(self, request: ScanRequest, cursor: SourceCursor | None) -> ScanBatch: ...
