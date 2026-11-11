from __future__ import annotations

from ..models import ScanBatch, ScanRequest, Sensitivity, SourceCursor, SourceHealth, SourceStatus


class ScreenTimeAdapter:
    """No reverse engineering: Screen Time is available only through safe import."""

    source_name = "screen-time"
    sensitivity = Sensitivity.RESTRICTED
    retention = (180, 180)

    def health(self, request: ScanRequest) -> SourceHealth:
        return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, "user_export_required", "screen-time-daily-aggregate-v1")

    def scan(self, request: ScanRequest, cursor: SourceCursor | None) -> ScanBatch:
        raise PermissionError("user_export_required")
