"""Real-source adapters for prep_daemon, reusing the installed helpers.

Reads only. The mail collector searches only the attendee addresses of the
event in the owner's own mailbox and only when 'mail' is an allowed source.
The sender reuses the existing private-chat CLI path and receipt format.
"""
from __future__ import annotations
import datetime as dt
import json
import re
import subprocess
from gtm.briefing_daemon import (CLI, CODE, DESTINATION, ExistingReads, ReviewRequired,
                                 verify_private_destination)
from gtm.calendar.routing import event_calendar
from gtm.prep_daemon import PrepStore

_ADDRESS = re.compile(r'^[A-Za-z0-9._%+][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')
_CONTROL = re.compile(r'[\x00-\x1f\x7f]+')
HORIZON_HOURS = 74


class CalendarReads:
    def __init__(self, reads=None):
        self.reads = reads or ExistingReads()

    def events(self, now):
        svc = self.reads.calendar()
        return self.reads._events(svc, now, now + dt.timedelta(hours=HORIZON_HOURS))

    def calendar_id(self, event):
        return event_calendar(event)

    def current_event(self, event_id):
        return self.reads.current_event(event_id)


class MailEvidence:
    """Subject and snippet of recent threads with the event's outside attendees."""

    def __init__(self, reads=None, per_address=3, own=(CODE,)):
        self.reads, self.per_address, self.own = reads or ExistingReads(), per_address, {a.casefold() for a in own}

    def collect(self, event, allowed):
        if 'mail' not in {a.casefold() for a in allowed}:
            return []
        svc = self.reads.mail_module.service('code')
        getter = getattr(self.reads.mail_module, 'verified_profile', None)
        actual = getter(svc) if callable(getter) else None
        if actual is None:
            actual = svc.users().getProfile(userId='me').execute().get('emailAddress', '')
        if not isinstance(actual, str) or actual.casefold() != CODE.casefold():
            raise ReviewRequired('mailbox_identity_mismatch')
        addresses = sorted({a['email'].casefold() for a in event.get('attendees', [])
                            if a.get('email') and not a.get('self') and not a.get('resource')
                            and a['email'].casefold() not in self.own and _ADDRESS.fullmatch(a['email'])})
        items = []
        for address in addresses[:6]:
            listing = svc.users().messages().list(userId='me', q=f'(from:{address} OR to:{address}) newer_than:90d',
                                                  maxResults=self.per_address).execute()
            for ref in listing.get('messages', []):
                msg = svc.users().messages().get(userId='me', id=ref['id'], format='metadata',
                                                 metadataHeaders=['Subject', 'Date']).execute()
                headers = {h['name'].lower(): h['value'] for h in msg.get('payload', {}).get('headers', [])}
                # snippet is a Message field the metadata format documents loosely: treat it as optional.
                text = _CONTROL.sub(' ', f"{headers.get('date', '')[:16]} {headers.get('subject', '')}: {msg.get('snippet') or ''}").strip()
                items.append(dict(source='mail', ref='mail:' + msg['id'], text=text))
        return items


def evidence_drafter(event, evidence):
    """Deterministic, model-free fallback: cites each thing it states.

    A model-backed drafter can replace this through the same signature; its
    output goes through the same citation check.
    """
    known = [dict(text=e['text'], ref=e['ref']) for e in evidence[:3]]
    questions = [dict(text='Was soll aus diesem Verlauf im Termin geklärt sein? ' + e['text'].split(':')[0][:80], ref=e['ref'])
                 for e in evidence[:2]]
    return dict(known=known, open_questions=questions)


class PrivateSender:
    """Existing CLI send to the verified private chat; uncertain results stay uncertain."""

    def __init__(self, store_root, target=DESTINATION, run=subprocess.run):
        if target != DESTINATION:
            raise ReviewRequired('private_destination_mismatch')
        self.root, self.target, self.run = store_root, target, run

    def send(self, identity, message):
        result = self.run([str(CLI), 'send', '--to', self.target, '--file', '-', '--json'],
                          input=message, text=True, capture_output=True, timeout=25)
        if result.returncode:
            return None  # unknown: the CLI may have sent before failing
        try:
            parsed = json.loads(result.stdout)
        except ValueError:
            return None
        if parsed.get('skipped'):
            return {'success': False}
        return parsed if isinstance(parsed, dict) else None
