"""Zero-model call timing and the actual Acribic Outreach briefing contract.

Only explicitly matched business calls enter this planner. Calendar text does
not authorize source collection, recipients, CRM changes or external sending.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json


@dataclass(frozen=True)
class BriefingDue:
    identity: str
    category: str
    event_id: str
    starts_at: datetime
    due_at: datetime
    late_seconds: int
    calendar_id: str = 'operator@primary.example.invalid'


def timestamp(raw):
    value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Calendar time must have an explicit offset')
    return value.astimezone(timezone.utc)


def due_briefing(event, *, calendar_id, category, now, max_delay_seconds=None):
    """None is not due/out of scope; missed deadlines remain explicit results.

    category must come from a trusted project/CRM match, never a guess from a
    person's name. Identity depends on the real start, not RSVP/etag changes.
    """
    if category not in ('acribic', 'stealth'):
        return None
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Explicit clock timezone required')
    if event.get('status') == 'cancelled' or 'dateTime' not in event.get('start', {}):
        return None
    if any(item.get('self') and item.get('responseStatus') == 'declined' for item in event.get('attendees', [])):
        return None
    if not event.get('id') or not calendar_id:
        raise ValueError('Real calendar and event identity required')
    starts = timestamp(event['start']['dateTime']); clock = now.astimezone(timezone.utc)
    due = starts - timedelta(minutes=15)
    if clock < due or clock >= starts:
        return None
    delay = int((clock - due).total_seconds())
    key = hashlib.sha256(json.dumps([calendar_id, event['id'], starts.isoformat()], separators=(',', ':')).encode()).hexdigest()
    if max_delay_seconds is not None and delay > max_delay_seconds:
        return {'state': 'missed_briefing_deadline', 'identity': key, 'category': category,
                'late_seconds': delay, 'auto_delivery': False}
    return BriefingDue(key, category, event['id'], starts, due, delay, calendar_id)


def render_briefing(*, time_label, person, role, company, company_context,
                    interest, questions, addressing, room=None, caveat=None, opener=None,
                    conversation_context=None, goals=None, interest_label='Konkretes Interesse'):
    """Render already evidenced content; no second analysis/model invocation.

    This keeps the matched Netlution example's structure and Alex's corrections:
    person/company, concrete interest, useful questions, Du/Sie and room once.
    """
    topic = interest or conversation_context
    values = (time_label, person, company_context, topic, addressing)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError('Briefing fields require actual evidence, no placeholders')
    if addressing not in ('Du', 'Sie', 'unklar'):
        raise ValueError('Last actually used form of address required')
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3 or any(not isinstance(q, str) or not q.strip() for q in questions):
        raise ValueError('One to three concrete questions required')
    person_label = person + (', ' + role if role else '')
    company_label = f' bei {company}' if company else ''
    blocks = [f'**{time_label} - {person_label}{company_label}.** {company_context.strip()}',
              f'**{interest_label if interest else "Gesprächsfokus"}:** {topic.strip()}',
              '**Fragen:** ' + ' '.join(questions),
              f'**Anrede: {addressing}.' + (f' Raum: {room}.' if room else '') + '**']
    if goals:
        blocks.insert(2, '**Gesprächsziel:** ' + ' '.join(goals))
    if opener:
        if not isinstance(opener, str) or len(opener) > 180 or '\n' in opener:
            raise ValueError('One short evidenced opener required')
        blocks.insert(1, '**Einstieg:** ' + opener)
    if caveat:
        blocks.append(caveat.strip())
    blocks.append('Sag mir Bescheid, wenn der Call durch ist.')
    output = '\n\n'.join(blocks)
    if len(output) > 3500:
        raise ValueError('Briefing too long; shorten deliberately, never truncate evidence')
    return output
