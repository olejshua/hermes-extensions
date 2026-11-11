"""Configuration for the Dreaming subsystem.

Configuration is path-explicit and lives in the same manifest JSON used by the
rest of the project, under a top-level ``dreaming`` object. Nothing here reads
environment variables for user-facing behaviour: production defaults must be
readable from the config file alone. Tests may pass overrides directly.
"""

from __future__ import annotations

import json
import ipaddress
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_MODEL_COMMAND: tuple[str, ...] = (
    "claude-subscription",
)
DEFAULT_TIMEZONE = "Europe/Berlin"
DEFAULT_RETRIEVAL_NAMESPACES: tuple[str, ...] = ("brain", "notes", "sessions", "legacy-memory", "context")

# Namespaces whose content is curated canonical knowledge and may therefore
# satisfy the corroboration gate on its own. `sessions`, `legacy-memory`,
# `mail`, and `context` are assistant or contextual evidence: useful for
# reflection, never sufficient to promote a claim by themselves.
DEFAULT_CANONICAL_NAMESPACES: tuple[str, ...] = ("brain", "notes")

# The hard ceiling on what may ever count as canonical corroboration. Config can
# narrow this set but never widen it: letting an operator declare `sessions`
# canonical would turn the assistant's own transcript into proof of a user fact
# and defeat the entire corroboration gate.
HARD_CANONICAL_NAMESPACES: frozenset[str] = frozenset({"brain", "notes", "ole-approved"})

# Local OpenViking search endpoint. A JSON body keeps private queries out of the
# process table, which an `ov find <query>` subprocess could not guarantee.
DEFAULT_RETRIEVAL_ENDPOINT = "http://127.0.0.1:1933/api/v1/search/find"

# Hermes session sources that never carry genuine user reflection material.
DEFAULT_EXCLUDED_SESSION_SOURCES: tuple[str, ...] = ("cron", "subagent")

# Substrings that mark a session (by title or source) as dreaming-generated.
# Ingesting these would let the system feed on its own output.
DEFAULT_SELF_MARKERS: tuple[str, ...] = (
    "hermes-dream",
    "hermes dreaming",
    "dream sweep",
    "dream-sweep",
    "dreaming sweep",
    "dream report",
    "dreams.md",
    "openviking_dream",
    "hermes-dream-prompt",
)


class ConfigError(ValueError):
    """Raised when the ``dreaming`` config object is missing or malformed."""


@dataclass(frozen=True)
class ProfileConfig:
    """One approved Hermes profile root.

    Profiles are enumerated explicitly. The implementation never scans
    ``~/.hermes/profiles`` or any other user directory for databases.
    """

    name: str
    state_db: Path
    lcm_db: Path | None = None
    enabled: bool = True


@dataclass(frozen=True)
class ModelConfig:
    command: tuple[str, ...] = DEFAULT_MODEL_COMMAND
    model: str = "sonnet"
    effort: str = "high"
    safe_mode: bool = True
    session_persistence: bool = False
    allow_tools: bool = False
    retry_attempts: int = 2
    retry_backoff_seconds: float = 2.0
    max_output_bytes: int = 2_000_000
    phase_timeout_seconds: Mapping[str, float] = field(
        default_factory=lambda: {"light": 300.0, "rem": 300.0, "deep": 300.0}
    )

    def __post_init__(self) -> None:
        # Pinned invariants, not defaults. Dreaming feeds untrusted conversation
        # text to the model, so a run with tools, a persisted session, or safe
        # mode off is never an acceptable configuration for this subsystem.
        if not self.safe_mode:
            raise ConfigError("dreaming.model.safe_mode cannot be disabled")
        if self.session_persistence:
            raise ConfigError("dreaming.model.session_persistence cannot be enabled")
        if self.allow_tools:
            raise ConfigError("dreaming.model.allow_tools cannot be enabled")

    def timeout_for(self, phase: str) -> float:
        return float(self.phase_timeout_seconds.get(phase, 300.0))


@dataclass(frozen=True)
class Budgets:
    """Hard bounds on how much material a single round may consider."""

    sessions_per_round: int = 12
    messages_per_session: int = 40
    characters_per_message: int = 1_200
    characters_per_session: int = 6_000
    characters_per_batch: int = 60_000
    evidence_snippet_characters: int = 320
    max_candidates_per_round: int = 60
    max_connections_per_round: int = 40
    max_retrieval_queries: int = 8
    max_retrieval_hits_per_query: int = 5
    max_retrieval_snippet_characters: int = 400
    max_report_items: int = 25


@dataclass(frozen=True)
class Thresholds:
    """Deterministic Deep-phase scoring weights and promotion gates.

    Weights follow OpenClaw's signal philosophy but are renormalised locally so
    a partial override in config cannot silently rescale the whole model.
    """

    weight_relevance: float = 0.28
    weight_evidence_quality: float = 0.16
    weight_frequency: float = 0.18
    weight_diversity: float = 0.14
    weight_recency: float = 0.12
    weight_consolidation: float = 0.08
    weight_durability: float = 0.04

    recency_half_life_days: float = 14.0

    # Gate: durable OpenViking promotion.
    promote_min_score: float = 0.72
    promote_min_evidence: int = 2
    promote_min_unique_sessions: int = 2
    promote_min_unique_days: int = 2
    promote_require_user_or_canonical: bool = True

    # Gate: softer landing in the context inbox for actionable-but-thin items.
    inbox_min_score: float = 0.55
    inbox_min_evidence: int = 1

    # Gate: keep as an explicit hypothesis rather than discarding.
    hypothesis_min_score: float = 0.35

    reject_max_score: float = 0.2

    def normalized_weights(self) -> dict[str, float]:
        raw = {
            "relevance": max(0.0, self.weight_relevance),
            "evidence_quality": max(0.0, self.weight_evidence_quality),
            "frequency": max(0.0, self.weight_frequency),
            "diversity": max(0.0, self.weight_diversity),
            "recency": max(0.0, self.weight_recency),
            "consolidation": max(0.0, self.weight_consolidation),
            "durability": max(0.0, self.weight_durability),
        }
        total = sum(raw.values())
        if total <= 0:
            return {key: 1.0 / len(raw) for key in raw}
        return {key: value / total for key, value in raw.items()}


@dataclass(frozen=True)
class RetrievalConfig:
    enabled: bool = True
    endpoint_url: str = DEFAULT_RETRIEVAL_ENDPOINT
    namespaces: tuple[str, ...] = DEFAULT_RETRIEVAL_NAMESPACES
    canonical_namespaces: tuple[str, ...] = DEFAULT_CANONICAL_NAMESPACES
    timeout_seconds: float = 15.0
    limit: int = 5
    max_response_bytes: int = 1_000_000
    levels: tuple[int, ...] = (0, 1)
    keyword_enabled: bool = False
    source_registry: Path | None = None
    approval_receipts: Mapping[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.levels or any(type(level) is not int or level not in {0, 1, 2} for level in self.levels):
            raise ConfigError("dreaming.retrieval.levels must contain only 0, 1, 2")
        if self.source_registry is not None and not self.source_registry.is_absolute():
            raise ConfigError("dreaming.retrieval.source_registry must be an absolute local path")
        if any(not path.is_absolute() for path in self.approval_receipts.values()):
            raise ConfigError("dreaming.retrieval.approval_receipts must use absolute paths")
        canonical = set(self.canonical_namespaces)
        unapproved = sorted(canonical - HARD_CANONICAL_NAMESPACES)
        if unapproved:
            raise ConfigError(
                "dreaming.retrieval.canonical_namespaces may only contain "
                f"{sorted(HARD_CANONICAL_NAMESPACES)}; rejected: {', '.join(unapproved)}"
            )
        # A canonical namespace that is never queried is a silent misconfiguration:
        # the corroboration gate would look satisfiable and never be satisfied.
        unqueried = sorted(canonical - set(self.namespaces))
        if unqueried:
            raise ConfigError(
                "dreaming.retrieval.canonical_namespaces must also appear in "
                f"dreaming.retrieval.namespaces; missing: {', '.join(unqueried)}"
            )

    def is_canonical(self, namespace: str) -> bool:
        return namespace in self.canonical_namespaces


@dataclass(frozen=True)
class DreamingConfig:
    enabled: bool
    timezone: str
    profiles: tuple[ProfileConfig, ...]
    dream_state_db: Path
    reports_dir: Path
    import_dir: Path
    dreams_md: Path
    # Generated import artifacts are built here first. This directory must stay
    # outside `import_dir` so a half-finished or failed run can never present a
    # file to the manifest sync that watches the import tree.
    staging_dir: Path = Path()
    # Typed B+ closure queues.  Dream writes an internal outbox first, then
    # stages into these private stores idempotently; neither queue applies an
    # action automatically.
    lifecycle_db: Path = Path()
    improvement_db: Path = Path()
    model: ModelConfig = field(default_factory=ModelConfig)
    fallback_models: tuple[ModelConfig, ...] = ()
    budgets: Budgets = field(default_factory=Budgets)
    thresholds: Thresholds = field(default_factory=Thresholds)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    lookback_days: int = 14
    full_backfill_enabled: bool = True
    loop_interval_minutes: float = 2.0
    max_rounds: int = 64
    nightly_start_local: str = "03:00"
    catchup_until_local: str = "06:30"
    lease_seconds: float = 3_600.0
    retain_runs: int = 200
    retain_reports: int = 60
    retain_publications: int = 200
    excluded_session_sources: tuple[str, ...] = DEFAULT_EXCLUDED_SESSION_SOURCES
    included_session_sources: tuple[str, ...] = ()
    require_human_binding: bool = False
    allowed_telegram_user_ids: tuple[str, ...] = ()
    reflection_context: Path | None = None
    project_context_command: tuple[str, ...] = ()
    working_context: Path | None = None
    project_results_db: Path | None = None
    self_markers: tuple[str, ...] = DEFAULT_SELF_MARKERS

    def __post_init__(self) -> None:
        staging = self.staging_dir
        if not str(staging) or str(staging) == ".":
            staging = self.dream_state_db.parent / "dream-staging"
            object.__setattr__(self, "staging_dir", staging)
        if not str(self.lifecycle_db) or str(self.lifecycle_db) == ".":
            object.__setattr__(
                self, "lifecycle_db", self.dream_state_db.parent / "lifecycle.sqlite3"
            )
        if not str(self.improvement_db) or str(self.improvement_db) == ".":
            object.__setattr__(
                self, "improvement_db", self.dream_state_db.parent / "improvements.sqlite3"
            )
        for label, value in (
            ("nightly_start_local", self.nightly_start_local),
            ("catchup_until_local", self.catchup_until_local),
        ):
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                raise ConfigError(f"dreaming.{label} must be HH:MM")
        if self.nightly_start_local >= self.catchup_until_local:
            raise ConfigError("dreaming nightly window must start before its same-day cutoff")
        _validate_private_outputs(self)

    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"unknown dreaming timezone: {self.timezone!r}") from exc

    def now(self) -> datetime:
        return datetime.now(tz=self.tzinfo())

    def enabled_profiles(self) -> tuple[ProfileConfig, ...]:
        return tuple(profile for profile in self.profiles if profile.enabled)

    def with_overrides(self, **kwargs: Any) -> "DreamingConfig":
        return replace(self, **kwargs)


def load_dreaming_config(path: Path) -> DreamingConfig:
    """Load the ``dreaming`` object from a manifest JSON file."""

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"manifest is not a JSON object: {path}")
    section = raw.get("dreaming")
    if section is None:
        raise ConfigError(f"manifest has no 'dreaming' object: {path}")
    if not isinstance(section, dict):
        raise ConfigError(f"'dreaming' must be an object in {path}")
    return parse_dreaming_config(section, base=Path(path).parent.resolve())


def parse_dreaming_config(section: Mapping[str, Any], *, base: Path) -> DreamingConfig:
    profiles = _parse_profiles(section.get("profiles"), base=base)
    if not profiles:
        raise ConfigError("dreaming.profiles must list at least one profile")

    return DreamingConfig(
        enabled=bool(section.get("enabled", False)),
        timezone=str(section.get("timezone", DEFAULT_TIMEZONE)),
        profiles=profiles,
        dream_state_db=_abs(base, _require(section, "dream_state_db")),
        reports_dir=_abs(base, _require(section, "reports_dir")),
        import_dir=_abs(base, _require(section, "import_dir")),
        dreams_md=_abs(base, _require(section, "dreams_md")),
        staging_dir=_abs(base, str(section["staging_dir"])) if section.get("staging_dir") else Path(),
        lifecycle_db=(
            _abs(base, str(section["lifecycle_db"]))
            if section.get("lifecycle_db")
            else Path()
        ),
        improvement_db=(
            _abs(base, str(section["improvement_db"]))
            if section.get("improvement_db")
            else Path()
        ),
        model=_parse_model(section.get("model")),
        fallback_models=_parse_fallback_models(section.get("fallback_models")),
        budgets=_parse_dataclass(Budgets, section.get("budgets"), "dreaming.budgets"),
        thresholds=_parse_dataclass(Thresholds, section.get("thresholds"), "dreaming.thresholds"),
        retrieval=_parse_retrieval(section.get("retrieval")),
        lookback_days=max(0, int(section.get("lookback_days", 14))),
        full_backfill_enabled=bool(section.get("full_backfill_enabled", True)),
        loop_interval_minutes=max(1.0, float(section.get("loop_interval_minutes", 2.0))),
        max_rounds=min(128, max(1, int(section.get("max_rounds", 64)))),
        nightly_start_local=str(section.get("nightly_start_local", "03:00")),
        catchup_until_local=str(section.get("catchup_until_local", "06:30")),
        lease_seconds=max(60.0, float(section.get("lease_seconds", 3_600.0))),
        retain_runs=max(1, int(section.get("retain_runs", 200))),
        retain_reports=max(1, int(section.get("retain_reports", 60))),
        retain_publications=max(1, int(section.get("retain_publications", 200))),
        excluded_session_sources=_lower_tuple(
            section.get("excluded_session_sources", DEFAULT_EXCLUDED_SESSION_SOURCES)
        ),
        included_session_sources=_lower_tuple(section.get("included_session_sources", ())),
        require_human_binding=bool(section.get("require_human_binding", False)),
        allowed_telegram_user_ids=tuple(str(x) for x in section.get("allowed_telegram_user_ids", ())),
        reflection_context=_abs(base, str(section["reflection_context"])) if section.get("reflection_context") else None,
        project_context_command=_parse_project_context(section.get("project_context")),
        working_context=_abs(base,str(section["working_context"])) if section.get("working_context") else None,
        project_results_db=_abs(base,str(section["project_results_db"])) if section.get("project_results_db") else None,
        self_markers=_lower_tuple(section.get("self_markers", DEFAULT_SELF_MARKERS)),
    )


def _parse_profiles(value: Any, *, base: Path) -> tuple[ProfileConfig, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("dreaming.profiles must be a list")
    profiles: list[ProfileConfig] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("each dreaming.profiles entry must be an object")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ConfigError("dreaming.profiles entry requires a non-empty 'name'")
        if name in seen:
            raise ConfigError(f"duplicate dreaming profile name: {name!r}")
        seen.add(name)
        state_db = item.get("state_db")
        if not state_db:
            raise ConfigError(f"dreaming profile {name!r} requires 'state_db'")
        lcm_raw = item.get("lcm_db")
        profiles.append(
            ProfileConfig(
                name=name,
                state_db=_abs(base, str(state_db)),
                lcm_db=_abs(base, str(lcm_raw)) if lcm_raw else None,
                enabled=bool(item.get("enabled", True)),
            )
        )
    return tuple(profiles)


def _parse_model(value: Any) -> ModelConfig:
    if value is None:
        return ModelConfig()
    if not isinstance(value, dict):
        raise ConfigError("dreaming.model must be an object")
    command = value.get("command", DEFAULT_MODEL_COMMAND)
    if isinstance(command, str):
        raise ConfigError("dreaming.model.command must be an argv list, not a string")
    if not isinstance(command, (list, tuple)) or not command:
        raise ConfigError("dreaming.model.command must be a non-empty argv list")
    timeouts_raw = value.get("phase_timeout_seconds", {})
    if not isinstance(timeouts_raw, dict):
        raise ConfigError("dreaming.model.phase_timeout_seconds must be an object")
    timeouts = {str(k).lower(): max(1.0, float(v)) for k, v in timeouts_raw.items()}
    for phase in ("light", "rem", "deep"):
        timeouts.setdefault(phase, 300.0)
    return ModelConfig(
        command=tuple(str(part) for part in command),
        model=str(value.get("model", "sonnet")),
        effort=str(value.get("effort", "high")),
        safe_mode=bool(value.get("safe_mode", True)),
        session_persistence=bool(value.get("session_persistence", False)),
        allow_tools=bool(value.get("allow_tools", False)),
        retry_attempts=max(1, int(value.get("retry_attempts", 2))),
        retry_backoff_seconds=max(0.0, float(value.get("retry_backoff_seconds", 2.0))),
        max_output_bytes=max(1_024, int(value.get("max_output_bytes", 2_000_000))),
        phase_timeout_seconds=timeouts,
    )


def _parse_fallback_models(value: Any) -> tuple[ModelConfig, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("dreaming.fallback_models must be a list")
    if len(value) > 3:
        raise ConfigError("dreaming.fallback_models supports at most three entries")
    return tuple(_parse_model(item) for item in value)


def _parse_retrieval(value: Any) -> RetrievalConfig:
    if value is None:
        return RetrievalConfig()
    if not isinstance(value, dict):
        raise ConfigError("dreaming.retrieval must be an object")
    if "ov_binary" in value:
        # Fail loudly rather than silently ignoring it: an operator who still
        # sets this believes queries go through the CLI, and they no longer do.
        raise ConfigError(
            "dreaming.retrieval.ov_binary is no longer used; Dreaming retrieval posts a JSON "
            "body to dreaming.retrieval.endpoint_url. The generic sync ov_binary is unaffected."
        )
    namespaces = value.get("namespaces", DEFAULT_RETRIEVAL_NAMESPACES)
    if not isinstance(namespaces, (list, tuple)):
        raise ConfigError("dreaming.retrieval.namespaces must be a list")
    canonical = value.get("canonical_namespaces", DEFAULT_CANONICAL_NAMESPACES)
    if not isinstance(canonical, (list, tuple)):
        raise ConfigError("dreaming.retrieval.canonical_namespaces must be a list")
    endpoint = str(value.get("endpoint_url", DEFAULT_RETRIEVAL_ENDPOINT)).strip()
    parsed_endpoint = urlsplit(endpoint)
    try:
        loopback = parsed_endpoint.hostname == "localhost" or ipaddress.ip_address(
            parsed_endpoint.hostname or ""
        ).is_loopback
    except ValueError:
        loopback = False
    if parsed_endpoint.scheme != "http" or not loopback or parsed_endpoint.username or parsed_endpoint.password:
        raise ConfigError("dreaming.retrieval.endpoint_url must be an unauthenticated loopback http URL")
    return RetrievalConfig(
        enabled=bool(value.get("enabled", True)),
        endpoint_url=endpoint,
        namespaces=tuple(str(item) for item in namespaces),
        canonical_namespaces=tuple(str(item) for item in canonical),
        timeout_seconds=max(1.0, float(value.get("timeout_seconds", 15.0))),
        limit=max(1, int(value.get("limit", 5))),
        max_response_bytes=max(4_096, int(value.get("max_response_bytes", 1_000_000))),
        levels=tuple(value.get("levels", (0, 1))),
        keyword_enabled=bool(value.get("keyword_enabled", False)),
        source_registry=Path(value["source_registry"]).expanduser() if value.get("source_registry") else None,
        approval_receipts={str(k): Path(v).expanduser() for k, v in value.get("approval_receipts", {}).items()},
    )


def _parse_dataclass(cls: type, value: Any, label: str):
    if value is None:
        return cls()
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be an object")
    known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = sorted(set(value) - known)
    if unknown:
        raise ConfigError(f"{label} has unknown keys: {', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    for name, item in value.items():
        current = cls.__dataclass_fields__[name]  # type: ignore[attr-defined]
        if current.type in ("int", int):
            kwargs[name] = int(item)
        elif current.type in ("float", float):
            kwargs[name] = float(item)
        elif current.type in ("bool", bool):
            kwargs[name] = bool(item)
        else:
            kwargs[name] = item
    return cls(**kwargs)


def _require(section: Mapping[str, Any], key: str) -> str:
    value = section.get(key)
    if not value:
        raise ConfigError(f"dreaming.{key} is required")
    return str(value)


def _abs(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    # Do not resolve() through symlinks here: callers reject symlinked targets
    # explicitly so that a hostile link cannot be silently followed.
    return Path(_normalize(path))


# Every private Dreaming output. `import_dir` is the only directory the manifest
# sync watches, and it indexes whatever it finds there, so none of these may sit
# inside it: a private report, a half-written staging file, DREAMS.md, or the
# Dream state database appearing under the watched tree would publish private
# reflection material as canonical memory.
_PRIVATE_OUTPUTS: tuple[str, ...] = (
    "reports_dir", "staging_dir", "dreams_md", "dream_state_db",
    "lifecycle_db", "improvement_db",
)
_ARTIFACT_DIRECTORIES: tuple[str, ...] = ("reports_dir", "staging_dir", "import_dir")
_ARTIFACT_FILES: tuple[str, ...] = (
    "dreams_md", "dream_state_db", "lifecycle_db", "improvement_db",
)
_WRITABLE_OUTPUTS: tuple[str, ...] = _PRIVATE_OUTPUTS + ("import_dir",)
_PROTECTED_MEMORY_NAMES = frozenset({"user.md", "memory.md"})


def _validate_private_outputs(config: "DreamingConfig") -> None:
    """Fail closed before any run touches the filesystem or the model."""

    watched = config.import_dir
    for name in _WRITABLE_OUTPUTS:
        path = getattr(config, name)
        if path.name.casefold() in _PROTECTED_MEMORY_NAMES:
            raise ConfigError(
                f"dreaming.{name} must never target protected USER.md or MEMORY.md: {path}"
            )
    for name in _PRIVATE_OUTPUTS:
        path = getattr(config, name)
        if path == watched or _is_within(path, watched):
            raise ConfigError(
                f"dreaming.{name} must not be, or live beneath, dreaming.import_dir: the manifest "
                f"indexes every file under the watched import tree, so private Dream output there "
                f"would be published as canonical memory ({path} under {watched})"
            )

    for index, first in enumerate(_ARTIFACT_DIRECTORIES):
        for second in _ARTIFACT_DIRECTORIES[index + 1 :]:
            left, right = getattr(config, first), getattr(config, second)
            if left == right:
                raise ConfigError(f"dreaming.{first} and dreaming.{second} must be distinct directories")
            if _is_within(left, right) or _is_within(right, left):
                raise ConfigError(
                    f"dreaming.{first} and dreaming.{second} must not be nested: each tree is pruned "
                    f"by run id independently, so one would delete the other's artifacts"
                )

    for index, first in enumerate(_ARTIFACT_FILES):
        for second in _ARTIFACT_FILES[index + 1 :]:
            if getattr(config, first) == getattr(config, second):
                raise ConfigError(
                    f"dreaming.{first} and dreaming.{second} must be distinct files"
                )
    for file_name in _ARTIFACT_FILES:
        path = getattr(config, file_name)
        for directory_name in _ARTIFACT_DIRECTORIES:
            directory = getattr(config, directory_name)
            if (path == directory or _is_within(path, directory)
                    or _is_within(directory, path)):
                raise ConfigError(
                    f"dreaming.{file_name} and dreaming.{directory_name} must not collide or be nested"
                )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _normalize(path: Path) -> str:
    import os

    return os.path.normpath(str(path))


def _lower_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise ConfigError("expected a list of strings, got a string")
    return tuple(str(item).strip().lower() for item in value if str(item).strip())


def parse_until(value: str, *, now: datetime, tz: ZoneInfo) -> datetime:
    """Parse ``--until`` as either an ISO datetime or a local ``HH:MM``.

    A bare ``HH:MM`` that has already passed today resolves to tomorrow, which
    is what an operator means when starting an overnight sweep at 23:40 with
    ``--until 10:00``.
    """

    text = value.strip()
    if not text:
        raise ValueError("--until requires a value")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)

    parts = text.split(":")
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        raise ValueError(f"--until must be an ISO datetime or HH:MM, got {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(f"--until has an out-of-range time: {value!r}")
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def default_dreaming_section(*, home: Path | None = None) -> dict[str, Any]:
    """The production-shaped ``dreaming`` block used by manifest scaffolding."""

    root = (home or Path.home()) / ".hermes"
    brain = root / "second-brain"
    profiles = root / "profiles"
    return {
        "enabled": True,
        "timezone": DEFAULT_TIMEZONE,
        "profiles": [
            {"name": "default", "state_db": str(root / "state.db"), "lcm_db": str(root / "lcm.db"), "enabled": True},
            *[
                {
                    "name": name,
                    "state_db": str(profiles / name / "state.db"),
                    "lcm_db": str(profiles / name / "lcm.db"),
                    "enabled": True,
                }
                for name in ("workspace-chat", "reading-list")
            ],
        ],
        "dream_state_db": str(brain / "dream-state.sqlite3"),
        "reports_dir": str(brain / "dreams" / "reports"),
        "import_dir": str(brain / "import" / "dreams"),
        "dreams_md": str(brain / "dreams" / "DREAMS.md"),
        "staging_dir": str(brain / "dreams" / "staging"),
        "lifecycle_db": str(brain / "lifecycle.sqlite3"),
        "improvement_db": str(brain / "improvements.sqlite3"),
        "model": {
            "command": list(DEFAULT_MODEL_COMMAND),
            "model": "sonnet",
            "effort": "high",
            "safe_mode": True,
            "session_persistence": False,
            "allow_tools": False,
            "retry_attempts": 2,
            "retry_backoff_seconds": 2.0,
            "max_output_bytes": 2000000,
            "phase_timeout_seconds": {"light": 420, "rem": 420, "deep": 420},
        },
        "budgets": {
            "sessions_per_round": 12,
            "messages_per_session": 40,
            "characters_per_message": 1200,
            "characters_per_session": 6000,
            "characters_per_batch": 60000,
            "evidence_snippet_characters": 320,
            "max_candidates_per_round": 60,
            "max_connections_per_round": 40,
            "max_retrieval_queries": 8,
            "max_retrieval_hits_per_query": 5,
            "max_retrieval_snippet_characters": 400,
            "max_report_items": 25,
        },
        "thresholds": {
            "promote_min_score": 0.72,
            "promote_min_evidence": 2,
            "promote_min_unique_sessions": 2,
            "promote_min_unique_days": 2,
            "promote_require_user_or_canonical": True,
            "inbox_min_score": 0.55,
            "inbox_min_evidence": 1,
            "hypothesis_min_score": 0.35,
        },
        "retrieval": {
            "enabled": True,
            "endpoint_url": DEFAULT_RETRIEVAL_ENDPOINT,
            "namespaces": list(DEFAULT_RETRIEVAL_NAMESPACES),
            "canonical_namespaces": list(DEFAULT_CANONICAL_NAMESPACES),
            "timeout_seconds": 15,
            "limit": 5,
            "max_response_bytes": 1_000_000,
        },
        "lookback_days": 14,
        "full_backfill_enabled": True,
        "loop_interval_minutes": 2,
        "max_rounds": 64,
        "nightly_start_local": "03:00",
        "catchup_until_local": "06:30",
        "lease_seconds": 3600,
        "retain_runs": 200,
        "retain_reports": 60,
        "retain_publications": 200,
        "excluded_session_sources": list(DEFAULT_EXCLUDED_SESSION_SOURCES),
    }


def dreams_manifest_source(*, home: Path | None = None) -> dict[str, Any]:
    """The manifest source entry that indexes generated Dream artifacts."""

    root = (home or Path.home()) / ".hermes" / "second-brain" / "import" / "dreams"
    return {
        "id": "dream-conclusions",
        "root": str(root),
        "namespace": "dreams",
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
    }


def sequence_or_empty(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _parse_project_context(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or set(value) - {"enabled", "command"} or type(value.get("enabled")) is not bool:
        raise ConfigError("dreaming.project_context must have enabled and command")
    if not value["enabled"]:
        return ()
    command = value.get("command")
    if not isinstance(command, list) or not 1 <= len(command) <= 32 or any(not isinstance(v, str) or not v or len(v) > 8192 for v in command):
        raise ConfigError("dreaming.project_context.command must be a bounded argv list")
    return tuple(command)
