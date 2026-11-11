from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Mapping

from .adapters import CalendarAdapter, ContactsAdapter, MessagesAdapter, NotesMetadataAdapter, NotionAdapter, PhotosAdapter, RemindersAdapter, SafariAdapter, SlackAdapter, WhatsAppAdapter
from .adapters.composio import ReadOnlyTransport
from .adapters.safe_import import DEFAULT_INGRESS_DIR, SafeImportAdapter
from .base import SourceAdapter
from .models import ScanBatch, ScanRequest, Sensitivity, SourceCursor, SourceHealth, SourceStatus


class ImportOnlyAdapter:
    sensitivity = Sensitivity.RESTRICTED
    retention = (90, 365)

    def __init__(self, source_name: str):
        self.source_name = source_name

    def health(self, request: ScanRequest) -> SourceHealth:
        return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, "user_export_required", f"safe-import-{self.source_name}-v1")

    def scan(self, request: ScanRequest, cursor: SourceCursor | None) -> ScanBatch:
        raise PermissionError("user_export_required")


class SourceRegistry:
    def __init__(self, adapters: Iterable[SourceAdapter] = ()): 
        self._adapters: dict[str, SourceAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: SourceAdapter) -> None:
        name = str(adapter.source_name).strip().lower()
        if not name or name in self._adapters:
            raise ValueError(f"duplicate_or_invalid_source:{name}")
        self._adapters[name] = adapter

    def get(self, name: str) -> SourceAdapter:
        try:
            return self._adapters[name.strip().lower()]
        except KeyError as exc:
            raise KeyError("unknown_source") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def adapters(self) -> tuple[SourceAdapter, ...]:
        return tuple(self._adapters[name] for name in self.names())


def default_registry(
    *,
    ingress_dir: Path | str | None = None,
    transports: Mapping[str, ReadOnlyTransport] | None = None,
    contacts_provider=None,
) -> SourceRegistry:
    whatsapp_path = os.environ.get("WHATSAPP_IMPORT_PATH")
    ingress = Path(ingress_dir or os.environ.get("CONTEXT_INGRESS_DIR", DEFAULT_INGRESS_DIR)).expanduser()
    configured_transports = dict(transports or {})
    return SourceRegistry(
        [
            CalendarAdapter(),
            RemindersAdapter(),
            SafariAdapter(),
            PhotosAdapter(),
            ContactsAdapter(provider=contacts_provider),
            MessagesAdapter(),
            NotesMetadataAdapter(),
            SafeImportAdapter.ingress_adapter("screen-time", ingress),
            NotionAdapter(transport=configured_transports.get("notion")),
            SlackAdapter(transport=configured_transports.get("slack")),
            WhatsAppAdapter(Path(whatsapp_path).expanduser() if whatsapp_path else None),
            SafeImportAdapter.ingress_adapter("health", ingress),
            SafeImportAdapter.ingress_adapter("sleep", ingress),
            SafeImportAdapter.ingress_adapter("workouts", ingress),
            ImportOnlyAdapter("banking"),
            ImportOnlyAdapter("purchases"),
        ]
    )
