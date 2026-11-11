"""Pure source checks shared by appointment reads and frozen hold cleanup.

No auth, model, scheduler or mutation. A Google `confirmed` event alone is not
someone's acceptance. An alternate appointment must be one of the same SENT
offer's actual dated slots, with the exact external parties, not a name match.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from gtm.calendar.confirmation import CODE, PIET, MailEvidence
from gtm.calendar.reply_acceptance import proposal_starts
from gtm.calendar.routing import ACRIBIC

INTERNAL = frozenset((CODE, PIET, 'operator@secondary.example.invalid', ACRIBIC))


def _dt(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('aware_calendar_time_required')
    return stamp


def _interval(event):
    def endpoint(value):
        if 'dateTime' in value:
            return _dt(value['dateTime'])
        return datetime.fromisoformat(value['date']).replace(tzinfo=ZoneInfo('Europe/Berlin'))
    start, end = endpoint(event['start']), endpoint(event['end'])
    if start >= end:
        raise ValueError('valid_calendar_interval_required')
    return start, end


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def external_parties(event):
    parties = {a.get('email', '').casefold() for a in event.get('attendees', [])
               if a.get('email') and not a.get('resource') and a['email'].casefold() not in INTERNAL}
    organizer = event.get('organizer', {}).get('email', '').casefold()
    if event.get('attendees') and organizer and organizer not in INTERNAL:
        parties.add(organizer)
    return frozenset(parties)


def calendar_status(event, account=CODE):
    """Keep positive calendar proof and individual pending/declined states."""
    guests = external_parties(event)
    attendees = event.get('attendees', [])
    organizer = event.get('organizer', {}).get('email', '').casefold()
    ours = [a for a in attendees if a.get('email', '').casefold() == account]
    if event.get('status') == 'cancelled':
        return 'cancelled'
    if event.get('attendeesOmitted') or event.get('status') != 'confirmed':
        return 'unknown'
    if not guests:
        return 'scheduled'
    if any(a.get('responseStatus') == 'declined' for a in ours):
        return 'declined'
    ours_accepted = organizer in (account, ACRIBIC) or any(
        a.get('responseStatus') == 'accepted' for a in ours)
    if organizer not in INTERNAL:
        # The external organizer has actually issued the invitation. A second
        # external attendee may still be needsAction (Lisa + Angelina).
        issuer = [a for a in attendees if a.get('email', '').casefold() == organizer]
        if organizer not in guests or (issuer and not any(
                a.get('responseStatus') == 'accepted' for a in issuer)):
            return 'unknown'
        return 'confirmed' if ours_accepted else 'invited_awaiting_response'
    accepted = any(a.get('email', '').casefold() in guests and
                   a.get('responseStatus') == 'accepted' for a in attendees)
    return 'confirmed' if ours_accepted and accepted else 'invited_awaiting_response'


def source_slots(source, stamp):
    try:
        return tuple(s for s, _ in proposal_starts(
            source, source_time=datetime.fromtimestamp(int(stamp)/1000, timezone.utc)))
    except (TypeError, ValueError):
        return ()


def owner_binding(hold):
    props = hold.get('extendedProperties', {}).get('private', {})
    if props.get('hermesSlotHold') != '1':
        raise ValueError('managed_proposal_required')
    owner = json.loads(props.get('hermesOwner', 'null'))
    if (not isinstance(owner, list) or len(owner) != 3 or owner[0] != 'code'
            or not isinstance(owner[1], str) or not owner[1]
            or not isinstance(owner[2], list) or len(owner[2]) != 1
            or not isinstance(owner[2][0], str) or '@' not in owner[2][0]):
        raise ValueError('exact_managed_owner_required')
    return owner


def proposal_relation(hold, event, sources):
    """Return immutable proof or a concrete preserve reason, without writes.

    `sources` contains complete same-thread MailEvidence plus actual Gmail
    millisecond receipt times. New drafts/replies after the invitation cutoff
    cannot be silently replaced by an older convenient SENT offer.
    """
    try:
        alias, thread, recipients = owner_binding(hold)
        if calendar_status(event) != 'confirmed':
            return {'state': 'preserve', 'reason': 'invitation_not_accepted'}
        if event.get('recurrence') or event.get('recurringEventId'):
            return {'state': 'preserve', 'reason': 'recurring_invitation_requires_review'}
        if hold.get('attendees') or hold.get('attendeesOmitted'):
            return {'state': 'preserve', 'reason': 'proposal_has_attendees'}
        party = recipients[0].casefold()
        parties = external_parties(event)
        if party not in parties:
            return {'state': 'preserve', 'reason': 'different_parties'}
        start, _ = _interval(hold)
        chosen, _ = _interval(event)
        cutoff = _dt(event['created'])
        candidates = []
        for source, stamp in sources:
            if (not isinstance(source, MailEvidence) or not source.complete
                    or source.account_email != CODE or source.thread_id != thread):
                raise ValueError('complete_same_thread_sources_required')
            receipt = datetime.fromtimestamp(int(stamp)/1000, timezone.utc)
            if receipt > cutoff:
                if source.sender.casefold() in parties and not set(source.labels) & {'SENT', 'DRAFT'}:
                    return {'state': 'preserve', 'reason': 'newer_counterparty_reply'}
                if source.sender == CODE and start in source_slots(source, stamp):
                    return {'state': 'preserve', 'reason': 'newer_offer_or_draft'}
            if source.sender == CODE and start in source_slots(source, stamp):
                candidates.append((source, stamp, receipt))
        if not candidates:
            return {'state': 'preserve', 'reason': 'sent_slot_not_proven'}
        candidates.sort(key=lambda row: int(row[1]), reverse=True)
        source, stamp, receipt = candidates[0]
        if len(candidates) > 1 and int(candidates[1][1]) == int(stamp):
            raise ValueError('ambiguous_latest_offer')
        if ('SENT' not in source.labels or 'DRAFT' in source.labels
                or not source.rfc_message_id or receipt >= cutoff):
            return {'state': 'preserve', 'reason': 'latest_offer_not_sent_before_invitation'}
        if set(r.casefold() for r in source.recipients) - INTERNAL != parties:
            return {'state': 'preserve', 'reason': 'exact_offer_parties_differ'}
        slots = source_slots(source, stamp)
        if chosen not in slots:
            return {'state': 'preserve', 'reason': 'invitation_not_from_same_offer'}
        return {'state': 'selected_proposal' if start == chosen else 'obsolete_proposal',
                'reason': 'same_sent_offer_exact_parties_and_accepted_calendar',
                'account': alias, 'thread_id': thread, 'recipient': party,
                'proposal_message_id': source.message_id,
                'proposal_source_hash': hashlib.sha256(source.source_text().encode()).hexdigest(),
                'proposal_receipt': receipt.isoformat(),
                'meeting_start': chosen.astimezone(timezone.utc).isoformat(),
                'hold_start': start.astimezone(timezone.utc).isoformat(),
                'external_parties': sorted(parties), 'cutoff': cutoff.isoformat()}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {'state': 'preserve', 'reason': 'source_binding_requires_review'}


def _ical_endpoint(value, params):
    if value.endswith('Z'):
        return datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    match = re.search(r'(?:^|;)TZID=([^;]+)', params)
    if not match:
        raise ValueError('calendar_request_timezone_required')
    name = match[1].strip('"')
    name = {'W. Europe Standard Time': 'Europe/Berlin'}.get(name, name)
    stamp = datetime.strptime(value, '%Y%m%dT%H%M%S').replace(tzinfo=ZoneInfo(name))
    if (stamp.utcoffset() != stamp.replace(fold=1).utcoffset() or
            stamp.astimezone(timezone.utc).astimezone(stamp.tzinfo).replace(tzinfo=None)
            != stamp.replace(tzinfo=None)):
        raise ValueError('calendar_request_ambiguous_wall_time')
    return stamp


def calendar_request(source, event, *, require_accepted=True):
    """Bind an external raw MIME REQUEST to the actual accepted Google event.

    RFC UID, organizer, all external participants, selected start/end and
    sequence agree. A forwarded/SENT invitation, cancellation or quote never
    grants cleanup authority. Multiple identical MIME alternatives are allowed.
    """
    if (source.account_email != CODE or not source.complete or
            set(source.labels) & {'SENT', 'DRAFT'} or CODE not in source.recipients
            or (calendar_status(event) != 'confirmed' if require_accepted else
                calendar_status(event) not in ('confirmed', 'invited_awaiting_response', 'declined')) or not event.get('etag')
            or not event.get('iCalUID') or event.get('recurrence') or event.get('recurringEventId')
            or source.sender.casefold() != event.get('organizer', {}).get('email', '').casefold()
            or source.sender.casefold() in INTERNAL):
        raise ValueError('exact_incoming_calendar_request_required')
    raw = base64.b64decode(source.raw_b64, validate=True)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if message.defects:
        raise ValueError('calendar_request_mime_defect')
    found = []
    for part in message.walk():
        if part.get_content_type() != 'text/calendar' and not (part.get_filename() or '').lower().endswith('.ics'):
            continue
        text = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8')
        lines = re.sub(r'\r?\n[ \t]', '', text).splitlines()
        methods = [line[7:] for line in lines if line.startswith('METHOD:')]
        if methods != ['REQUEST']:
            raise ValueError('calendar_request_method')
        inside = False; fields = {}; count = 0
        for line in lines:
            if line == 'BEGIN:VEVENT':
                inside = True; count += 1
            elif line == 'END:VEVENT':
                inside = False
            elif inside and ':' in line:
                key, value = line.split(':', 1); name, _, params = key.partition(';')
                fields.setdefault(name, []).append((params, value))
        if count != 1 or any(k in fields for k in ('RRULE', 'RECURRENCE-ID', 'EXDATE', 'RDATE')):
            raise ValueError('single_calendar_request_required')
        def one(name):
            values = fields.get(name, [])
            if len(values) != 1:
                raise ValueError('calendar_request_field_missing_or_duplicate')
            return values[0]
        if one('UID')[1] != event['iCalUID'] or one('STATUS')[1] != 'CONFIRMED':
            raise ValueError('calendar_request_uid_or_status')
        if int(one('SEQUENCE')[1]) != event.get('sequence', 0):
            raise ValueError('calendar_request_sequence_changed')
        if one('ORGANIZER')[1].casefold() != 'mailto:' + source.sender.casefold():
            raise ValueError('calendar_request_organizer')
        endpoints = tuple(_ical_endpoint(one(k)[1], one(k)[0]) for k in ('DTSTART', 'DTEND'))
        if endpoints != _interval(event):
            raise ValueError('calendar_request_interval_changed')
        attendees = []
        for _, value in fields.get('ATTENDEE', []):
            if not value.casefold().startswith('mailto:'):
                raise ValueError('calendar_request_attendee')
            attendees.append(value[7:].casefold())
        if CODE not in attendees or set(attendees) - INTERNAL | {source.sender.casefold()} != external_parties(event):
            raise ValueError('calendar_request_party_mismatch')
        found.append({'message_id': source.message_id, 'thread_id': source.thread_id,
                      'source_hash': hashlib.sha256(source.source_text().encode()).hexdigest(),
                      'uid': event['iCalUID'], 'sequence': event.get('sequence', 0)})
    if not found or any(row != found[0] for row in found):
        raise ValueError('calendar_request_not_unique')
    return found[0]


def own_calendar_reply_headers(rows):
    """Google's technical Sender is allowed only for our own accepted RSVP.

    The general business-mail authority rule is unchanged. Exact raw headers
    are retained; only this dedicated calendar transport validation permits the
    documented Google Sender to differ from the CODE From/Reply-To identity.
    Callers must separately prove actual CODE profile + SENT without DRAFT.
    """
    from gtm.mail_observer.sources import checked_headers
    senders = [value for name, value in rows if name.casefold() == 'sender']
    if len(senders) == 1 and parseaddr(senders[0])[1].casefold() == 'calendar-notification@google.com':
        from email.headerregistry import HeaderRegistry
        sender = HeaderRegistry()('sender', senders[0])
        if sender.defects or len(sender.addresses) != 1:
            raise ValueError('calendar_reply_transport_sender_invalid')
        headers = checked_headers([(name, value) for name, value in rows if name.casefold() != 'sender'])
        headers['sender'] = senders[0] # Keep the actual technical sender visible.
    else:
        headers = checked_headers(rows)
    if (parseaddr(headers['from'])[1].casefold() != CODE or
            not re.match(r'^(?:Accepted|Angenommen):\s', headers.get('subject', ''), re.I)):
        raise ValueError('calendar_reply_owned_acceptance_headers_required')
    return headers


def calendar_reply(source, event):
    """An own SENT RSVP is a wakeup, never the invitation/delete authority."""
    if (source.account_email != CODE or source.sender != CODE or not source.rfc_message_id
            or 'SENT' not in source.labels or 'DRAFT' in source.labels
            or event.get('organizer', {}).get('email', '').casefold() not in source.recipients
            or calendar_status(event) != 'confirmed'):
        raise ValueError('actual_own_sent_calendar_acceptance_required')
    raw = base64.b64decode(source.raw_b64, validate=True)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if message.defects:
        raise ValueError('calendar_reply_mime_defect')
    headers = own_calendar_reply_headers([(name, str(value)) for name, value in message.items()])
    if headers['message-id'].strip() != source.rfc_message_id:
        raise ValueError('calendar_reply_raw_identity_changed')
    proofs = []
    for part in message.walk():
        if part.get_content_type() != 'text/calendar' and not (part.get_filename() or '').lower().endswith('.ics'):
            continue
        text = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8')
        lines = re.sub(r'\r?\n[ \t]', '', text).splitlines()
        if [line for line in lines if line.startswith('METHOD:')] != ['METHOD:REPLY']:
            raise ValueError('calendar_reply_method')
        fields = {}; inside = False; count = 0
        for line in lines:
            if line == 'BEGIN:VEVENT': inside = True; count += 1
            elif line == 'END:VEVENT': inside = False
            elif inside and ':' in line:
                key, value = line.split(':', 1); name, _, params = key.partition(';')
                fields.setdefault(name, []).append((params, value))
        def one(name):
            values = fields.get(name, [])
            if len(values) != 1: raise ValueError('calendar_reply_field_missing_or_duplicate')
            return values[0]
        if (count != 1 or any(k in fields for k in ('RRULE', 'RECURRENCE-ID'))
                or one('UID')[1] != event.get('iCalUID') or one('STATUS')[1] != 'CONFIRMED'
                or int(one('SEQUENCE')[1]) != event.get('sequence', 0)
                or one('ORGANIZER')[1].casefold() != 'mailto:' + event['organizer']['email'].casefold()
                or tuple(_ical_endpoint(one(k)[1], one(k)[0]) for k in ('DTSTART', 'DTEND')) != _interval(event)):
            raise ValueError('calendar_reply_actual_event_changed')
        own = [(params, value) for params, value in fields.get('ATTENDEE', []) if value.casefold() == 'mailto:' + CODE]
        if len(own) != 1 or not re.search(r'(?:^|;)PARTSTAT=ACCEPTED(?:;|$)', own[0][0], re.I):
            raise ValueError('calendar_reply_own_acceptance_required')
        proofs.append((event['iCalUID'], event.get('sequence', 0)))
    if not proofs or any(p != proofs[0] for p in proofs):
        raise ValueError('calendar_reply_not_unique')
    return {'message_id': source.message_id, 'source_hash': hashlib.sha256(source.source_text().encode()).hexdigest()}
