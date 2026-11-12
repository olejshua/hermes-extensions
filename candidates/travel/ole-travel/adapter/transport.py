"""Community transport.rest provider; GET-only, no account or API key.

Provider responses are untrusted source data. Coverage means the pages the
provider exposes for the requested window, not every possible rail itinerary.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlencode

from .contracts import (Journey, JourneyQuery, JourneyResult, Leg,
                        ProviderUnavailable, Station, TravelError, instant, now, text)

BASES = {"vbb": "https://v6.vbb.transport.rest", "db": "https://v6.db.transport.rest"}


class CurlReadOnly:
    """Fixed public hosts/paths. No redirects, shell, auth, writes, or hidden retries."""
    def __init__(self, on_response=None):
        self.on_response = on_response

    def __call__(self, provider: str, path: str, params: dict):
        if provider not in BASES or path not in {"/locations", "/journeys"}:
            raise TravelError("Unapproved public travel endpoint")
        url = BASES[provider] + path + "?" + urlencode(params)
        try:
            result = subprocess.run(
                ["curl", "-q", "--silent", "--show-error", "--max-time", "20",
                 "--connect-timeout", "8", "--max-filesize", "5000000",
                 "--write-out", "\n%{http_code}", "--", url],
                capture_output=True, timeout=22, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderUnavailable(provider, "public read unavailable") from exc
        body, _, status = result.stdout.rpartition(b"\n")
        code = int(status) if status.isdigit() else None
        if result.returncode or code != 200:
            # stderr/body may contain arbitrary source data; never put them in logs.
            raise ProviderUnavailable(provider, "public read failed", code)
        if len(body) > 5_000_000:
            raise ProviderUnavailable(provider, "response exceeds full-source budget")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderUnavailable(provider, "invalid full JSON response") from exc
        if self.on_response is not None:
            self.on_response(provider, path, params, body)
        return data, hashlib.sha256(body).hexdigest()


def optional_time(value):
    if value is not None:
        instant(value)
    return value


def optional_int(value):
    if value is not None and type(value) is not int:
        raise TravelError("Delay must be seconds or null")
    return value


def remarks(value) -> tuple[dict, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
        raise TravelError("Invalid complete remarks collection")
    # Preserve complete source; no executable HTML or instructions are consumed.
    for remark in value:
        for key in ("text", "summary"):
            if remark.get(key) is not None:
                text(remark[key], "remark")
    return tuple(value)


def parse_journey(raw: dict) -> Journey:
    if not isinstance(raw, dict) or not isinstance(raw.get("legs"), list) or not raw["legs"]:
        raise TravelError("Journey needs full legs")
    legs = []
    for item in raw["legs"]:
        if not isinstance(item, dict):
            raise TravelError("Invalid leg")
        origin, destination = item.get("origin"), item.get("destination")
        if not isinstance(origin, dict) or not isinstance(destination, dict):
            raise TravelError("Leg needs origin and destination")
        walking = item.get("walking", False)
        if type(walking) is not bool:
            raise TravelError("Invalid walking flag")
        line = item.get("line")
        if not walking and not isinstance(line, dict):
            raise TravelError("Public leg needs source line/product")
        line = line or {}
        cancelled = item.get("cancelled")
        if cancelled is not None and type(cancelled) is not bool:
            raise TravelError("Invalid cancellation flag")
        leg = Leg(
            text(origin.get("name"), "origin name"), text(destination.get("name"), "destination name"),
            optional_time(item.get("departure")), optional_time(item.get("plannedDeparture")),
            optional_time(item.get("arrival")), optional_time(item.get("plannedArrival")),
            optional_int(item.get("departureDelay")), optional_int(item.get("arrivalDelay")),
            text(line.get("name"), "line name", required=not walking),
            text(line.get("product"), "line product", required=not walking), walking, cancelled,
            text(item.get("departurePlatform"), "departure platform", required=False),
            text(item.get("arrivalPlatform"), "arrival platform", required=False),
            remarks(item.get("remarks")), text(item.get("tripId"), "trip ID", required=False), item.get("distance"))
        if leg.distance_meters is not None and (type(leg.distance_meters) not in {int, float} or leg.distance_meters < 0):
            raise TravelError("Invalid walking/leg distance")
        if not leg.effective_departure or not leg.effective_arrival:
            raise TravelError("Missing actual and planned leg time")
        if instant(leg.effective_arrival) < instant(leg.effective_departure):
            raise TravelError("Arrival precedes departure")
        legs.append(leg)
    transit = [leg for leg in legs if not leg.walking]
    if not transit:
        raise TravelError("Expected public-transport journey")
    # Multiple legs on a provider's same trip are a continuation, not a transfer.
    boardings = sum(1 for i, leg in enumerate(transit)
                    if i == 0 or leg.trip_id is None or leg.trip_id != transit[i-1].trip_id)
    departure, arrival = legs[0].effective_departure, legs[-1].effective_arrival
    elapsed = (instant(arrival)-instant(departure)).total_seconds()/60
    if elapsed < 0:
        raise TravelError("Invalid full journey chronology")
    cancel = True if any(leg.cancelled is True for leg in legs) else (
        False if all(leg.cancelled is False for leg in transit) else None)
    basis = "provider_effective" if all(leg.departure and leg.arrival for leg in legs) else (
        "planned" if all(not leg.departure and not leg.arrival for leg in legs) else "mixed")
    identity_data = [(leg.origin, leg.destination, leg.planned_departure or leg.departure,
                      leg.planned_arrival or leg.arrival, leg.trip_id, leg.line) for leg in legs]
    identity = hashlib.sha256(json.dumps(identity_data, ensure_ascii=False).encode()).hexdigest()
    at_risk = any(instant(left.effective_arrival) > instant(right.effective_departure)
                  for left, right in zip(legs, legs[1:]))
    return Journey(identity, tuple(legs), departure, arrival, elapsed, max(boardings-1, 0),
                   cancel, remarks(raw.get("remarks")), basis, at_risk)


class TransportRest:
    def __init__(self, read=None):
        self.read = read or CurlReadOnly()

    def stations(self, provider: str, query: str) -> tuple[Station, ...]:
        if provider not in BASES:
            raise TravelError("Unknown provider")
        text(query, "explicit location query")
        raw, _ = self.read(provider, "/locations", {"query": query, "results": 10,
            "addresses": "false", "poi": "false", "language": "de", "pretty": "false"})
        if not isinstance(raw, list):
            raise ProviderUnavailable(provider, "invalid station result")
        result = []
        for item in raw:
            if not isinstance(item, dict) or item.get("type") not in {"stop", "station"}:
                raise ProviderUnavailable(provider, "invalid station candidate")
            result.append(Station(item.get("id"), item.get("name")).checked())
        return tuple(result)  # never silently choose the first/fuzzy candidate

    def journeys(self, query: JourneyQuery) -> JourneyResult:
        query.checked()
        params = {"from": query.origin.id, "to": query.destination.id,
                  query.time_mode: query.window_end if query.window_end and query.time_mode == "arrival" else query.at,
                  "results": query.results_per_page, "remarks": "true", "stopovers": "true",
                  "tickets": "false", "language": "de", "pretty": "false"}
        if query.provider == "db":
            params["routingMode"] = "HYBRID"  # REALTIME pagination is documented incomplete
        if query.max_transfers is not None:
            params["transfers"] = query.max_transfers
        if query.products:
            mapping={p:(p,) for p in ['suburban','subway','bus','tram','ferry','express','regional']}
            if query.provider=='db':
                mapping.update(express=('nationalExpress','national'),regional=('regionalExpress','regional'))
            for product,names in mapping.items():
                for name in names:params[name]='true' if product in query.products else 'false'
        direction = "earlier" if query.time_mode == "arrival" else "later"
        cursor, seen_cursors, hashes, collected = None, set(), [], {}
        updated, reason, coverage = None, None, "incomplete"
        pages = 0
        for _ in range(query.max_pages):
            # Current live VBB endpoint returns 400 "Missing origin" if only
            # laterThan is sent, despite the documentation's wording. Retain
            # station IDs/options on every page and omit only the initial clock.
            request = params if cursor is None else {
                **{key: value for key, value in params.items() if key != query.time_mode},
                f"{direction}Than": cursor}
            try:
                raw, source_hash = self.read(query.provider, "/journeys", request)
            except ProviderUnavailable as exc:
                if pages == 0:
                    raise
                reason = str(exc); break
            if not isinstance(raw, dict) or not isinstance(raw.get("journeys"), list):
                raise ProviderUnavailable(query.provider, "invalid full journey response")
            pages += 1; hashes.append(source_hash)
            timestamp = raw.get("realtimeDataUpdatedAt")
            if timestamp is not None:
                if type(timestamp) not in {int, float} or timestamp < 0:
                    raise ProviderUnavailable(query.provider, "invalid source realtime timestamp")
                try:
                    updated = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                except (OverflowError, OSError, ValueError) as exc:
                    raise ProviderUnavailable(query.provider, "invalid source realtime timestamp") from exc
            try:
                rows = [parse_journey(item) for item in raw["journeys"]]
            except TravelError as exc:
                raise ProviderUnavailable(query.provider, "source schema validation failed") from exc
            for journey in rows:
                if query.contains(journey) and (query.max_transfers is None or journey.transfers <= query.max_transfers):
                    collected[journey.identity] = journey
            next_cursor = raw.get(f"{direction}Ref")
            if next_cursor is not None:
                text(next_cursor, "pagination cursor")
            cursor = next_cursor
            if query.window_end is None:
                coverage = "requested_provider_page"; break
            # Read every item in the crossing page. Never equate page-size with completeness.
            boundary = instant(query.at if direction == "earlier" else query.window_end)
            clocks = [instant(j.arrival if direction == "earlier" else j.departure) for j in rows]
            crossed = clocks and (min(clocks) <= boundary if direction == "earlier" else max(clocks) >= boundary)
            if crossed:
                coverage = "provider_window_boundary_reached"; break
            if not cursor:
                # Missing cursor is not proof that a requested future window was exhausted.
                reason = "Provider did not supply a continuation before the window boundary"; break
            if cursor in seen_cursors:
                reason = "Provider repeated a pagination cursor"; break
            seen_cursors.add(cursor)
        else:
            reason = "Page budget reached before window boundary"
        journeys = tuple(sorted(collected.values(), key=lambda j: instant(j.departure)))
        return JourneyResult(query.provider, "community_transport_rest", "ready" if coverage != "incomplete" else "partial",
                             now(), updated, journeys, pages, coverage, reason, cursor, tuple(hashes))
