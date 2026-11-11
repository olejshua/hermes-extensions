"""JSON schemas for the structured phase outputs, plus a local validator.

The same schema dict is handed to the model via ``--json-schema`` and used to
re-validate whatever comes back. Keeping one source of truth means the contract
cannot drift between what we ask for and what we accept. The validator
implements the subset of JSON Schema the schemas actually use and rejects
anything it does not understand rather than passing it through.
"""

from __future__ import annotations

from typing import Any

CANDIDATE_KINDS = (
    "idea",
    "decision",
    "preference",
    "commitment",
    "person",
    "project",
    "open_loop",
    "constraint",
    "pattern",
)
INSIGHT_KINDS = (
    "fact",
    "proposal",
    "connection",
    "contradiction",
    "stale_conflict",
    "blind_spot",
    "hypothesis",
    "open_loop",
)
DURABILITY = ("ephemeral", "seasonal", "durable")
ACTIONABILITY = ("none", "watch", "act")
EVIDENCE_ROLES = ("user", "assistant", "canonical")
VERDICTS = ("promote", "inbox", "hypothesis", "defer", "reject")

MAX_CLAIM_CHARS = 400
MAX_DETAIL_CHARS = 900
MAX_QUOTE_CHARS = 400


def _claim_identity_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["subject", "predicate", "object", "polarity", "scope"],
        "properties": {
            "subject": {"type": "string", "minLength": 1, "maxLength": 120},
            "predicate": {"type": "string", "minLength": 1, "maxLength": 80},
            "object": {"type": "string", "minLength": 1, "maxLength": 180},
            "polarity": {"type": "string", "enum": ["positive", "negative"]},
            "scope": {"type": "string", "minLength": 1, "maxLength": 120},
        },
    }


def _evidence_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": 12,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ref", "role", "quote"],
            "properties": {
                "ref": {"type": "string", "maxLength": 120},
                "role": {"type": "string", "enum": list(EVIDENCE_ROLES)},
                "quote": {"type": "string", "maxLength": MAX_QUOTE_CHARS},
            },
        },
    }


LIGHT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidates", "themes", "queries"],
    "properties": {
        "candidates": {
            "type": "array",
            "maxItems": 80,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "kind", "claim", "claim_identity", "evidence", "confidence",
                    "durability", "actionability",
                ],
                "properties": {
                    "kind": {"type": "string", "enum": list(CANDIDATE_KINDS)},
                    "claim": {"type": "string", "maxLength": MAX_CLAIM_CHARS},
                    "claim_identity": _claim_identity_schema(),
                    "detail": {"type": "string", "maxLength": MAX_DETAIL_CHARS},
                    "evidence": _evidence_schema(),
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "durability": {"type": "string", "enum": list(DURABILITY)},
                    "actionability": {"type": "string", "enum": list(ACTIONABILITY)},
                    "tags": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 48}},
                },
            },
        },
        "themes": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "summary"],
                "properties": {
                    "name": {"type": "string", "maxLength": 80},
                    "summary": {"type": "string", "maxLength": MAX_DETAIL_CHARS},
                    "refs": {"type": "array", "maxItems": 12, "items": {"type": "string", "maxLength": 120}},
                },
            },
        },
        "queries": {
            "type": "array",
            "maxItems": 16,
            "items": {"type": "string", "maxLength": 200},
        },
    },
}

REM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["insights"],
    "properties": {
        "insights": {
            "type": "array",
            "maxItems": 60,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "claim", "evidence", "confidence"],
                "properties": {
                    "kind": {"type": "string", "enum": list(INSIGHT_KINDS)},
                    "claim": {"type": "string", "maxLength": MAX_CLAIM_CHARS},
                    "detail": {"type": "string", "maxLength": MAX_DETAIL_CHARS},
                    "evidence": _evidence_schema(),
                    "candidate_ids": {
                        "type": "array",
                        "maxItems": 12,
                        "items": {"type": "string", "maxLength": 64},
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "contradiction_sides": {
                        "type": "array",
                        "maxItems": 2,
                        "items": {"type": "string", "maxLength": MAX_CLAIM_CHARS},
                    },
                    "reflection": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["scope", "proposed_result", "expected_check", "uncertainty", "importance", "novelty", "language"],
                        "properties": {
                            "scope": {"type": "string", "enum": ["ole_work", "hermes"]},
                            "proposed_result": {"type": "string", "maxLength": 400},
                            "expected_check": {"type": "string", "maxLength": 400},
                            "uncertainty": {"type": "string", "maxLength": 400},
                            "importance": {"type": "string", "enum": ["normal", "important"]},
                            "novelty": {"type": "string", "enum": ["new_experiment", "existing_request", "already_done", "unknown"]},
                            "language": {"type": "string", "enum": ["de"]},
                        },
                    },
                },
            },
        },
        "notes": {"type": "string", "maxLength": MAX_DETAIL_CHARS},
    },
}

QUALITY_CRITERIA = (
    "goal_relevance", "current_evidence", "concrete_effect", "novelty",
    "counterarguments", "uncertainty", "insight_value",
)

QUALITY_REVIEW_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"], "additionalProperties": False,
    "required": ["version", "decision", "novelty_key", "duplicate_of", "summary", *QUALITY_CRITERIA],
    "properties": {
        "version": {"type": "string", "enum": ["dream-quality/v1"]},
        "decision": {"type": "string", "enum": ["useful", "withhold"]},
        "novelty_key": {"type": "string", "maxLength": 100},
        "duplicate_of": {"type": "array", "maxItems": 4,
                         "items": {"type": "string", "maxLength": 64}},
        "summary": {"type": "string", "maxLength": 360},
        **{key: {"type": "object", "additionalProperties": False,
                 "required": ["passed", "reason"],
                 "properties": {"passed": {"type": "boolean"},
                                "reason": {"type": "string", "maxLength": 600}}}
           for key in QUALITY_CRITERIA},
    },
}

DEEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reviews"],
    "properties": {
        "reviews": {
            "type": "array",
            "maxItems": 120,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "supported", "verdict", "rationale"],
                "properties": {
                    "id": {"type": "string", "maxLength": 64},
                    "supported": {"type": "boolean"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "rationale": {"type": "string", "maxLength": MAX_DETAIL_CHARS},
                    "quality_review": QUALITY_REVIEW_SCHEMA,
                    "action_review": {
                        "type": "object", "additionalProperties": False,
                        "required": ["relation", "target_refs", "rationale"],
                        "properties": {
                            "relation": {"type": "string", "enum": ["existing_project_work", "independent_new_idea", "unknown"]},
                            "target_refs": {"type": "array", "maxItems": 8,
                                            "items": {"type": "string", "maxLength": 120}},
                            "rationale": {"type": "string", "minLength": 1, "maxLength": 600},
                        },
                    },
                    "concerns": {
                        "type": "array",
                        "maxItems": 6,
                        "items": {"type": "string", "maxLength": 200},
                    },
                },
            },
        },
        "summary": {"type": "string", "maxLength": 4000},
    },
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "light": LIGHT_SCHEMA,
    "rem": REM_SCHEMA,
    "deep": DEEP_SCHEMA,
}


class SchemaError(ValueError):
    """Raised when model output does not match the declared schema."""


def validate(instance: Any, schema: dict[str, Any], *, path: str = "$") -> None:
    """Validate ``instance`` against the supported JSON Schema subset.

    Raises :class:`SchemaError` with a path-qualified message on the first
    violation. Unknown schema keywords are ignored, but unknown *instance*
    properties are rejected wherever ``additionalProperties`` is ``False``.
    """

    expected = schema.get("type")
    if isinstance(expected, list):
        if instance is None and "null" in expected:
            return
        concrete = [value for value in expected if value != "null"]
        if len(concrete) != 1:
            raise SchemaError(f"{path}: unsupported schema union")
        expected = concrete[0]
    if expected == "object":
        if not isinstance(instance, dict):
            raise SchemaError(f"{path}: expected object, got {_kind(instance)}")
        for key in schema.get("required", []):
            if key not in instance:
                raise SchemaError(f"{path}: missing required property {key!r}")
        properties: dict[str, Any] = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(instance) - set(properties))
            if unknown:
                raise SchemaError(f"{path}: unknown properties: {', '.join(unknown)}")
        for key, value in instance.items():
            sub = properties.get(key)
            if sub is not None:
                validate(value, sub, path=f"{path}.{key}")
        return

    if expected == "array":
        if not isinstance(instance, list):
            raise SchemaError(f"{path}: expected array, got {_kind(instance)}")
        min_items = schema.get("minItems")
        if min_items is not None and len(instance) < int(min_items):
            raise SchemaError(f"{path}: expected at least {min_items} items, got {len(instance)}")
        max_items = schema.get("maxItems")
        if max_items is not None and len(instance) > int(max_items):
            raise SchemaError(f"{path}: expected at most {max_items} items, got {len(instance)}")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(instance):
                validate(item, item_schema, path=f"{path}[{index}]")
        return

    if expected == "string":
        if not isinstance(instance, str):
            raise SchemaError(f"{path}: expected string, got {_kind(instance)}")
        min_length = schema.get("minLength")
        if min_length is not None and len(instance) < int(min_length):
            raise SchemaError(f"{path}: string shorter than {min_length} characters")
        max_length = schema.get("maxLength")
        if max_length is not None and len(instance) > int(max_length):
            raise SchemaError(f"{path}: string longer than {max_length} characters")
        enum = schema.get("enum")
        if enum is not None and instance not in enum:
            raise SchemaError(f"{path}: {instance!r} is not one of {sorted(enum)}")
        return

    if expected == "number":
        if isinstance(instance, bool) or not isinstance(instance, (int, float)):
            raise SchemaError(f"{path}: expected number, got {_kind(instance)}")
        minimum = schema.get("minimum")
        if minimum is not None and instance < minimum:
            raise SchemaError(f"{path}: {instance} is below minimum {minimum}")
        maximum = schema.get("maximum")
        if maximum is not None and instance > maximum:
            raise SchemaError(f"{path}: {instance} is above maximum {maximum}")
        return

    if expected == "integer":
        if isinstance(instance, bool) or not isinstance(instance, int):
            raise SchemaError(f"{path}: expected integer, got {_kind(instance)}")
        minimum = schema.get("minimum")
        if minimum is not None and instance < minimum:
            raise SchemaError(f"{path}: {instance} is below minimum {minimum}")
        maximum = schema.get("maximum")
        if maximum is not None and instance > maximum:
            raise SchemaError(f"{path}: {instance} is above maximum {maximum}")
        return

    if expected == "boolean":
        if not isinstance(instance, bool):
            raise SchemaError(f"{path}: expected boolean, got {_kind(instance)}")
        return

    if expected is not None:
        raise SchemaError(f"{path}: unsupported schema type {expected!r}")


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__
