from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Sensitivity(str, Enum):
    LOW = "low"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class SourceStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    PENDING_PERMISSION = "pending_permission"
    UNSUPPORTED = "unsupported"
    ERROR = "error"
    DISABLED = "disabled"


@dataclass(frozen=True)
class SourceCursor:
    value: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def coerce(cls, value: SourceCursor | dict[str, Any] | None) -> SourceCursor | None:
        if value is None or isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise TypeError("cursor must be an object")
        return cls(dict(value))


@dataclass(frozen=True)
class ScanRequest:
    source: str = ""
    full: bool = False
    dry_run: bool = False
    limit: int = 1000
    now: dt.datetime | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceHealth:
    source: str
    status: SourceStatus
    reason_code: str = "ok"
    schema_fingerprint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "schema_fingerprint": self.schema_fingerprint,
        }


@dataclass(frozen=True)
class RawItem:
    external_id: str
    payload: dict[str, Any]
    observed_at: str
    account_scope: str = ""
    expires_at: float | None = None


@dataclass(frozen=True)
class Derivative:
    external_id: str
    event: dict[str, Any]
    account_scope: str = ""
    export_allowed: bool = True
    redaction_version: str = "v1"
    expires_at: float | None = None


@dataclass(frozen=True)
class ScanBatch:
    raw_items: tuple[RawItem, ...] = ()
    derivatives: tuple[Derivative, ...] = ()
    cursor: SourceCursor | None = None
    schema_fingerprint: str = ""
    skipped: int = 0
    reason_code: str = "ok"
    full_reconciliation: bool = False
    scan_complete: bool = True


@dataclass(frozen=True)
class ScanResult:
    source: str
    status: SourceStatus
    reason_code: str
    fetched: int = 0
    stored: int = 0
    derivatives: int = 0
    skipped: int = 0
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "fetched": self.fetched,
            "stored": self.stored,
            "derivatives": self.derivatives,
            "skipped": self.skipped,
            "dry_run": self.dry_run,
        }
