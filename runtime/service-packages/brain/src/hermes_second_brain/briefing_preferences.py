"""Typed, private preferences for Context Inbox daily briefings."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .context_inbox import (
    MAX_SECTION_LIMIT,
    MIN_SECTION_LIMIT,
    _prepare_private_file_parent,
    atomic_text_writer,
)


SCHEMA_VERSION = 1
SECTIONS = ("events", "reminders", "habits")
MIN_OUTPUT_CHARACTERS = 100
MAX_OUTPUT_CHARACTERS = 20_000
MAX_EXCLUDED_PLATFORMS = 32
_PLATFORM = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_TOP_LEVEL_KEYS = {
    "schema_version",
    "included_sections",
    "limits",
    "max_output_characters",
    "quiet_when_empty",
    "excluded_platforms",
}
_LIMIT_KEYS = set(SECTIONS)


def default_preferences_path() -> Path:
    configured = os.environ.get("HERMES_BRIEFING_PREFERENCES")
    return Path(configured or "~/.hermes/second-brain/briefing-preferences.json").expanduser()


@dataclass(frozen=True)
class BriefingPreferences:
    schema_version: int = SCHEMA_VERSION
    included_sections: tuple[str, ...] = SECTIONS
    event_limit: int = 20
    reminder_limit: int = 10
    habit_limit: int = 10
    max_output_characters: int = 3900
    quiet_when_empty: bool = True
    excluded_platforms: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "included_sections": list(self.included_sections),
            "limits": {
                "events": self.event_limit,
                "reminders": self.reminder_limit,
                "habits": self.habit_limit,
            },
            "max_output_characters": self.max_output_characters,
            "quiet_when_empty": self.quiet_when_empty,
            "excluded_platforms": list(self.excluded_platforms),
        }


DEFAULT_PREFERENCES = BriefingPreferences()


def parse_preferences(raw: Any) -> BriefingPreferences:
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise ValueError("briefing preferences must be a JSON object")
    if any(not isinstance(key, str) for key in raw):
        raise ValueError("briefing preference keys must be strings")
    missing = sorted(_TOP_LEVEL_KEYS - set(raw))
    unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
    if missing:
        errors.append("missing keys: " + ", ".join(missing))
    if unknown:
        errors.append("unknown keys: " + ", ".join(unknown))
    if type(raw.get("schema_version")) is not int or raw.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be integer {SCHEMA_VERSION}")

    sections = raw.get("included_sections")
    normalized_sections: tuple[str, ...] = ()
    if not isinstance(sections, list) or any(not isinstance(value, str) for value in sections):
        errors.append("included_sections must be an array of section names")
    elif len(sections) != len(set(sections)):
        errors.append("included_sections must not contain duplicates")
    elif any(value not in SECTIONS for value in sections):
        errors.append("included_sections supports only events, reminders, and habits")
    else:
        normalized_sections = tuple(value for value in SECTIONS if value in sections)

    limits = raw.get("limits")
    parsed_limits: dict[str, int] = {}
    if not isinstance(limits, dict):
        errors.append("limits must be an object")
    elif any(not isinstance(key, str) for key in limits):
        errors.append("limits keys must be strings")
    else:
        missing_limits = sorted(_LIMIT_KEYS - set(limits))
        unknown_limits = sorted(set(limits) - _LIMIT_KEYS)
        if missing_limits:
            errors.append("limits missing keys: " + ", ".join(missing_limits))
        if unknown_limits:
            errors.append("limits contain unknown keys: " + ", ".join(unknown_limits))
        for section in SECTIONS:
            value = limits.get(section)
            if type(value) is not int or not MIN_SECTION_LIMIT <= value <= MAX_SECTION_LIMIT:
                errors.append(f"limits.{section} must be an integer from {MIN_SECTION_LIMIT} to {MAX_SECTION_LIMIT}")
            else:
                parsed_limits[section] = value

    max_characters = raw.get("max_output_characters")
    if type(max_characters) is not int or not MIN_OUTPUT_CHARACTERS <= max_characters <= MAX_OUTPUT_CHARACTERS:
        errors.append(
            f"max_output_characters must be an integer from {MIN_OUTPUT_CHARACTERS} to {MAX_OUTPUT_CHARACTERS}"
        )
    quiet = raw.get("quiet_when_empty")
    if type(quiet) is not bool:
        errors.append("quiet_when_empty must be a boolean")

    excluded = raw.get("excluded_platforms")
    normalized_excluded: tuple[str, ...] = ()
    if not isinstance(excluded, list) or any(not isinstance(value, str) for value in excluded):
        errors.append("excluded_platforms must be an array of platform identifiers")
    elif len(excluded) > MAX_EXCLUDED_PLATFORMS:
        errors.append(f"excluded_platforms may contain at most {MAX_EXCLUDED_PLATFORMS} values")
    else:
        lowered = [value.lower() for value in excluded]
        if len(lowered) != len(set(lowered)):
            errors.append("excluded_platforms must not contain duplicates")
        if any(not _PLATFORM.fullmatch(value) for value in lowered):
            errors.append("excluded_platforms contains an unsafe platform identifier")
        else:
            normalized_excluded = tuple(sorted(lowered))

    if errors:
        raise ValueError("; ".join(errors))
    return BriefingPreferences(
        included_sections=normalized_sections,
        event_limit=parsed_limits["events"],
        reminder_limit=parsed_limits["reminders"],
        habit_limit=parsed_limits["habits"],
        max_output_characters=max_characters,
        quiet_when_empty=quiet,
        excluded_platforms=normalized_excluded,
    )


def load_preferences(path: Path | str | None = None) -> BriefingPreferences:
    target = Path(path).expanduser() if path is not None else default_preferences_path()
    _reject_unsafe_preference_path(target)
    if not target.exists():
        return DEFAULT_PREFERENCES
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"briefing preference path is not a regular file: {target}")
        if info.st_mode & 0o077:
            raise ValueError(f"briefing preference file must be private (0600): {target}")
        if info.st_size > 64 * 1024:
            raise ValueError("briefing preference file is too large")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            raw = json.load(handle, object_pairs_hook=_unique_object)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return parse_preferences(raw)


def write_preferences(preferences: BriefingPreferences, path: Path | str | None = None) -> Path:
    validated = parse_preferences(preferences.as_dict())
    target = Path(path).expanduser() if path is not None else default_preferences_path()
    _reject_unsafe_preference_path(target)
    _prepare_private_file_parent(target.parent)
    _reject_unsafe_preference_path(target)
    with atomic_text_writer(target) as handle:
        json.dump(validated.as_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return target


def update_preferences(
    updates: Mapping[str, Any],
    path: Path | str | None = None,
) -> BriefingPreferences:
    if not isinstance(updates, Mapping):
        raise ValueError("briefing preference updates must be an object")
    if any(not isinstance(key, str) for key in updates):
        raise ValueError("briefing preference update keys must be strings")
    current = load_preferences(path).as_dict()
    for key, value in updates.items():
        if key == "limits":
            if not isinstance(value, Mapping):
                raise ValueError("limits update must be an object")
            if any(not isinstance(limit_key, str) for limit_key in value):
                raise ValueError("limits update keys must be strings")
            current["limits"].update(value)
        else:
            current[key] = value
    parsed = parse_preferences(current)
    write_preferences(parsed, path)
    return parsed


def reset_preferences(path: Path | str | None = None, *, force: bool = False) -> BriefingPreferences:
    custom_target = path is not None or "HERMES_BRIEFING_PREFERENCES" in os.environ
    target = Path(path).expanduser() if path is not None else default_preferences_path()
    _reject_unsafe_preference_path(target)
    if custom_target and not force and target.exists():
        # Do not blindly overwrite an existing arbitrary/invalid custom target.
        # If it already parses cleanly as briefing preferences, reset normally.
        # Otherwise refuse without modifying a single byte unless force is set.
        try:
            load_preferences(target)
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"refusing to overwrite existing non-preference content at {target}; "
                "pass --force to replace it"
            ) from exc
    write_preferences(DEFAULT_PREFERENCES, target)
    return DEFAULT_PREFERENCES


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_unsafe_preference_path(path: Path) -> None:
    target = path.expanduser().absolute()
    for index, candidate in enumerate((target, *target.parents)):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISLNK(info.st_mode):
            continue
        if index == 0:
            # The final component is never followed (also guarded by O_NOFOLLOW).
            raise ValueError(f"refusing symlinked briefing preference path: {candidate}")
        # Ancestor symlinks are tolerated only when root owns them (for example
        # the macOS /var -> /private/var system compatibility link). A symlink
        # owned by any non-root UID -- ours or another user's -- is rejected.
        if info.st_uid != 0:
            raise ValueError(
                f"refusing untrusted symlink ancestor of briefing preference path: {candidate}"
            )
