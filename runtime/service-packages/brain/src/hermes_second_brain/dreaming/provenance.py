"""Bounded source metadata; retrieval time is never a source date."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from .redaction import redact

STATUSES = frozenset({"confirmed", "hypothesis", "disputed", "superseded", "unknown"})


def _text(value: Any, limit: int = 256) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return redact(" ".join(value.split()))[:limit]


def source_day(value: Any) -> str | None:
    """Accept explicit ISO source dates only; never guess from an index timestamp."""
    text = _text(value, 64)
    if not text:
        return None
    try:
        if len(text) == 10:
            return date.fromisoformat(text).isoformat()
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def source_provenance(item: dict[str, Any]) -> dict[str, Any]:
    tags = item.get("tags", {})
    if isinstance(tags, list):
        parsed = {}
        for tag in tags:
            if isinstance(tag, str) and "=" in tag:
                key, value = tag.split("=", 1)
                parsed[key] = value
            elif isinstance(tag, dict) and isinstance(tag.get("key"), str):
                parsed[tag["key"]] = tag.get("value")
        tags = parsed
    metadata = item.get("metadata", {})
    fields = {
        **(tags if isinstance(tags, dict) else {}),
        **(metadata if isinstance(metadata, dict) else {}),
        **{k: v for k, v in item.items() if v is not None},
    }
    status = _text(fields.get("evidence_status") or fields.get("knowledge_status"))
    return {
        "source_uri": _text(fields.get("source_uri") or item.get("uri") or item.get("path"), 2048),
        "source_id": _text(fields.get("source_id"), 512),
        "source_date": source_day(fields.get("source_date") or fields.get("document_date")),
        "source_version": _text(fields.get("source_version") or fields.get("sha256")),
        "evidence_status": status if status in STATUSES else "unknown",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def evidence_packet(item) -> dict[str, Any]:
    """The same provenance accompanies the quote in prompts and durable evidence."""
    return {"role": item.role, "quote": item.snippet, "day": item.day,
            "provenance": item.provenance}


def evidence_role(canonical: bool, provenance: dict[str, Any]) -> str:
    """Explicitly disputed, superseded or hypothetical material cannot corroborate a fact."""
    invalid = provenance.get("evidence_status") != "confirmed"
    if str(provenance.get("source_uri") or "").startswith("viking://resources/ole-approved/"):
        invalid = invalid or provenance.get("approval_verified") is not True
    return "canonical" if canonical and not invalid else "assistant"
