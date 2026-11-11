from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)=([^&\s]+)"),
    re.compile(r"(?i)\b(bearer)\s+[a-z0-9._~+/=-]{12,}"),
    re.compile(r"\b\d{6}\b"),
]


def redact(value: object) -> object:
    if not isinstance(value, str):
        return value
    redacted = value
    redacted = SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}=[REDACTED]", redacted)
    redacted = SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)} [REDACTED]", redacted)
    redacted = SECRET_PATTERNS[2].sub("[CODE]", redacted)
    return redacted


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "message": redact(record.getMessage()),
            "logger": record.name,
        }
        for key, value in getattr(record, "fields", {}).items():
            payload[key] = redact(value)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def configure_logging(verbose: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.WARNING)
