from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf", ".docx"}
DEFAULT_EXCLUDES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    "cache",
    ".cache",
    "build",
    "dist",
}
BINARY_EXTENSIONS = {
    ".7z",
    ".avi",
    ".bin",
    ".dmg",
    ".exe",
    ".gif",
    ".gz",
    ".heic",
    ".jpg",
    ".jpeg",
    ".mov",
    ".mp3",
    ".mp4",
    ".png",
    ".tar",
    ".webp",
    ".zip",
}
SECRET_NAME_HINTS = {"token", "password", "passwd", "api-key", "apikey", "private-key", "oauth", "cookie", "credential", "credentials", "secret", "secrets", "auth-token", "access-key"}


def default_b_plus_section() -> dict[str, Any]:
    """Portable private-store and subscription-wrapper defaults for B+."""

    root = "~/.hermes/second-brain"
    return {
        "lifecycle_db": f"{root}/lifecycle.sqlite3",
        "observatory_db": f"{root}/process-observatory.sqlite3",
        "observatory_spool": f"{root}/process-observatory-spool.d",
        "observatory_quarantine": f"{root}/process-observatory-quarantine.d",
        "observatory_retention_days": 90,
        "improvement_db": f"{root}/improvements.sqlite3",
        "improvement_ttl_days": 30,
        "review": {
            "claude_wrapper": "claude-subscription",
            "codex_cli": "/opt/homebrew/bin/codex",
            "codex_auth_file": "~/.codex/auth.json",
            "sandbox_exec": "/usr/bin/sandbox-exec",
            "primary_model": "sonnet",
            "primary_effort": "high",
            "primary_timeout_seconds": 420,
            "secondary_timeout_seconds": 420,
            "primary_max_output_bytes": 256000,
            "secondary_max_output_bytes": 256000,
        },
    }


@dataclass(frozen=True)
class Source:
    id: str
    root: Path
    namespace: str
    include_extensions: set[str] = field(default_factory=lambda: set(SUPPORTED_EXTENSIONS))
    exclude_globs: tuple[str, ...] = ()
    max_bytes: int = 25_000_000
    required_status: str | None = None
    approved_paths: tuple[str, ...] = ()
    approval_receipts: Path | None = None
    accepted_statuses: tuple[str, ...] = ("confirmed",)


@dataclass(frozen=True)
class Manifest:
    state_db: Path
    context_inbox_db: Path = Path("~/.hermes/second-brain/context-inbox.sqlite3").expanduser()
    ov_binary: str = "ov"
    ov_timeout_seconds: float = 120.0
    retry_attempts: int = 3
    retry_backoff_seconds: float = 0.25
    concurrency: int = 2
    wait_for_indexing: bool = True
    sources: tuple[Source, ...] = ()
    source_grants: Path | None = None


def _as_abs(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def load_manifest(path: Path) -> Manifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent.resolve()
    sources = []
    namespaces: dict[str, str] = {}
    for item in raw.get("sources", []):
        exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in item.get("include_extensions", SUPPORTED_EXTENSIONS)}
        source_id = str(item["id"])
        namespace = str(item.get("namespace", source_id))
        if namespace in namespaces:
            raise ValueError(f"duplicate source namespace {namespace!r} for sources {namespaces[namespace]!r} and {source_id!r}; namespaces must be unique")
        namespaces[namespace] = source_id
        sources.append(
            Source(
                id=source_id,
                root=_as_abs(base, str(item["root"])),
                namespace=namespace,
                include_extensions=exts,
                exclude_globs=tuple(item.get("exclude_globs", ())),
                max_bytes=int(item.get("max_bytes", 25_000_000)),
                required_status=item.get("required_status"),
                approved_paths=tuple(item.get("approved_paths", ())),
                approval_receipts=_as_abs(base, item["approval_receipts"]) if item.get("approval_receipts") else None,
                accepted_statuses=tuple(item.get("accepted_statuses", ("confirmed",))),
            )
        )
    return Manifest(
        state_db=_as_abs(base, str(raw.get("state_db", ".state/hermes-second-brain.sqlite3"))),
        source_grants=_as_abs(base, raw["source_grants"]) if raw.get("source_grants") else None,
        context_inbox_db=_as_abs(base, str(raw.get("context_inbox_db", "~/.hermes/second-brain/context-inbox.sqlite3"))),
        ov_binary=str(raw.get("ov_binary", "ov")),
        ov_timeout_seconds=float(raw.get("ov_timeout_seconds", 120)),
        retry_attempts=int(raw.get("retry_attempts", 3)),
        retry_backoff_seconds=float(raw.get("retry_backoff_seconds", 0.25)),
        concurrency=max(1, int(raw.get("concurrency", 2))),
        wait_for_indexing=bool(raw.get("wait_for_indexing", True)),
        sources=tuple(sources),
    )


def write_default_manifest(path: Path) -> None:
    from .dreaming.config import default_dreaming_section, dreams_manifest_source

    dreaming = default_dreaming_section()
    dreaming["enabled"] = False
    data: dict[str, Any] = {
        "state_db": ".state/hermes-second-brain.sqlite3",
        "context_inbox_db": "~/.hermes/second-brain/context-inbox.sqlite3",
        "ov_binary": "ov",
        "concurrency": 2,
        "retry_attempts": 3,
        "sources": [
            {
                "id": "repo-brain",
                "root": "../../../brain",
                "namespace": "brain",
                "include_extensions": sorted(SUPPORTED_EXTENSIONS),
                "exclude_globs": [
                    "**/.git/**",
                    "**/.venv/**",
                    "**/cache/**",
                    "**/build/**",
                    "**/*token*",
                    "**/*password*",
                    "**/*passwd*",
                    "**/*api[-_]?key*",
                    "**/*private[-_]?key*",
                    "**/*oauth*",
                    "**/*cookie*",
                    "**/*credential*",
                    "**/*secret*",
                    "**/*auth[-_]?token*",
                    "**/*access[-_]?key*",
                ],
            }
            ,
            {
                "id": "context-inbox",
                "root": "~/.hermes/second-brain/import/context",
                "namespace": "context",
                "include_extensions": [".txt"],
                "exclude_globs": [
                    "**/*token*",
                    "**/*password*",
                    "**/*passwd*",
                    "**/*api[-_]?key*",
                    "**/*private[-_]?key*",
                    "**/*oauth*",
                    "**/*cookie*",
                    "**/*credential*",
                    "**/*secret*",
                    "**/.env*",
                ],
            },
            {
                "id": "mail-context-bundles",
                "root": "~/.hermes/second-brain/import/mail-bundles",
                "namespace": "mail",
                "include_extensions": [".md"],
                "exclude_globs": [
                    "**/*token*",
                    "**/*password*",
                    "**/*passwd*",
                    "**/*api[-_]?key*",
                    "**/*private[-_]?key*",
                    "**/*oauth*",
                    "**/*cookie*",
                    "**/*credential*",
                    "**/*secret*",
                    "**/*auth[-_]?token*",
                    "**/*access[-_]?key*",
                    "**/.env*",
                ],
            },
            dreams_manifest_source(),
        ],
        "dreaming": dreaming,
        "b_plus": default_b_plus_section(),
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
