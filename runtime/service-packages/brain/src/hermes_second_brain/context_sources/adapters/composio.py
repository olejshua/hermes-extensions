from __future__ import annotations

import math
from typing import Any, Protocol

from ..models import Derivative, RawItem, ScanBatch, ScanRequest, Sensitivity, SourceCursor, SourceHealth, SourceStatus
from ..redaction import REDACTION_VERSION, redact_text, stable_hash, strip_url_secrets


class ReadOnlyTransport(Protocol):
    def execute(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


NOTION_ALLOWLIST = frozenset({"NOTION_SEARCH", "NOTION_FETCH", "NOTION_LIST_PAGES", "NOTION_QUERY_DATABASE"})
SLACK_ALLOWLIST = frozenset({"SLACK_SEARCH", "SLACK_FETCH_MESSAGES", "SLACK_LIST_CHANNELS", "SLACK_LIST_THREADS"})
MUTATION_WORDS = ("CREATE", "UPDATE", "DELETE", "SEND", "POST", "REPLY", "REACT", "WRITE", "ARCHIVE", "INVITE")


def validate_read_tool(tool: str, allowlist: frozenset[str]) -> str:
    normalized = str(tool).strip().upper()
    if normalized not in allowlist or any(word in normalized for word in MUTATION_WORDS):
        raise ValueError("tool_not_read_only")
    return normalized


class CredentialFreeToolAdapter:
    sensitivity = Sensitivity.SENSITIVE
    retention = (90, 365)
    allowlist: frozenset[str] = frozenset()
    default_tool = ""

    def __init__(self, source_name: str, *, transport: ReadOnlyTransport | None = None, tool: str | None = None):
        self.source_name = source_name
        self.transport = transport
        self.tool = validate_read_tool(tool or self.default_tool, self.allowlist)

    def health(self, request: ScanRequest) -> SourceHealth:
        if self.transport is None:
            return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, "read_transport_not_configured")
        available = getattr(self.transport, "available", None)
        if callable(available):
            try:
                if not bool(available()):
                    return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, "read_transport_unavailable")
            except Exception:
                return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, "read_transport_unavailable")
        return SourceHealth(self.source_name, SourceStatus.HEALTHY, "ok", f"{self.source_name}-readonly-v1")

    def scan(self, request: ScanRequest, cursor: SourceCursor | None) -> ScanBatch:
        if self.transport is None:
            raise PermissionError("read_transport_not_configured")
        # The adapter accepts an injected transport only. It owns no credential
        # fields and never serializes transport/configuration into the vault.
        max_pages = min(10, max(1, int(request.options.get("max_pages", 3))))
        continuing_full = bool(cursor and cursor.value.get("reconciling"))
        reconciliation = bool(request.full or continuing_full)
        page_token = "" if (request.full and not continuing_full) or cursor is None else str(cursor.value.get("page_token") or "")
        record_offset = int(cursor.value.get("record_index", 0)) if cursor and (not request.full or continuing_full) else 0
        if len(page_token) > 512:
            raise ValueError("transport_cursor_too_large")
        raw: list[RawItem] = []
        derivatives: list[Derivative] = []
        skipped = 0
        limited = False
        pages_read = 0
        while pages_read < max_pages:
            pages_read += 1
            current_token = page_token
            arguments = {"limit": 100}
            if page_token:
                arguments["cursor"] = page_token
            response = self.transport.execute(self.tool, arguments)
            if not isinstance(response, dict):
                raise ValueError("transport_invalid_schema")
            records = response.get("items", response.get("results", []))
            if not isinstance(records, list) or len(records) > 1000:
                raise ValueError("transport_invalid_schema")
            for record_index, item in enumerate(records):
                if record_index < record_offset:
                    continue
                if len(raw) >= request.limit:
                    page_token = current_token
                    record_offset = record_index
                    limited = True
                    break
                parsed = self._parse(item)
                if parsed is None:
                    skipped += 1
                    continue
                external_id, payload, event = parsed
                raw.append(RawItem(external_id, payload, str(payload.get("timestamp") or ""), account_scope=str(payload.get("workspace_hash") or "")))
                derivatives.append(Derivative(external_id, event, account_scope=str(payload.get("workspace_hash") or "")))
                if len(raw) >= request.limit:
                    page_token = current_token
                    record_offset = record_index + 1
                    limited = True
                    break
            if limited:
                break
            next_token = response.get("next_cursor")
            page_token = str(next_token or "")
            record_offset = 0
            if len(page_token) > 512:
                raise ValueError("transport_cursor_too_large")
            if not page_token:
                break
        if page_token and pages_read >= max_pages:
            limited = True
        cursor_value = {"page_token": page_token, "record_index": record_offset, "reconciling": bool(reconciliation and limited)}
        return ScanBatch(
            tuple(raw), tuple(derivatives), SourceCursor(cursor_value), f"{self.source_name}-readonly-v1", skipped,
            "partial" if limited else "ok", full_reconciliation=reconciliation, scan_complete=not limited,
        )

    def _parse(self, item: Any) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
        if not isinstance(item, dict):
            return None
        external_id = _primitive_text(item.get("id") or item.get("message_id") or item.get("page_id"), 1024)
        timestamp = _primitive_text(item.get("timestamp") or item.get("updated_at") or item.get("ts"), 128)
        source_text = _primitive_text(item.get("text") or item.get("title") or item.get("content"), 16_384)
        text = redact_text(source_text, limit=2000)
        if not external_id or not text:
            return None
        workspace = _primitive_text(item.get("workspace_id") or item.get("team_id"), 4096)
        workspace_hash = stable_hash(workspace, namespace=f"{self.source_name}-workspace") if workspace else ""
        channel = _primitive_text(item.get("channel_id") or item.get("database_id") or item.get("parent_id"), 4096)
        channel_hash = stable_hash(channel, namespace=f"{self.source_name}-container") if channel else ""
        url = strip_url_secrets(_primitive_text(item.get("url") or item.get("permalink"), 8192))
        payload = {"text": text, "timestamp": timestamp, "workspace_hash": workspace_hash, "container_hash": channel_hash, "url": url}
        event = {
            "platform": self.source_name,
            "workspace": workspace_hash,
            "conversation_id": channel_hash,
            "conversation_name": self.source_name.title(),
            "conversation_type": "remote_readonly",
            "body": text,
            "message_ts": timestamp,
            "source_message_id": "remote_" + stable_hash(external_id, namespace=f"{self.source_name}-item"),
            "permalink": url,
            "direction": "unknown",
            "raw": {"source": self.source_name, "transport": "injected_readonly", "redaction_version": REDACTION_VERSION},
        }
        return external_id, payload, event


def _primitive_text(value: Any, limit: int) -> str:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    text = str(value)
    return text if len(text.encode("utf-8")) <= limit else ""


class NotionAdapter(CredentialFreeToolAdapter):
    allowlist = NOTION_ALLOWLIST
    default_tool = "NOTION_SEARCH"

    def __init__(self, *, transport: ReadOnlyTransport | None = None, tool: str | None = None):
        super().__init__("notion", transport=transport, tool=tool)


class SlackAdapter(CredentialFreeToolAdapter):
    allowlist = SLACK_ALLOWLIST
    default_tool = "SLACK_SEARCH"

    def __init__(self, *, transport: ReadOnlyTransport | None = None, tool: str | None = None):
        super().__init__("slack", transport=transport, tool=tool)
