"""Strict request/result contracts; unknown live fields stay unknown."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


class TravelError(ValueError):
    pass


class ProviderUnavailable(TravelError):
    def __init__(self, provider: str, reason: str, status: int | None = None):
        self.provider, self.reason, self.status = provider, reason, status
        super().__init__(f"{provider}: {reason}" + (f" (HTTP {status})" if status else ""))


def instant(value: str) -> datetime:
    if not isinstance(value, str):
        raise TravelError("Timestamp must be an ISO-8601 string with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TravelError("Invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise TravelError("Timezone required; do not guess date/time or timezone")
    return parsed


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def text(value: Any, name: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 100_000:
        raise TravelError(f"Invalid {name}; source was not silently truncated")
    return value


@dataclass(frozen=True)
class Station:
    id: str
    name: str

    def checked(self) -> Station:
        text(self.id, "station ID"); text(self.name, "station name")
        if not self.id.isdigit() or len(self.id) > 16:
            raise TravelError("Select a provider station ID from a current station search")
        return self


@dataclass(frozen=True)
class JourneyQuery:
    provider: str
    origin: Station
    destination: Station
    at: str
    time_mode: str = "departure"
    window_end: str | None = None
    max_transfers: int | None = None
    results_per_page: int = 6
    max_pages: int = 8
    products: tuple[str, ...] = ()

    def checked(self) -> JourneyQuery:
        if self.provider not in {"vbb", "db"}:
            raise TravelError("Provider must be vbb or db")
        self.origin.checked(); self.destination.checked()
        if self.origin.id == self.destination.id:
            raise TravelError("Origin and destination must differ")
        if self.time_mode not in {"departure", "arrival"}:
            raise TravelError("Specify departure or arrival")
        start = instant(self.at)
        if self.window_end is not None:
            end = instant(self.window_end)
            if not start < end or (end-start).total_seconds() > 86400:
                raise TravelError("Explicit window must be positive and at most 24 hours")
        for value, low, high, name in [(self.results_per_page, 1, 10, "page size"),
                                       (self.max_pages, 1, 20, "page budget")]:
            if type(value) is not int or not low <= value <= high:
                raise TravelError(f"Invalid {name}")
        if self.max_transfers is not None and (type(self.max_transfers) is not int or not 0 <= self.max_transfers <= 10):
            raise TravelError("Invalid transfer count")
        allowed={'suburban','subway','bus','tram','ferry','express','regional'}
        if not isinstance(self.products,tuple) or len(set(self.products))!=len(self.products) or any(p not in allowed for p in self.products):
            raise TravelError("Invalid explicit transport products")
        return self

    def contains(self, journey: Journey) -> bool:
        product_aliases={'nationalExpress':'express','national':'express','regionalExpress':'regional'}
        if self.products and any(product_aliases.get(leg.product,leg.product) not in self.products for leg in journey.legs if not leg.walking):
            return False
        value = journey.departure if self.time_mode == "departure" else journey.arrival
        at = instant(value)
        if self.window_end is None and self.time_mode == "arrival":
            return at <= instant(self.at)
        return at >= instant(self.at) and (self.window_end is None or at < instant(self.window_end))


@dataclass(frozen=True)
class Leg:
    origin: str
    destination: str
    departure: str | None
    planned_departure: str | None
    arrival: str | None
    planned_arrival: str | None
    departure_delay_seconds: int | None
    arrival_delay_seconds: int | None
    line: str | None
    product: str | None
    walking: bool
    cancelled: bool | None
    departure_platform: str | None
    arrival_platform: str | None
    remarks: tuple[dict, ...]
    trip_id: str | None = None
    distance_meters: float | None = None

    @property
    def effective_departure(self) -> str:
        return self.departure or self.planned_departure  # parser requires at least one

    @property
    def effective_arrival(self) -> str:
        return self.arrival or self.planned_arrival


@dataclass(frozen=True)
class Journey:
    identity: str
    legs: tuple[Leg, ...]
    departure: str
    arrival: str
    duration_minutes: float
    transfers: int
    cancelled: bool | None
    remarks: tuple[dict, ...]
    time_basis: str
    connection_at_risk: bool = False

    def fields(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class JourneyResult:
    provider: str
    source_kind: str
    status: str
    fetched_at: str
    realtime_updated_at: str | None
    journeys: tuple[Journey, ...]
    pages_read: int
    coverage: str
    reason: str | None = None
    next_cursor: str | None = None
    source_hashes: tuple[str, ...] = ()
    fallback_reason: str | None = None
    source_time_labels: tuple[str | None, ...] = ()

    def fields(self) -> dict:
        return asdict(self)
