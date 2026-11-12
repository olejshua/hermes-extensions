"""Native-tool boundary: deterministic API reads with a real official-web fallback.

The caller supplies its existing background browser worker. No worker means a
concrete blocked state, never fabricated journey data or a link-only success.
"""
from dataclasses import replace

from .contracts import JourneyQuery, JourneyResult, ProviderUnavailable, now
from .transport import TransportRest
from .web import compare_fares, plan, validate_observation


class TravelService:
    def __init__(self, transport=None, official_reader=None, fare_reader=None):
        self.transport = transport or TransportRest()
        self.official_reader = official_reader
        self.fare_reader = fare_reader

    def journeys(self, query: JourneyQuery, *, prefer_official: bool = False) -> JourneyResult:
        query.checked()
        partial = None
        reason = "Official web requested" if prefer_official else None
        if not prefer_official:
            try:
                result = self.transport.journeys(query)
                partial = result if result.status=='partial' else None
                if result.status == "ready" or self.official_reader is None:
                    return result
                reason = result.reason or "Community result is incomplete"
            except ProviderUnavailable as exc:
                reason = str(exc)
        if self.official_reader is None:
            return JourneyResult(query.provider, "unavailable", "blocked", now(), None, (), 0,
                                 "incomplete", "Existing background browser worker required for official live lookup",
                                 fallback_reason=reason)
        request = plan(query)
        try:
            observed = self.official_reader(request)  # actual read callback, not a search link
        except Exception as exc:
            if getattr(exc,'reason',None)=='travel_cancelled':raise
            if partial is not None:
                return replace(partial,fallback_reason=getattr(exc,'reason','official_reader_unavailable'))
            if hasattr(exc,'reason'):exc.fallback_reason=reason
            raise
        result = validate_observation(request, observed)
        return replace(result, fallback_reason=reason)

    def fares(self, query, profile):
        query.checked(); profile.checked()
        if query.provider != "db" or self.fare_reader is None:
            raise ProviderUnavailable("db", "Existing official background fare reader required")
        observations = tuple(self.fare_reader(plan(query, operation="fares"), profile))
        for observation in observations:
            if observation.query != query or observation.profile != profile:
                raise ProviderUnavailable("db", "Fare result belongs to different travel conditions")
        return compare_fares(observations)
