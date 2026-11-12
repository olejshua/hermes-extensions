"""Short natural output with acquisition time and honest live-data limits."""
from zoneinfo import ZoneInfo

from .contracts import instant


def connection_brief(result, *, limit=3):
    if result.status == "blocked":
        return "Die Verbindung konnte noch nicht live geprüft werden. Die offizielle Webauskunft muss gelesen werden."
    candidates=[journey for journey in result.journeys if journey.cancelled is not True and not journey.connection_at_risk]
    lines=[]
    for journey in candidates[:limit]:
        departure=instant(journey.departure).astimezone(ZoneInfo("Europe/Berlin")).strftime("%H:%M")
        arrival=instant(journey.arrival).astimezone(ZoneInfo("Europe/Berlin")).strftime("%H:%M")
        vehicles=" → ".join(leg.line for leg in journey.legs if not leg.walking)
        changes="direkt" if journey.transfers==0 else f"{journey.transfers} Umstieg"+("e" if journey.transfers!=1 else "")
        delay=next(leg.departure_delay_seconds for leg in journey.legs if not leg.walking)
        note="Verspätung unbekannt" if delay is None else "keine Abfahrtsverspätung gemeldet" if delay==0 else (
            f"Abfahrt {delay/60:g} Min. verspätet" if delay>0 else f"Abfahrt {-delay/60:g} Min. früher")
        lines.append(f"{departure}–{arrival} · {vehicles} · {journey.duration_minutes:g} Min. · {changes} · {note}")
    if not lines:
        lines.append("Im geprüften Ergebnis liegt keine passende Fahrt ohne gemeldeten Ausfall oder erkennbar verpassten Anschluss vor.")
    cancelled=sum(journey.cancelled is True for journey in result.journeys)
    if cancelled:
        lines.append(f"{cancelled} gemeldete Ausfälle im Ergebnis.")
    if result.status=="partial":
        lines.append("Der gewünschte Zeitraum ist erst teilweise geprüft.")
    source="Community-Auskunft transport.rest" if result.source_kind=="community_transport_rest" else "offizielle Webauskunft"
    at=instant(result.fetched_at).astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
    lines.append(f"Abgerufen {at} (Berlin), {source}.")
    return "\n".join(lines)
