"""Zero-model planner for proactive preparation before important meetings.

Unlike briefing.py (a short call briefing shortly before an explicitly matched
Acribic/Stealth call), this module finds events that deserve preparation hours
ahead, such as a fundraising session or any meeting with outside attendees.

The result only ever goes to the owner's private channel. It never contacts an
attendee, edits the event, sends mail or touches the CRM. Calendar text selects
candidates but never supplies instructions or recipients.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
import re

# Titles that are holds, reminders or routine noise, never worth a prep note.
ROUTINE = re.compile(
    r'\b(luma|blocker|vorschlag|hold|fokus|focus|busy|besetzt|mittag|lunch|reminder|'
    r'erinnerung|pause|feierabend|anrufen)\b', re.I)
# Generic importance cues; deployment-specific names come in through `important`.
IMPORTANT = re.compile(
    r'\b(fundrais|investor|investment|pitch|pre-?seed|seed|due diligence|term ?sheet|'
    r'interview|vorstellungsgespr|board|kunde|customer|demo|workshop|session)\w*', re.I)
QUIET_START, QUIET_END = time(21, 30), time(8, 0)
MIN_MINUTES, MAX_MINUTES = 20, 8 * 60


@dataclass(frozen=True)
class PrepConfig:
    """Preparation policy. Every value is a design default until `owner_set`
    names it; `owner_set` records which fields Ole actually chose.

    Outside-attendee events do not qualify by default, because most are not
    worth a note. Events Ole names (terms or event ids) always qualify and
    bypass the duration and routine-title filters.
    """
    lead_hours: int = 18
    quiet_start: time = QUIET_START
    quiet_end: time = QUIET_END
    evening_slot: time = time(20, 0)
    min_minutes: int = MIN_MINUTES
    max_minutes: int = MAX_MINUTES
    outside_attendees_qualify: bool = False
    important_terms: tuple = ()
    important_event_ids: tuple = ()
    owner_set: frozenset = frozenset()

    def source(self, name):
        return 'owner' if name in self.owner_set else 'default'


@dataclass(frozen=True)
class PrepDue:
    identity: str
    event_id: str
    calendar_id: str
    starts_at: datetime
    due_at: datetime
    late_seconds: int
    reason: str
    outside_attendees: tuple


def _stamp(raw):
    value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Calendar time must have an explicit offset')
    return value.astimezone(timezone.utc)


def outside_attendees(event, own_addresses):
    own = {a.casefold() for a in own_addresses}
    return tuple(sorted({
        a.get('email', '').casefold() for a in event.get('attendees', [])
        if a.get('email') and not a.get('self') and not a.get('resource')
        and a.get('responseStatus') != 'declined' and a['email'].casefold() not in own}))


def qualifies(event, own_addresses, config=None):
    """Return the reason this event needs preparation, or None.

    Owner-named events win over the generic filters; the generic filters
    (duration, routine titles, outside attendees) only shape the defaults.
    """
    config = config or PrepConfig()
    if event.get('status') == 'cancelled' or 'dateTime' not in event.get('start', {}) \
            or 'dateTime' not in event.get('end', {}):
        return None
    if any(a.get('self') and a.get('responseStatus') == 'declined' for a in event.get('attendees', [])):
        return None
    title = str(event.get('summary') or '')
    if event.get('id') in config.important_event_ids:
        return 'owner_named_event'
    if any(str(t).strip() and str(t).casefold() in title.casefold() for t in config.important_terms):
        return 'owner_named_topic'
    minutes = (_stamp(event['end']['dateTime']) - _stamp(event['start']['dateTime'])).total_seconds() / 60
    if not config.min_minutes <= minutes <= config.max_minutes or ROUTINE.search(title):
        return None
    if IMPORTANT.search(title):
        return 'important_title'
    if config.outside_attendees_qualify and outside_attendees(event, own_addresses):
        return 'outside_attendees'
    return None


def _delivery_time(due, tz, config):
    """Move a due time out of quiet hours: forward to the morning, never into the night."""
    local = due.astimezone(tz)
    if config.quiet_end <= local.time() < config.quiet_start:
        return due
    day = local.date() if local.time() < config.quiet_end else local.date() + timedelta(days=1)
    return datetime.combine(day, config.quiet_end, tz).astimezone(timezone.utc)


def due_preparation(event, *, calendar_id, own_addresses, now, config=None, max_delay_seconds=None,
                    tz='Europe/Berlin', min_lead_minutes=30):
    """PrepDue when the preparation window is open, a missed-state dict when
    delivery is too late to be useful, otherwise None.

    Identity is bound to calendar, event and real start, so RSVP or etag changes
    never repeat a note while a reschedule gets a fresh one.
    """
    config = config or PrepConfig()
    if not isinstance(config.lead_hours, int) or isinstance(config.lead_hours, bool) or not 2 <= config.lead_hours <= 72:
        raise ValueError('prep_lead_out_of_range')
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Explicit clock timezone required')
    reason = qualifies(event, own_addresses, config)
    if reason is None:
        return None
    if not event.get('id') or not calendar_id:
        raise ValueError('Real calendar and event identity required')
    zone = ZoneInfo(tz)
    starts = _stamp(event['start']['dateTime']); clock = now.astimezone(timezone.utc)
    latest = starts - timedelta(minutes=min_lead_minutes)
    due = _delivery_time(starts - timedelta(hours=config.lead_hours), zone, config)
    if due >= latest:  # window too tight after quiet hours: latest evening slot before the start
        local = latest.astimezone(zone)
        day = local.date() if local.time() >= config.evening_slot else local.date() - timedelta(days=1)
        due = datetime.combine(day, config.evening_slot, zone).astimezone(timezone.utc)
    if clock < due or clock >= latest:
        return None
    if not config.quiet_end <= clock.astimezone(zone).time() < config.quiet_start:
        return None  # catch-up after a restart waits for the morning, never pings at night
    delay = int((clock - due).total_seconds())
    key = hashlib.sha256(json.dumps([calendar_id, event['id'], starts.isoformat(), 'prep'],
                                    separators=(',', ':')).encode()).hexdigest()
    if max_delay_seconds is not None and delay > max_delay_seconds:
        return {'state': 'missed_prep_window', 'identity': key, 'late_seconds': delay, 'auto_delivery': False}
    return PrepDue(key, event['id'], calendar_id, starts, due, delay, reason,
                   outside_attendees(event, own_addresses))


def render_prep(*, time_label, title, people, known, open_questions, prepared=None, gaps=None):
    """Owner-facing preparation note from already evidenced content.

    known: facts with a source in mail, notes or CRM. open_questions: what the
    owner still has to decide or answer. gaps: what could not be found.
    """
    def items(values, low, high, name):
        if not isinstance(values, list) or not low <= len(values) <= high or \
                any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(f'{name} requires {low} to {high} evidenced entries')
        return [v.strip() for v in values]
    for value in (time_label, title):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Preparation requires an actual time and title')
    known = items(known, 1, 5, 'known')
    questions = items(open_questions, 1, 5, 'open_questions')
    names = ', '.join(p.strip() for p in people if isinstance(p, str) and p.strip())
    blocks = [f'**{time_label} - {title.strip()}**' + (f' mit {names}.' if names else '.'),
              '**Stand:** ' + ' '.join(known)]
    if prepared:
        blocks.append('**Schon vorbereitet:** ' + ' '.join(items(prepared, 1, 3, 'prepared')))
    blocks.append('**Offene Fragen an dich:** ' + ' '.join(f'{i}) {q}' for i, q in enumerate(questions, 1)))
    if gaps:
        blocks.append('**Nicht gefunden:** ' + ' '.join(items(gaps, 1, 3, 'gaps')))
    blocks.append('Nichts davon ist nach außen gegangen. Antworte mit deinen Punkten, dann bereite ich den Rest vor.')
    output = '\n\n'.join(blocks)
    if len(output) > 3500:
        raise ValueError('Preparation too long; shorten deliberately, never truncate evidence')
    return output
