"""Validate machine-captured source frames, not LLM-written field assertions.

The future native browser collector owns these envelopes and action receipts.
It must derive record data from the selected DOM records, preserving absence.
This validator cannot attest a browser process or turn a fabricated frame into
an authentic provider response. Native collector/host attestation is separate.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from .contracts import TravelError, instant, now, text

SCHEMA = "travel.browser.capture.v2"
MAX_SOURCE_AGE = timedelta(minutes=3)


def pairs_unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise TravelError("Duplicate source field")
        value[key] = item
    return value


def invalid_number(value):
    raise TravelError("Non-finite source value")


def fresh(observed_at: str, *, requested_at: str | None = None):
    current, at = instant(now()), instant(observed_at)
    if current-at > MAX_SOURCE_AGE or at-current > timedelta(seconds=30):
        raise TravelError("Source acquisition exceeds the current freshness limit")
    if requested_at is not None and at < instant(requested_at)-timedelta(seconds=15):
        raise TravelError("Source was acquired before this request")


def frame(source_text: str, source_url: str, observed_at: str, provider: str) -> dict:
    if not isinstance(source_text,str) or not source_text or len(source_text.encode())>1_800_000:
        raise TravelError("Complete bounded machine-captured browser source required")
    try:
        parsed = json.loads(source_text, object_pairs_hook=pairs_unique, parse_constant=invalid_number)
    except (TypeError, ValueError) as exc:
        raise TravelError("Bound machine-captured source frame required; flat page text is insufficient") from exc
    if (not isinstance(parsed, dict) or parsed.get("schema") != SCHEMA
            or parsed.get("provider") != provider or parsed.get("source_url") != source_url):
        raise TravelError("Original frame envelope does not match this acquisition")
    acquired = parsed.get("observed_at")
    fresh(acquired)
    if abs((instant(acquired)-instant(observed_at)).total_seconds()) > 1:
        raise TravelError("Original acquisition timestamp differs from this frame observation")
    records = parsed.get("records")
    if not isinstance(records, list) or parsed.get("records_complete") is not True:
        raise TravelError("Full original record collection required")
    ids = set()
    for record in records:
        if (not isinstance(record, dict) or record.get("kind") not in {"journey", "unparsed_journey", "fare", "control", "fare_conditions"}
                or not isinstance(record.get("data"), dict)):
            raise TravelError("Invalid original source record")
        rid = text(record.get("id"), "source record identifier")
        if rid in ids:
            raise TravelError("Duplicate original record identifier")
        ids.add(rid)
    return parsed


def records(parsed: dict, kind: str) -> tuple[dict, ...]:
    return tuple(record for record in parsed["records"] if record["kind"] == kind)


def full_hash(source_text: str) -> str:
    return hashlib.sha256(source_text.encode()).hexdigest()


def exact(left, right) -> bool:
    """Distinguish null, false, 0, strings and ordered complete collections."""
    try:
        return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TravelError("Invalid source projection value") from exc


def chain_error(parsed: dict, previous: dict | None, previous_hash: str | None, direction: str) -> str | None:
    """Original page index plus matching observed control and native action receipt.

Metadata must be captured by the native collector, never patched up by an LLM.
An omitted immutable frame breaks its successor's index/hash/receipt chain.
"""
    navigation = parsed.get("navigation")
    if not isinstance(navigation, dict) or type(navigation.get("index")) is not int:
        return "Source frame lacks an observed page index"
    if previous is None:
        if navigation["index"] != 0 or navigation.get("incoming") is not None:
            return "Initial source page/action is not established"
        return None
    prior = previous.get("navigation", {})
    outgoing = prior.get("outgoing")
    if navigation["index"] != prior.get("index", -2)+1:
        return "Original source page chain has a missing or repeated page"
    if not isinstance(outgoing, dict) or outgoing.get("direction") != direction:
        return "Previous original page lacks a matching continuation control"
    labels = {"db":{"later":"Spätere Verbindungen","earlier":"Frühere Verbindungen"},
              "vbb":{"later":"Spätere Abfahrten","earlier":"Frühere Abfahrten"}}
    if outgoing.get("label") != labels[parsed["provider"]][direction]:
        return "Source control is not the selected provider's read-only continuation"
    controls = records(previous, "control")
    matched = [r for r in controls if r["id"] == outgoing.get("control_id")]
    if len(matched) != 1 or not exact(matched[0]["data"],outgoing):
        return "Continuation is not bound to its original source control"
    for key in ("control_id", "target_ref", "label"):
        if not isinstance(outgoing.get(key), str) or not outgoing[key].strip():
            return "Original continuation control is incomplete"
    expected = {"previous_frame_sha256": previous_hash, "action": outgoing, "completed": True}
    if not exact(navigation.get("incoming"),expected):
        return "Continuation action receipt does not link the original source frames"
    if instant(parsed["observed_at"]) < instant(previous["observed_at"]):
        return "Source acquisition moved backwards along the page chain"
    return None
