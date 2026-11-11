"""Strict validation for the checked-in Second Brain data ownership map."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_OWNERSHIP_PATH = Path(__file__).with_name("data_ownership.json")
REQUIRED_BOUNDARIES = {
    "durable_fact",
    "temporary_context",
    "routine",
    "episode",
    "self_lesson",
}
REQUIRED_CATEGORIES = {
    "critical_user_facts_preferences",
    "assistant_operational_lessons_environment",
    "raw_session_transcript_history",
    "passive_messaging_context",
    "user_documents_notes",
    "searchable_retrieval_mirror",
    "ttl_temporary_context_episodes",
    "briefing_preferences",
    "personal_tasks_reminders",
    "routines",
    "hermes_work_orders",
    "profile_runtime_config",
}
# Version-1 ownership contract. Each required category is pinned to its expected
# routing and the exact system/role/write-policy of its single master store so
# that a mutation cannot silently reroute a critical master (for example moving
# durable user facts to OpenViking), drop routines, or promote intake staging
# into a master. This table is deliberately keyed to schema_version 1 and fails
# closed: an unrecognised routing or master mismatch is rejected.
VERSION_1_CATEGORY_CONTRACT: dict[str, dict[str, Any]] = {
    "critical_user_facts_preferences": {
        "routing": "durable_fact",
        "master_system": "hermes_profile_memory",
        "master_write_policy": "curated_only",
        "master_locations": ("$HERMES_HOME/memories/USER.md",),
    },
    "assistant_operational_lessons_environment": {
        "routing": "self_lesson",
        "master_system": "hermes_profile_memory",
        "master_write_policy": "curated_only",
        "master_locations": ("$HERMES_HOME/memories/MEMORY.md",),
    },
    "raw_session_transcript_history": {
        "routing": "history",
        "master_system": "hermes_state_lcm",
        "master_write_policy": "hermes_core_only",
        "master_locations": ("Hermes profile session and state databases",),
    },
    "passive_messaging_context": {
        "routing": "passive_context",
        "master_system": "context_inbox_sqlite",
        "master_write_policy": "context_inbox_only",
        "master_locations": ("context-inbox.sqlite3 context_events table",),
    },
    "user_documents_notes": {
        "routing": "document",
        "master_system": "user_source_roots",
        "master_write_policy": "source_owner_only",
        "master_locations": (
            "manifest-declared original roots and explicitly selected note destination",
        ),
    },
    "searchable_retrieval_mirror": {
        "routing": "retrieval_index",
        "master_system": "second_brain_routing_policy",
        "master_write_policy": "policy_only",
        "master_locations": ("data_ownership.json category master declarations",),
    },
    "ttl_temporary_context_episodes": {
        "routing": "temporary_context",
        "master_system": "context_inbox_sqlite",
        "master_write_policy": "context_inbox_only",
        "master_locations": ("context-inbox.sqlite3 context_temporary_memory table",),
    },
    "briefing_preferences": {
        "routing": "preference",
        "master_system": "briefing_preferences_file",
        "master_write_policy": "preferences_cli_only",
        "master_locations": ("$HOME/.hermes/second-brain/briefing-preferences.json",),
    },
    "personal_tasks_reminders": {
        "routing": "personal_task",
        "master_system": "hermes_native_todo_cron",
        "master_write_policy": "native_tools_only",
        "master_locations": ("Hermes native todo, reminder, and cron records",),
    },
    "routines": {
        "routing": "routine",
        "master_system": "hermes_native_todo_cron",
        "master_write_policy": "native_tools_only",
        "master_locations": ("Hermes native recurring reminder and routine workflow records",),
    },
    "hermes_work_orders": {
        "routing": "work_order",
        "master_system": "hermes_native_kanban",
        "master_write_policy": "native_tools_only",
        "master_locations": ("Hermes native Kanban and delegation records",),
    },
    "profile_runtime_config": {
        "routing": "runtime_config",
        "master_system": "profile_config_files",
        "master_write_policy": "profile_owner_only",
        "master_locations": ("$HERMES_HOME/config.yaml",),
    },
}
SUPPORTED_ROLES = {"master", "mirror", "derived_index", "staging"}
SUPPORTED_ROUTING = {
    "durable_fact",
    "self_lesson",
    "history",
    "passive_context",
    "document",
    "retrieval_index",
    "temporary_context",
    "episode",
    "preference",
    "routine",
    "personal_task",
    "work_order",
    "runtime_config",
}
SUPPORTED_SYSTEMS = {
    "hermes_profile_memory",
    "hermes_state_lcm",
    "context_inbox_sqlite",
    "user_source_roots",
    "openviking",
    "second_brain_routing_policy",
    "briefing_preferences_file",
    "hermes_native_todo_cron",
    "hermes_native_kanban",
    "profile_config_files",
}
SUPPORTED_WRITE_POLICIES = {
    "curated_only",
    "hermes_core_only",
    "context_inbox_only",
    "source_owner_only",
    "indexer_only",
    "policy_only",
    "preferences_cli_only",
    "native_tools_only",
    "staging_only",
    "profile_owner_only",
}
_TOP_LEVEL_KEYS = {"schema_version", "title", "routing_boundaries", "categories"}
_CATEGORY_KEYS = {"id", "description", "routing", "stores"}
_STORE_KEYS = {"id", "system", "location", "role", "master", "write_policy"}
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_AMBIGUOUS = {"any", "default", "elsewhere", "misc", "other", "tbd", "unknown", "various"}


class OwnershipConfigError(ValueError):
    """Raised when an ownership file cannot be parsed or validated."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = tuple(errors)


def load_ownership_config(path: Path | str = DEFAULT_OWNERSHIP_PATH) -> dict[str, Any]:
    target = Path(path).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError, OwnershipConfigError) as exc:
        if isinstance(exc, OwnershipConfigError):
            raise
        raise OwnershipConfigError([f"cannot read ownership config {target}: {exc}"]) from exc
    errors = validate_ownership_config(raw)
    if errors:
        raise OwnershipConfigError(errors)
    return raw


def validate_ownership_config(raw: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["ownership config must be a JSON object"]
    _check_keys(raw, _TOP_LEVEL_KEYS, "ownership config", errors)
    if type(raw.get("schema_version")) is not int or raw.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be integer {SCHEMA_VERSION}")
    _check_text(raw.get("title"), "title", errors, minimum=4, maximum=160)

    boundaries = raw.get("routing_boundaries")
    if not isinstance(boundaries, dict):
        errors.append("routing_boundaries must be an object")
    else:
        missing = sorted(REQUIRED_BOUNDARIES - set(boundaries))
        unknown = sorted(set(boundaries) - REQUIRED_BOUNDARIES)
        if missing:
            errors.append("routing_boundaries missing: " + ", ".join(missing))
        if unknown:
            errors.append("routing_boundaries contain unsupported keys: " + ", ".join(unknown))
        for key in sorted(boundaries):
            _check_text(boundaries[key], f"routing_boundaries.{key}", errors, minimum=20, maximum=800)

    categories = raw.get("categories")
    if not isinstance(categories, list) or not categories:
        errors.append("categories must be a non-empty array")
        return errors
    seen_categories: set[str] = set()
    for index, category in enumerate(categories):
        label = f"categories[{index}]"
        if not isinstance(category, dict):
            errors.append(f"{label} must be an object")
            continue
        _check_keys(category, _CATEGORY_KEYS, label, errors)
        category_id = category.get("id")
        if not isinstance(category_id, str) or not _SAFE_ID.fullmatch(category_id):
            errors.append(f"{label}.id must be a safe snake_case identifier")
        elif category_id in seen_categories:
            errors.append(f"duplicate category id: {category_id}")
        else:
            seen_categories.add(category_id)
        _check_text(category.get("description"), f"{label}.description", errors, minimum=12, maximum=500)
        routing = category.get("routing")
        if routing not in SUPPORTED_ROUTING:
            errors.append(f"{label}.routing has unsupported value: {routing!r}")
        stores = category.get("stores")
        if not isinstance(stores, list) or not stores:
            errors.append(f"{label}.stores must be a non-empty array")
            continue
        masters = 0
        master_store: dict[str, Any] | None = None
        seen_stores: set[str] = set()
        for store_index, store in enumerate(stores):
            store_label = f"{label}.stores[{store_index}]"
            if not isinstance(store, dict):
                errors.append(f"{store_label} must be an object")
                continue
            _check_keys(store, _STORE_KEYS, store_label, errors)
            store_id = store.get("id")
            if not isinstance(store_id, str) or not _SAFE_ID.fullmatch(store_id):
                errors.append(f"{store_label}.id must be a safe snake_case identifier")
            elif store_id in seen_stores:
                errors.append(f"{label} has duplicate store id: {store_id}")
            else:
                seen_stores.add(store_id)
            role = store.get("role")
            master = store.get("master")
            write_policy = store.get("write_policy")
            if role not in SUPPORTED_ROLES:
                errors.append(f"{store_label}.role has unsupported value: {role!r}")
            if type(master) is not bool:
                errors.append(f"{store_label}.master must be a boolean")
            elif master:
                masters += 1
                master_store = store
                if role != "master":
                    errors.append(f"{store_label} is a {role!r} but is flagged as a master")
                if write_policy == "staging_only":
                    errors.append(f"{store_label} promotes intake staging to a master")
            elif role == "master":
                errors.append(f"{store_label} has role 'master' but master is false")
            if store.get("system") not in SUPPORTED_SYSTEMS:
                errors.append(f"{store_label}.system has unsupported value: {store.get('system')!r}")
            if write_policy not in SUPPORTED_WRITE_POLICIES:
                errors.append(f"{store_label}.write_policy has unsupported value: {write_policy!r}")
            _check_location(store.get("location"), f"{store_label}.location", errors)
        if masters != 1:
            errors.append(f"{label} must define exactly one master; found {masters}")
        _check_category_contract(category_id, routing, master_store, label, errors)

    missing_categories = sorted(REQUIRED_CATEGORIES - seen_categories)
    if missing_categories:
        errors.append("required categories missing: " + ", ".join(missing_categories))
    return errors


def validation_result(path: Path | str = DEFAULT_OWNERSHIP_PATH) -> dict[str, Any]:
    target = Path(path).expanduser()
    try:
        config = load_ownership_config(target)
    except OwnershipConfigError as exc:
        return {"valid": False, "path": str(target), "errors": list(exc.errors)}
    return {
        "valid": True,
        "path": str(target),
        "schema_version": config["schema_version"],
        "categories": len(config["categories"]),
        "errors": [],
    }


def _check_category_contract(
    category_id: Any,
    routing: Any,
    master_store: dict[str, Any] | None,
    label: str,
    errors: list[str],
) -> None:
    contract = VERSION_1_CATEGORY_CONTRACT.get(category_id) if isinstance(category_id, str) else None
    if contract is None:
        return
    if routing != contract["routing"]:
        errors.append(f"{label}.routing must be {contract['routing']!r} for required category {category_id}")
    if master_store is None:
        return
    if master_store.get("system") != contract["master_system"]:
        errors.append(f"{label} master system must be {contract['master_system']!r} for {category_id}")
    if master_store.get("role") != "master":
        errors.append(f"{label} master must use role 'master' for {category_id}")
    if master_store.get("write_policy") != contract["master_write_policy"]:
        errors.append(
            f"{label} master write_policy must be {contract['master_write_policy']!r} for {category_id}"
        )
    location = master_store.get("location")
    if isinstance(location, str) and re.search(r"\bor\b", location, re.IGNORECASE):
        errors.append(f"{label} master location must not encode ambiguous alternatives")
    approved_locations = contract["master_locations"]
    if location not in approved_locations:
        expected = " or ".join(repr(value) for value in approved_locations)
        errors.append(f"{label} master location must be exactly {expected} for {category_id}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OwnershipConfigError([f"duplicate JSON key: {key}"])
        result[key] = value
    return result


def _check_keys(value: dict[str, Any], expected: set[str], label: str, errors: list[str]) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        errors.append(f"{label} missing keys: {', '.join(missing)}")
    if unknown:
        errors.append(f"{label} contains unknown keys: {', '.join(unknown)}")


def _check_text(value: Any, label: str, errors: list[str], minimum: int, maximum: int) -> None:
    if not isinstance(value, str):
        errors.append(f"{label} must be a string")
        return
    if value != value.strip() or not minimum <= len(value) <= maximum:
        errors.append(f"{label} must be trimmed and {minimum}..{maximum} characters")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        errors.append(f"{label} contains control characters")


def _check_location(value: Any, label: str, errors: list[str]) -> None:
    before = len(errors)
    _check_text(value, label, errors, minimum=2, maximum=300)
    if len(errors) != before or not isinstance(value, str):
        return
    if value.lower() in _AMBIGUOUS:
        errors.append(f"{label} is ambiguous: {value!r}")
    if "\x00" in value or any(token in value for token in ("*", "?", "[", "]")):
        errors.append(f"{label} contains unsafe wildcard or NUL syntax")
    if ".." in Path(value.replace("$", "root")).parts:
        errors.append(f"{label} contains parent traversal")
