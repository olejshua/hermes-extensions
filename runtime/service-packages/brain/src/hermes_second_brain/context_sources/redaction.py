from __future__ import annotations

import hashlib
import math
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REDACTION_VERSION = "v1"
_TOKEN = re.compile(
    r"(?ix)\b(?:"
    r"bearer\s+[a-z0-9._~-]{12,}|"
    r"sk-[a-z0-9_-]{12,}|"
    r"xox[baprs]-[a-z0-9-]{8,}|"
    r"(?:token|api[ _-]?key|password|passwd|secret|credential)\s*[=:]\s*\S+|"
    r"eyj[a-z0-9_-]{8,}\.[a-z0-9_-]{4,}\.[a-z0-9_-]{4,}"
    r")"
)
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d .()/-]{7,}\d)(?!\w)")
_IBAN = re.compile(r"(?i)\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_PAN = re.compile(r"(?<!\d)(?:\d[ -]?){12,19}(?!\d)")


def stable_hash(value: str, *, namespace: str = "context-source") -> str:
    return hashlib.sha256(f"{namespace}\0{value}".encode("utf-8")).hexdigest()


def strip_url_secrets(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme.lower(), host + port, parsed.path or "/", "", ""))


def redact_text(value: str, *, limit: int = 1000) -> str:
    text = " ".join(str(value).split())[: max(0, limit)]
    text = _TOKEN.sub("[REDACTED_TOKEN]", text)
    text = _EMAIL.sub(lambda m: f"[email-domain:{m.group(1).lower()}]", text)
    text = _PHONE.sub("[REDACTED_PHONE]", text)
    text = _IBAN.sub("[REDACTED_ACCOUNT]", text)
    text = _PAN.sub("[REDACTED_CARD]", text)
    return text


def contains_financial_identifier(value: Any) -> bool:
    text = str(value)
    return bool(_IBAN.search(text) or _PAN.search(text))


def sanitize_error(exc: BaseException | str) -> str:
    # Public status must never echo paths, responses, queries, or credentials.
    name = type(exc).__name__ if isinstance(exc, BaseException) else "error"
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:80] or "error"


def validate_bounded_json(value: Any, *, max_depth: int = 8, max_items: int = 1024, max_string: int = 16_384) -> None:
    """Reject non-JSON, non-finite, or structurally unbounded values."""
    seen = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal seen
        seen += 1
        if seen > max_items or depth > max_depth:
            raise ValueError("json_schema_too_complex")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, int):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("non_finite_number")
            return
        if isinstance(item, str):
            if len(item.encode("utf-8")) > max_string:
                raise ValueError("json_string_too_large")
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 128:
                    raise ValueError("invalid_json_key")
                visit(child, depth + 1)
            return
        raise TypeError("non_json_value")

    visit(value, 0)
