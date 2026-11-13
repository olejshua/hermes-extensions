"""Real-source adapters for prep_daemon, reusing the installed helpers.

Reads only. TwentyEvidence adds CRM facts when 'twenty' is allowed. The mail collector searches only the attendee addresses of the
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
    name = 'mail'

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


def code_gmail_service(reads=None):
    """Gmail client for the owner's 'code' mailbox, only after the profile address equals CODE.

    Same acquisition and identity check as MailEvidence (mail_module.service('code') plus
    verified_profile, or users.getProfile). Building the client reads nothing by itself."""
    reads = reads or ExistingReads()
    svc = reads.mail_module.service('code')
    getter = getattr(reads.mail_module, 'verified_profile', None)
    actual = getter(svc) if callable(getter) else None
    if actual is None:
        actual = svc.users().getProfile(userId='me').execute().get('emailAddress', '')
    if not isinstance(actual, str) or actual.casefold() != CODE.casefold():
        raise ReviewRequired('mailbox_identity_mismatch')
    return svc


class TwentyEvidence:
    """Read-only CRM facts for the event's outside attendees, only when 'twenty' is allowed.

    A person counts only if an exact attendee address is on the returned record,
    so a filter that matches loosely cannot attach someone else's facts. Truncated
    result pages are skipped, not guessed. CRM text is data: control characters are
    stripped and length is bounded.

    Contract sources (not a live run): the filter form {emails:{primaryEmail:{eq}}}
    is used by Twenty's own partners app source, the additionalEmails like-filter
    appears in twentyhq/twenty issue 12965, jobTitle in the server's person field
    list, and pageInfo/stage/closeDate/companyId filters match this repo's existing
    queries. Twenty's docs say every workspace has its own schema, so the first
    collect checks the live metadata for the fields used and refuses if any is
    missing. Results stay an address-checked subset, not a completeness proof.
    """
    name = 'twenty'
    REQUIRED = {'person': {'name', 'emails', 'jobTitle', 'company'},
                'opportunity': {'name', 'stage', 'closeDate', 'company'}}
    PERSON = ('query($filter:PersonFilterInput!){people(first:5,filter:$filter){pageInfo{hasNextPage} '
              'edges{node{id companyId name{firstName lastName} jobTitle emails{primaryEmail additionalEmails} '
              'company{name}}}}}')
    OPPORTUNITIES = ('query($filter:OpportunityFilterInput!){opportunities(first:5,filter:$filter){pageInfo{hasNextPage} '
                     'edges{node{id name stage closeDate}}}}')

    def __init__(self, client, own=(CODE,)):
        self.client, self.own = client, {a.casefold() for a in own}
        self.schema_ok = False

    def _check_schema(self):
        if self.schema_ok:
            return
        objects = self.client.metadata()
        for kind, needed in self.REQUIRED.items():
            matches = [o for o in objects if o.get('nameSingular') == kind and o.get('isActive')]
            if len(matches) != 1:
                raise ReviewRequired('twenty_schema_object_missing')
            have = {f.get('name') for f in matches[0].get('fields', []) if f.get('isActive')}
            if not needed <= have:
                raise ReviewRequired('twenty_schema_field_missing')
        self.schema_ok = True

    @staticmethod
    def _clean(value, limit=120):
        return _CONTROL.sub(' ', str(value or '')).strip()[:limit]

    def collect(self, event, allowed):
        if 'twenty' not in {a.casefold() for a in allowed}:
            return []
        addresses = sorted({a['email'].casefold() for a in event.get('attendees', [])
                            if a.get('email') and not a.get('self') and not a.get('resource')
                            and a['email'].casefold() not in self.own and _ADDRESS.fullmatch(a['email'])})
        if not addresses:
            return []
        self._check_schema()
        items, seen_companies = [], set()
        for address in addresses[:6]:
            match = {'or': [{'emails': {'primaryEmail': {'eq': address}}},
                            {'emails': {'additionalEmails': {'like': '%' + address + '%'}}}]}
            found = self.client.query(self.PERSON, {'filter': match})['people']
            if found.get('pageInfo', {}).get('hasNextPage'):
                continue
            for row in found.get('edges', []):
                person = row['node']
                mail = person.get('emails') or {}
                have = {str(mail.get('primaryEmail') or '').casefold(), *[str(e).casefold() for e in mail.get('additionalEmails') or []]}
                if address not in have:
                    continue
                name = ' '.join(filter(None, ((person.get('name') or {}).get('firstName'), (person.get('name') or {}).get('lastName'))))
                company = (person.get('company') or {}).get('name')
                text = self._clean(f"CRM: {name}" + (f", {person['jobTitle']}" if person.get('jobTitle') else '') +
                                   (f" bei {company}" if company else ''))
                items.append(dict(source='twenty', ref='twenty:person:' + str(person['id']), text=text))
                cid = person.get('companyId')
                if cid and cid not in seen_companies:
                    seen_companies.add(cid)
                    opps = self.client.query(self.OPPORTUNITIES, {'filter': {'companyId': {'eq': cid}}})['opportunities']
                    if opps.get('pageInfo', {}).get('hasNextPage'):
                        continue
                    for o in opps.get('edges', []):
                        node = o['node']
                        items.append(dict(source='twenty', ref='twenty:opportunity:' + str(node['id']),
                                          text=self._clean(f"CRM-Chance {node.get('name')}: Stufe {node.get('stage')}")))
        return items


class Evidence(list):
    """Collected items plus the sources that could not be read."""

    def __init__(self, items=(), failed_sources=()):
        super().__init__(items)
        self.failed_sources = tuple(failed_sources)


class CombinedEvidence:
    """Several collectors behind the one collect() the daemon expects.

    A failing collector never discards what the others verified: its name goes
    into failed_sources so the note states the gap instead of implying coverage.
    """

    def __init__(self, *collectors):
        self.collectors = collectors

    def collect(self, event, allowed):
        items, failed = [], []
        for collector in self.collectors:
            try:
                items.extend(collector.collect(event, allowed))
            except Exception:
                failed.append(getattr(collector, 'name', 'unknown'))
        return Evidence(items, failed)


class Unconfigured:
    """Placeholder for an allowed source that has no private configuration: always a named gap."""

    def __init__(self, name):
        self.name = name

    def collect(self, event, allowed):
        if self.name in {a.casefold() for a in allowed}:
            raise ReviewRequired(self.name + '_not_configured')
        return []


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
