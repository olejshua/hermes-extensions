from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ids import stable_id

SECURITY_RE = re.compile(r"(?i)(verification code|one-time code|2fa|mfa|security alert|password reset|login code|auth code)")
CODE_RE = re.compile(r"\b\d{4,8}\b")


@dataclass(frozen=True)
class MailEvent:
    event_id: str
    message_id: str
    received_at: str
    from_domain: str
    subject: str
    body_text: str
    tags: tuple[str, ...]
    retention_days: int

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "MailEvent":
        required = ["message_id", "received_at", "from_domain", "subject", "body_text"]
        missing = [key for key in required if key not in raw]
        if missing:
            raise ValueError(f"mail event missing fields: {','.join(missing)}")
        tags = tuple(str(t) for t in raw.get("tags", []))
        retention = int(raw.get("retention_days", 365))
        subject = redact_mail_text(str(raw["subject"]))
        body = redact_mail_text(str(raw["body_text"]))
        event_id = stable_id(str(raw["message_id"]), str(raw["received_at"]), subject, body)
        return cls(event_id, str(raw["message_id"]), str(raw["received_at"]), str(raw["from_domain"]), subject, body, tags, retention)

    def to_resource(self) -> dict[str, Any]:
        return {
            "id": self.event_id,
            "source": "sanitized-mail-jsonl",
            "message_id": self.message_id,
            "received_at": self.received_at,
            "from_domain": self.from_domain,
            "subject": self.subject,
            "body_text": self.body_text,
            "tags": list(self.tags),
            "retention_days": self.retention_days,
        }

    @property
    def filename(self) -> str:
        return f"mail-{self.event_id}.md"

    def as_markdown(self) -> str:
        metadata = {
            "event_id": self.event_id,
            "message_id": self.message_id,
            "received_at": self.received_at,
            "from_domain": self.from_domain,
            "subject": self.subject,
            "tags": list(self.tags),
            "retention_days": self.retention_days,
        }
        lines = ["---"]
        for key, value in metadata.items():
            lines.append(f"{key}: {json.dumps(value, sort_keys=True)}")
        lines.extend(["---", "", self.body_text.rstrip(), ""])
        return "\n".join(lines)


def redact_mail_text(text: str) -> str:
    if SECURITY_RE.search(text):
        text = SECURITY_RE.sub("[SECURITY-MAIL]", text)
    return CODE_RE.sub("[CODE]", text)


def ingest_jsonl(input_path: Path, output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    events = read_mail_events(input_path)
    with output_path.open("w", encoding="utf-8") as dst:
        for event in events:
            dst.write(json.dumps(event.to_resource(), sort_keys=True) + "\n")
    return len(events)


def materialize_markdown(input_path: Path, output_dir: Path) -> int:
    safe_dir = _prepare_output_dir(output_dir)
    count = 0
    for event in read_mail_events(input_path):
        target = _safe_child(safe_dir, event.filename)
        _write_atomic_if_changed(target, event.as_markdown())
        count += 1
    return count


def read_mail_events(input_path: Path) -> list[MailEvent]:
    seen: set[str] = set()
    events: list[MailEvent] = []
    with input_path.open("r", encoding="utf-8") as src:
        for line_no, line in enumerate(src, start=1):
            if not line.strip():
                continue
            try:
                event = MailEvent.from_json(json.loads(line))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{input_path}:{line_no}: {exc}") from exc
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            events.append(event)
    return events


def _prepare_output_dir(output_dir: Path) -> Path:
    if output_dir.exists() and output_dir.is_symlink():
        raise ValueError(f"output directory must not be a symlink: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise ValueError(f"output path is not a directory: {output_dir}")
    return output_dir.resolve()


def _safe_child(output_dir: Path, filename: str) -> Path:
    target = output_dir / filename
    if target.name != filename or target.parent != output_dir:
        raise ValueError(f"unsafe mail output filename: {filename}")
    return target


def _write_atomic_if_changed(target: Path, content: str) -> bool:
    if target.is_symlink():
        raise ValueError(f"refusing to overwrite symlink: {target}")
    if target.exists():
        if target.read_text(encoding="utf-8") == content:
            return False
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return True
