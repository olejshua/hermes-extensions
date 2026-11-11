"""Pure exact-slot evidence boundary; no calendar, Gmail, or CRM writes.

Adapters must supply a fresh response for every required calendar. Missing or malformed
responses are UNKNOWN, never FREE. Shortlists cannot be supplied as evidence.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Mapping


class Status(str, Enum):
    FREE = 'free'
    BUSY = 'busy'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class Assessment:
    status: Status
    reason: str
    calendar_ids: tuple[str, ...]


def _aware(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError('timezone required')
    return dt


def assess_exact_slot(start, response: Mapping, required_ids, *, checked_at,
                      now, max_age=timedelta(seconds=60), pending=()):
    """Assess start-15m through start+30m, matching the existing hold boundary.

    The returned status alone never authorizes a draft: the existing managed
    scheduling transaction must reserve/read back the private 30-minute hold.
    `pending` includes every durable claim; own-hold exclusion requires a
    separate exact-owner readback and must occur in the production adapter.
    """
    ids = tuple(required_ids)
    try:
        dt, checked, current = map(_aware, (start, checked_at, now))
        if len(ids) < 2 or len(set(ids)) != len(ids) or not all(isinstance(i, str) and i for i in ids):
            raise ValueError('distinct required calendar IDs required')
        if not timedelta(0) <= current - checked <= max_age:
            raise ValueError('stale or future evidence')
        lower, upper = dt-timedelta(minutes=15), dt+timedelta(minutes=30)
        # Require coverage, preventing a day-shortlist or narrower query from
        # being misrepresented as an exact live availability check.
        if _aware(response['timeMin']) > lower or _aware(response['timeMax']) < upper:
            raise ValueError('incomplete query coverage')
        calendars = response['calendars']
        intervals = []
        for cid in ids:
            entry = calendars[cid]
            if entry.get('errors') or not isinstance(entry.get('busy'), list):
                raise ValueError('missing busy evidence or calendar error')
            for interval in entry['busy']:
                a, b = _aware(interval['start']), _aware(interval['end'])
                if a >= b:
                    raise ValueError('invalid busy interval')
                intervals.append((a, b))
        for a, b in pending:
            a, b = _aware(a), _aware(b)
            if a >= b:
                raise ValueError('invalid pending interval')
            intervals.append((a, b))
        if any(lower < b and upper > a for a, b in intervals):
            return Assessment(Status.BUSY, 'live_overlap_or_pending_claim', ids)
        return Assessment(Status.FREE, 'complete_exact_live_check', ids)
    except (KeyError, TypeError, ValueError, AttributeError):
        return Assessment(Status.UNKNOWN, 'incomplete_invalid_or_stale_evidence', ids)
