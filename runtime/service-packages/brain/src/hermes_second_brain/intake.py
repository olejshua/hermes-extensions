"""Typed, deterministic staging for model-classified mixed user intake."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .context_inbox import ContextInbox, DEFAULT_CONTEXT_DB
from .temporary_memory import (
    MAX_SOURCE_REF_CHARACTERS,
    MAX_TTL,
    canonical_utc,
    coerce_utc_clock,
    parse_utc_timestamp,
    validate_bounded_text,
    validate_confidence,
)


SCHEMA_VERSION = 1
MAX_ITEMS = 50
MAX_SUMMARY_CHARACTERS = 180
MAX_CONTENT_CHARACTERS = 4000
MAX_DUE_HINT_CHARACTERS = 200
MAX_BUNDLE_BYTES = 512 * 1024
DESTINATIONS = (
    "reminder",
    "note",
    "memory_fact",
    "temporary_context",
    "routine",
    "personal_task",
    "hermes_work_order",
)
ACTIONS_BY_DESTINATION = {
    "reminder": {"create_reminder"},
    "note": {"create_note", "append_note"},
    "memory_fact": {"propose_self_lesson", "propose_user_fact"},
    "temporary_context": {"store_temporary_context"},
    "routine": {"create_routine"},
    "personal_task": {"create_personal_task"},
    "hermes_work_order": {"create_work_order"},
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BUNDLE_REQUIRED_KEYS = {"schema_version", "bundle_id", "items"}
_BUNDLE_OPTIONAL_KEYS = {"source_ref"}
_ITEM_REQUIRED_KEYS = {
    "id",
    "summary",
    "content",
    "destination",
    "action",
    "confidence",
    "requires_approval",
    "sensitive",
}
_ITEM_OPTIONAL_KEYS = {"external", "due_hint", "expires_at", "source_ref"}


def load_intake_bundle(path: Path | str) -> dict[str, Any]:
    target = Path(path)
    if str(target) == "-":
        raise ValueError("stdin must be supplied by the CLI")
    if target.is_symlink():
        raise ValueError(f"refusing symlinked intake file: {target}")
    descriptor = -1
    try:
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"intake path is not a regular file: {target}")
        if info.st_size > MAX_BUNDLE_BYTES:
            raise ValueError(f"intake JSON exceeds {MAX_BUNDLE_BYTES} bytes")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            raw = json.load(handle, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read intake bundle {target}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(raw, dict):
        raise ValueError("intake bundle must be a JSON object")
    return raw


def decode_intake_bundle(value: str) -> dict[str, Any]:
    try:
        raw = json.loads(value, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"invalid intake JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("intake bundle must be a JSON object")
    return raw


def validate_intake_bundle(
    raw: Any,
    *,
    now: str | dt.datetime | None = None,
    enforce_freshness: bool = True,
) -> dict[str, Any]:
    clock = coerce_utc_clock(now)
    if not isinstance(raw, dict):
        raise ValueError("intake bundle must be a JSON object")
    if any(not isinstance(key, str) for key in raw):
        raise ValueError("intake bundle keys must be strings")
    missing = sorted(_BUNDLE_REQUIRED_KEYS - set(raw))
    unknown = set(raw) - _BUNDLE_REQUIRED_KEYS - _BUNDLE_OPTIONAL_KEYS
    errors: list[str] = []
    if missing:
        errors.append("bundle missing keys: " + ", ".join(missing))
    if unknown:
        # Never echo model-supplied key names back to the tool caller.
        errors.append("bundle contains unsupported keys")
    if type(raw.get("schema_version")) is not int or raw.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be integer {SCHEMA_VERSION}")
    try:
        bundle_id = _validate_stable_id(raw.get("bundle_id"), "bundle_id")
    except ValueError as exc:
        errors.append(str(exc))
        bundle_id = "invalid"
    try:
        source_ref = validate_bounded_text(
            raw.get("source_ref", ""), "source_ref", MAX_SOURCE_REF_CHARACTERS, required=False
        )
    except ValueError as exc:
        errors.append(str(exc))
        source_ref = ""
    items = raw.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        errors.append(f"items must be an array containing 1..{MAX_ITEMS} objects")
        items = []

    normalized_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(items):
        label = f"items[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            continue
        if any(not isinstance(key, str) for key in item):
            errors.append(f"{label} keys must be strings")
            continue
        missing_item = sorted(_ITEM_REQUIRED_KEYS - set(item))
        unknown_item = set(item) - _ITEM_REQUIRED_KEYS - _ITEM_OPTIONAL_KEYS
        if missing_item:
            errors.append(f"{label} missing keys: {', '.join(missing_item)}")
        if unknown_item:
            # Never echo model-supplied key names back to the tool caller.
            errors.append(f"{label} contains unsupported keys")
        try:
            item_id = _validate_stable_id(item.get("id"), f"{label}.id")
            if item_id in seen_ids:
                raise ValueError(f"{label}.id duplicates an earlier item id")
            seen_ids.add(item_id)
        except ValueError as exc:
            errors.append(str(exc))
            item_id = f"invalid-{index}"
        try:
            summary = validate_bounded_text(
                item.get("summary"), f"{label}.summary", MAX_SUMMARY_CHARACTERS, required=True
            )
            if "\n" in summary or "\r" in summary:
                raise ValueError(f"{label}.summary must be a single line")
        except ValueError as exc:
            errors.append(str(exc))
            summary = "invalid"
        try:
            content = validate_bounded_text(
                item.get("content"), f"{label}.content", MAX_CONTENT_CHARACTERS, required=True
            )
        except ValueError as exc:
            errors.append(str(exc))
            content = "invalid"
        destination = item.get("destination")
        if destination not in DESTINATIONS:
            # State only the allowed enum; never echo the model-supplied value.
            errors.append(f"{label}.destination must be one of: " + ", ".join(DESTINATIONS))
            destination = "reminder"
        action = item.get("action")
        if not isinstance(action, str) or action not in ACTIONS_BY_DESTINATION.get(destination, set()):
            supported = ", ".join(sorted(ACTIONS_BY_DESTINATION.get(destination, set())))
            errors.append(f"{label}.action must be one of: {supported}")
            action = sorted(ACTIONS_BY_DESTINATION[destination])[0]
        try:
            confidence = validate_confidence(item.get("confidence"))
        except ValueError as exc:
            errors.append(f"{label}.{exc}")
            confidence = 0.0
        requires_approval = item.get("requires_approval")
        sensitive = item.get("sensitive")
        external = item.get("external", False)
        for field, value in (
            ("requires_approval", requires_approval),
            ("sensitive", sensitive),
            ("external", external),
        ):
            if type(value) is not bool:
                errors.append(f"{label}.{field} must be a boolean")
        requires_approval = requires_approval if type(requires_approval) is bool else True
        sensitive = sensitive if type(sensitive) is bool else True
        external = external if type(external) is bool else True
        try:
            due_hint = validate_bounded_text(
                item.get("due_hint", ""),
                f"{label}.due_hint",
                MAX_DUE_HINT_CHARACTERS,
                required=False,
            )
        except ValueError as exc:
            errors.append(str(exc))
            due_hint = ""
        try:
            item_source_ref = validate_bounded_text(
                item.get("source_ref", source_ref),
                f"{label}.source_ref",
                MAX_SOURCE_REF_CHARACTERS,
                required=False,
            )
        except ValueError as exc:
            errors.append(str(exc))
            item_source_ref = ""

        expires_at = ""
        supplied_expiry = item.get("expires_at")
        if destination == "temporary_context":
            try:
                # Structural parse always runs; the future-expiry and max-TTL
                # freshness bounds are insert-only so an identical staged retry
                # stays idempotent even after the expiry has elapsed.
                expiry = parse_utc_timestamp(supplied_expiry, f"{label}.expires_at")
                if enforce_freshness:
                    if expiry <= clock:
                        raise ValueError(f"{label}.expires_at must be in the future")
                    if expiry - clock > MAX_TTL:
                        raise ValueError(f"{label}.expires_at exceeds the maximum TTL of 365 days")
                expires_at = canonical_utc(expiry)
            except ValueError as exc:
                errors.append(str(exc))
        elif supplied_expiry not in (None, ""):
            errors.append(f"{label}.expires_at is supported only for temporary_context")

        normalized_items.append(
            {
                "id": item_id,
                "summary": summary,
                "content": content,
                "destination": destination,
                "action": action,
                "confidence": confidence,
                "requires_approval": requires_approval,
                "sensitive": sensitive,
                "external": external,
                "due_hint": due_hint,
                "expires_at": expires_at,
                "source_ref": item_source_ref,
                "status": "pending_approval"
                if requires_approval or sensitive or external
                else "staged",
                "execution_status": "not_executed",
            }
        )
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "source_ref": source_ref,
        "items": normalized_items,
    }


def preview_intake_bundle(
    raw: Any,
    *,
    now: str | dt.datetime | None = None,
) -> dict[str, Any]:
    normalized = validate_intake_bundle(raw, now=now)
    return _result(normalized, committed=False, created=False)


def stage_intake_bundle(
    raw: Any,
    *,
    db_path: Path | str = DEFAULT_CONTEXT_DB,
    now: str | dt.datetime | None = None,
) -> dict[str, Any]:
    clock = coerce_utc_clock(now)
    # Structural/canonical normalization first, WITHOUT insert-only freshness
    # enforcement, so an identical retry of an already-staged bundle stays
    # idempotent even after a temporary item's expiry has elapsed. The canonical
    # payload (and therefore its hash) is independent of the clock.
    normalized = validate_intake_bundle(raw, now=clock, enforce_freshness=False)
    identity_key = normalized["bundle_id"]
    staging_bundle_id = "intake_" + hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:32]
    payload_json = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    result = _result(normalized, committed=True, created=True)
    stamp = canonical_utc(clock)
    inbox = ContextInbox(db_path)
    with inbox.closing_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM context_intake_bundles WHERE identity_key=?", (identity_key,)
        ).fetchone()
        if existing is not None:
            if existing["payload_sha256"] != payload_hash:
                raise ValueError("bundle_id already exists with different staged content")
            existing_items = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM context_intake_items WHERE staging_bundle_id=? ORDER BY position",
                    (existing["staging_bundle_id"],),
                )
            ]
            return _result_from_rows(dict(existing), existing_items, created=False)
        # New staging only: now apply insert-only freshness validation so a fresh
        # bundle with an already-elapsed expiry (or over-long TTL) is rejected.
        validate_intake_bundle(raw, now=clock, enforce_freshness=True)
        conn.execute(
            """
            INSERT INTO context_intake_bundles(
              staging_bundle_id,identity_key,schema_version,payload_sha256,source_ref,item_count,
              pending_approval_count,status,confirmation,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                staging_bundle_id,
                identity_key,
                SCHEMA_VERSION,
                payload_hash,
                normalized["source_ref"],
                result["item_count"],
                result["pending_approval_count"],
                result["status"],
                result["confirmation"],
                stamp,
            ),
        )
        for position, item in enumerate(normalized["items"]):
            staging_item_id = "sti_" + hashlib.sha256(
                f"{identity_key}\0{item['id']}".encode("utf-8")
            ).hexdigest()[:32]
            conn.execute(
                """
                INSERT INTO context_intake_items(
                  staging_item_id,staging_bundle_id,source_item_id,position,summary,content,
                  destination,action,confidence,requires_approval,sensitive,external,status,
                  execution_status,due_hint,expires_at,source_ref,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    staging_item_id,
                    staging_bundle_id,
                    item["id"],
                    position,
                    item["summary"],
                    item["content"],
                    item["destination"],
                    item["action"],
                    item["confidence"],
                    int(item["requires_approval"]),
                    int(item["sensitive"]),
                    int(item["external"]),
                    item["status"],
                    "not_executed",
                    item["due_hint"],
                    item["expires_at"],
                    item["source_ref"],
                    stamp,
                ),
            )
    result["staging_bundle_id"] = staging_bundle_id
    return result


def _result(normalized: dict[str, Any], *, committed: bool, created: bool) -> dict[str, Any]:
    counts = {destination: 0 for destination in DESTINATIONS}
    pending = 0
    items = []
    for item in normalized["items"]:
        counts[item["destination"]] += 1
        pending += item["status"] == "pending_approval"
        items.append(
            {
                "id": item["id"],
                "destination": item["destination"],
                "action": item["action"],
                "status": item["status"],
                "execution_status": "not_executed",
            }
        )
    routes = {key: counts[key] for key in DESTINATIONS if counts[key]}
    verb = "Staged" if committed else "Validated"
    route_text = ", ".join(routes)
    pending_text = f"; {pending} pending approval" if pending else ""
    effect = "nothing was executed" if committed else "nothing was staged or executed"
    confirmation = f"{verb} {len(items)} items across {route_text}{pending_text}; {effect}."
    return {
        "ok": True,
        "bundle_id": normalized["bundle_id"],
        "staging_bundle_id": "",
        "committed": committed,
        "created": created,
        "status": "pending_approval" if pending else ("staged" if committed else "valid"),
        "item_count": len(items),
        "pending_approval_count": pending,
        "routes": routes,
        "items": items,
        "confirmation": confirmation,
        "executed": False,
    }


def _result_from_rows(bundle: dict[str, Any], rows: list[dict[str, Any]], *, created: bool) -> dict[str, Any]:
    routes = {destination: 0 for destination in DESTINATIONS}
    items = []
    for row in rows:
        routes[row["destination"]] += 1
        items.append(
            {
                "id": row["source_item_id"],
                "destination": row["destination"],
                "action": row["action"],
                "status": row["status"],
                "execution_status": row["execution_status"],
            }
        )
    return {
        "ok": True,
        "bundle_id": bundle["identity_key"],
        "staging_bundle_id": bundle["staging_bundle_id"],
        "committed": True,
        "created": created,
        "status": bundle["status"],
        "item_count": bundle["item_count"],
        "pending_approval_count": bundle["pending_approval_count"],
        "routes": {key: value for key, value in routes.items() if value},
        "items": items,
        "confirmation": bundle["confirmation"],
        "executed": False,
    }


def _validate_stable_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{label} must be a stable 1..128 character identifier")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # Never echo the model-supplied key name back to the tool caller.
            raise ValueError("intake bundle contains a duplicate JSON key")
        result[key] = value
    return result
