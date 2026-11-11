"""Read-only Google appointment evidence; account-scoped credential refresh."""
from __future__ import annotations
import argparse
import base64
from datetime import date, datetime, time, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
import json
import hashlib
import time as monotime
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from gtm.calendar.confirmation import MailEvidence
from gtm.calendar.reconciliation import calendar_status, proposal_relation, source_slots, INTERNAL, _interval
from gtm.calendar.routing import CODE, FUXAM, ACRIBIC, AVAILABILITY, verified_calendars

CONTRACT = 'ole-appointment-status-v2'
TZ = ZoneInfo('Europe/Berlin')
ACCOUNTS = {'code': 'operator@primary.example.invalid', 'stealth': 'operator@secondary.example.invalid'}

class Unavailable(Exception):
    pass

class GoogleRead:
    """Only allowlisted GET resources, using a bearer from the account auth store."""
    def __init__(self, token):
        self.token = token

    def get(self, resource, **params):
        if re.search(r'^gmail/v1/users/me/messages/(?:send|insert|import|modify|trash|untrash|batchDelete|batchModify)(?:/|$)', resource):
            raise Unavailable('read_resource_not_allowed')
        if not re.fullmatch(r'(gmail/v1/users/me/(profile|threads/[\w-]+|drafts(?:/[\w-]+)?|messages(?:/[\w-]+(?:/attachments/[\w-]+)?)?)|calendar/v3/users/me/calendarList|calendar/v3/calendars/[^/]+/events(?:/[^/]+)?)', resource):
            raise Unavailable('read_resource_not_allowed')
        url = 'https://www.googleapis.com/' + resource
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url + ('?' + query if query else ''), headers={'Authorization': 'Bearer ' + self.token}, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                return json.load(response)
        except Exception as exc:
            raise Unavailable('http_' + str(getattr(exc, 'code', 'unavailable'))) from None


def connect(account):
    try:
        if account not in ACCOUNTS:
            raise Unavailable('credential_unavailable')
        sys.path.insert(0, '/home/hermes/.hermes/scripts')
        try:
            from google_auth_store import load_credentials_for_path
        finally:
            sys.path.pop(0)
        credentials = load_credentials_for_path(
            Path('/home/hermes/.hermes') / ('google_token_' + account + '.json'))
        api = GoogleRead(credentials.token)
        if api.get('gmail/v1/users/me/profile').get('emailAddress', '').casefold() != ACCOUNTS[account]:
            raise Unavailable('account_identity_mismatch')
        return api
    except Unavailable:
        raise
    except Exception:
        raise Unavailable('credential_unavailable') from None


def pages(api, resource, field='items', **params):
    token, seen = None, set()
    while True:
        result = api.get(resource, **params, pageToken=token)
        yield from result.get(field, [])
        token = result.get('nextPageToken')
        if not token:
            return
        if token in seen:
            raise Unavailable('pagination_cycle')
        seen.add(token)


def decode(data):
    return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))


def plain(message, api=None):
    from gtm.mail_observer.sources import _HTML
    def rendered(html):
        parser = _HTML(); parser.feed(html); parser.close()
        return ' '.join(parser.text)
    if message.get('raw'):
        msg = BytesParser(policy=policy.default).parsebytes(decode(message['raw']))
        text = lambda p: p.get_payload(decode=True).decode(p.get_content_charset() or 'utf-8', errors='strict')
        parts = [text(p) for p in msg.walk() if p.get_content_type() == 'text/plain' and p.get_content_disposition() != 'attachment']
        if not parts:
            parts = [rendered(text(p)) for p in msg.walk() if p.get_content_type() == 'text/html' and p.get_content_disposition() != 'attachment']
        if not parts:
            raise Unavailable('plain_text_unavailable')
        return '\n'.join(parts)
    def visit(part, kind='text/plain'):
        if part.get('filename'):
            return []
        if part.get('mimeType') == kind:
            body = part.get('body', {})
            data = body.get('data')
            if data is None and body.get('attachmentId') and api:
                data = api.get('gmail/v1/users/me/messages/' + message['id'] + '/attachments/' + body['attachmentId']).get('data')
            if data is None:
                raise Unavailable('plain_body_missing')
            headers = {h['name'].lower(): h['value'] for h in part.get('headers', [])}
            charset = re.search(r'charset=["\']?([^;"\' ]+)', headers.get('content-type', ''), re.I)
            return [decode(data).decode(charset.group(1) if charset else 'utf-8', errors='strict')]
        return [s for child in part.get('parts', []) for s in visit(child, kind)]
    parts = visit(message.get('payload', {}))
    if not parts:
        parts = [rendered(value) for value in visit(message.get('payload', {}), 'text/html')]
    if not parts:
        raise Unavailable('plain_text_unavailable')
    return '\n'.join(parts)


def unquoted(text):
    # Preserve exact prefix bytes/spacing; do not normalize or rewrite the draft.
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if re.match(r'^\s*(>|On .+wrote:|Am .+schrieb.*:|Le .+écrit.*:|From:|Von:|[-_]{3,}\s*(Original|Forwarded|Ursprüngliche))', line, re.I):
            return ''.join(lines[:i])
    return text


def mapping(event):
    props = event.get('extendedProperties', {}).get('private', {})
    ids = set()
    for key in ('sourceThreadId', 'gmailThreadId', 'threadId', 'sourceThread'):
        if props.get(key):
            ids.add(props[key])
    if props.get('hermesOwner'):
        try:
            owner = json.loads(props['hermesOwner'])
            if not isinstance(owner, list) or len(owner) != 3 or not isinstance(owner[1], str):
                return None
            ids.add(owner[1])
        except (TypeError, ValueError):
            return None
    ids.update(re.findall(r'^Mailthread:\s*([a-zA-Z0-9_-]+)\s*$', event.get('description', ''), re.M))
    if len(ids) != 1 or not re.fullmatch(r'[a-zA-Z0-9_-]+', next(iter(ids), '')):
        return None
    return ids.pop()


def stamp(message):
    return int(message['internalDate'])


class Snapshot:
    """One invocation's cache; never persists sources or credentials."""
    def __init__(self, connector):
        self.connector, self.clients, self.cache, self.calls = connector, {}, {}, []
    def api(self, alias):
        if alias not in self.clients:
            self.clients[alias] = self.connector(alias)
        snapshot = self
        class Read:
            def get(_, resource, **params):
                key = (alias, resource, json.dumps(params, sort_keys=True))
                if key not in snapshot.cache:
                    started = monotime.monotonic()
                    try:
                        snapshot.cache[key] = snapshot.clients[alias].get(resource, **params)
                    finally:
                        snapshot.calls.append({'account': alias, 'resource': resource.split('/')[0],
                                               'seconds': round(monotime.monotonic()-started, 4)})
                return snapshot.cache[key]
        return Read()


def source_reference(message, headers, body, alias):
    return {'message_id': message['id'], 'thread_id': message['threadId'],
            'timestamp_ms': stamp(message), 'labels': message.get('labelIds', []),
            'from': [a.casefold() for _, a in getaddresses([headers.get('from', '')])],
            'to': [a.casefold() for _, a in getaddresses([headers[k] for k in ('to', 'cc') if headers.get(k)])],
            'subject': headers.get('subject', ''), 'new_text': unquoted(body),
            'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
            'display': 'authored_prefix_only_full_original_available',
            'full_source': {'account': alias, 'message_id': message['id']}}


def mail_evidence(api, tid, drafts, event, account, show_drafts):
    from gtm.mail_observer.sources import checked_headers
    thread = api.get('gmail/v1/users/me/threads/' + tid, format='full')
    if thread.get('id') != tid or not isinstance(thread.get('messages'), list) or not thread['messages']:
        raise Unavailable('thread_identity_or_content_missing')
    messages = sorted(thread['messages'], key=stamp)
    sources, public, errors, current = [], [], [], []
    seen = set()
    for message in messages:
        try:
            if message.get('threadId') != tid or message['id'] in seen:
                raise Unavailable('thread_message_identity_changed')
            seen.add(message['id'])
            headers = checked_headers([(h['name'], h['value']) for h in message.get('payload', {}).get('headers', [])])
            body = plain(message, api)
            sender = [a.casefold() for _, a in getaddresses([headers.get('from', '')])]
            recipients = tuple(sorted({a.casefold() for _, a in getaddresses(
                [headers[k] for k in ('to', 'cc') if headers.get(k)]) if a}))
            if len(sender) != 1 or not recipients:
                raise Unavailable('mail_authority_unavailable')
            source = MailEvidence(ACCOUNTS[account], message['id'], tid, sender[0], recipients,
                                  body, tuple(message.get('labelIds', [])), headers.get('message-id', '').strip(),
                                  headers.get('in-reply-to', '').strip(), True)
            sources.append((source, message['internalDate']))
            if 'DRAFT' not in source.labels:
                public.append(source_reference(message, headers, body, account))
        except Exception as exc:
            errors.append({'message_id': message.get('id'), 'error': safe_error(exc)})
    for draft in drafts:
        if draft.get('message', {}).get('threadId') != tid:
            continue
        try:
            message = api.get('gmail/v1/users/me/drafts/' + draft['id'], format='full')['message']
            if message.get('threadId') != tid or 'DRAFT' not in message.get('labelIds', []) or 'SENT' in message.get('labelIds', []):
                raise Unavailable('draft_identity_or_labels_changed')
            body = plain(message, api)
            headers = checked_headers([(h['name'], h['value']) for h in message.get('payload', {}).get('headers', [])])
            sender = [a.casefold() for _, a in getaddresses([headers.get('from', '')])]
            recipients = tuple(sorted({a.casefold() for _, a in getaddresses([headers[k] for k in ('to', 'cc') if headers.get(k)]) if a}))
            if sender != [ACCOUNTS[account]] or not recipients:
                raise Unavailable('draft_authority_changed')
            item = {'draft_id': draft['id'], 'message_id': message['id'], 'timestamp_ms': stamp(message),
                    'status': 'draft_unsent', 'full_source': {'account': account, 'message_id': message['id']}}
            if show_drafts:
                item['text'] = unquoted(body)
            current.append(item)
            sources = [(s, t) for s, t in sources if s.message_id != message['id']]
            sources.append((MailEvidence(ACCOUNTS[account], message['id'], tid, sender[0], recipients, body,
                            tuple(message['labelIds']), headers.get('message-id', '').strip(), headers.get('in-reply-to', '').strip(), True), message['internalDate']))
        except Exception as exc:
            errors.append({'draft_id': draft['id'], 'error': safe_error(exc)})
    point = event.get('start', {}).get('dateTime')
    slot = datetime.fromisoformat(point.replace('Z', '+00:00')) if point else None
    proposals = [(s, t) for s, t in sources if s.sender == ACCOUNTS[account] and
                 'SENT' in s.labels and 'DRAFT' not in s.labels and s.rfc_message_id and slot in source_slots(s, t)]
    latest = max(proposals, key=lambda row: int(row[1]), default=None)
    public_by_id = {m['message_id']: m for m in public}
    sent = public_by_id.get(latest[0].message_id) if latest else None
    replies = [m for m in public if latest and m['timestamp_ms'] > int(latest[1]) and
               not set(m['labels']) & {'SENT', 'DRAFT'} and set(m['from']) & set(latest[0].recipients)]
    reply = max(replies, key=lambda m: m['timestamp_ms'], default=None)
    state = ('reply_requires_interpretation' if reply else 'proposed_unanswered') if sent else 'draft_unsent' if current else 'unknown'
    if errors:
        state = 'unknown'
    # A directly read, authority-bound current draft remains a positive fact
    # even when an older message is unavailable. This does not assert that no
    # separate acceptance exists; history coverage remains independently false.
    known_draft = bool(current)
    if known_draft and not sent:
        state = 'draft_unsent'
    return ({'mail_errors': errors, 'email_status': state, 'latest_sent_proposal': sent,
             'latest_counterparty_response': reply, 'mail_sources': public,
             'current_drafts': sorted(current, key=lambda d: d['timestamp_ms']), 'thread_id': tid,
             'draft_unsent_verified': known_draft, 'history_complete': not errors,
             'absence_of_acceptance_proven': False}, sources)


def classify(api, event, drafts, account, show_drafts=False):
    props = event.get('extendedProperties', {}).get('private', {})
    hold = props.get('hermesSlotHold') == '1'
    invited = bool(event.get('attendees')) and not hold
    row = {'event_id': event['id'], 'summary': event.get('summary', ''), 'start': event.get('start'),
           'end': event.get('end'), 'kind': 'proposal_hold' if hold else 'invited_event' if invited else 'calendar_event',
           'status': 'unknown' if hold else calendar_status(event, ACCOUNTS[account]),
           'attendees': event.get('attendees', []), 'event_updated': event.get('updated'),
           'iCalUID': event.get('iCalUID'), 'evidence': []}
    tid = mapping(event)
    sources = []
    if hold and not tid:
        row['mail_error'] = 'missing_or_conflicting_explicit_thread_mapping'
    if tid:
        try:
            proof, sources = mail_evidence(api, tid, drafts, event, account, show_drafts)
            row.update(proof)
            if hold:
                row['status'] = row['email_status']
        except Exception as exc:
            row['mail_error'] = safe_error(exc)
    return row, sources


def _selected_calendars(api, alias, coverage):
    rows = list(pages(api, 'calendar/v3/users/me/calendarList', maxResults=250))
    if alias == 'code':
        class Request:
            def execute(self): return {'items': rows}
        class Adapter:
            def calendarList(self): return self
            def list(self, **kwargs): return Request()
        try:
            verified_calendars(Adapter())
        except ValueError:
            coverage['errors'].append('required_calendar_identity_unverified')
        selected = [c for c in rows if c.get('id') in AVAILABILITY]
        if len({c['id'] for c in selected}) != len(selected):
            raise Unavailable('duplicate_calendar_identity')
        return selected
    selected = [c for c in rows if c.get('id') == ACCOUNTS[alias] and c.get('primary')]
    if len(selected) != 1:
        raise Unavailable('required_primary_calendar_identity_unverified')
    return selected


def _mirror(row, event, snapshot):
    props = event.get('extendedProperties', {}).get('private', {})
    if props.get('blockerPurpose') != 'private_call_mirror':
        return
    row['kind'] = 'meeting_mirror'
    row['status'] = 'unknown'
    try:
        cid, eid, uid = (props.get(k) for k in ('stealthSourceCalendar', 'stealthSourceEvent', 'stealthSourceUID'))
        if cid != ACCOUNTS['stealth'] or not eid or not uid or event.get('attendees'):
            raise Unavailable('mirror_source_binding_unverified')
        resource = 'calendar/v3/calendars/' + urllib.parse.quote(cid, safe='') + '/events/' + urllib.parse.quote(eid, safe='')
        try:
            # A source calendar explicitly shared with CODE may be readable
            # through the already verified principal. No routing/write change.
            actual = snapshot.api('code').get(resource)
        except Unavailable:
            actual = snapshot.api('stealth').get(resource)
        a, b = _interval(actual); x, y = _interval(event)
        if actual.get('id') != eid or actual.get('iCalUID') != uid or a != x or y < b:
            raise Unavailable('mirror_source_identity_or_interval_changed')
        row.update(mirrored_event={'account': 'stealth', 'calendar_id': cid,
                   'event_id': eid, 'iCalUID': uid, 'start': actual['start'], 'end': actual['end'],
                   'source_status': actual.get('status'), 'organizer': actual.get('organizer'),
                   'attendees': actual.get('attendees', []), 'updated': actual.get('updated')})
        if not actual.get('organizer') or not actual.get('attendees'):
            raise Unavailable('mirror_source_participant_details_unavailable')
        row['status'] = calendar_status(actual, ACCOUNTS['stealth'])
    except Exception as exc:
        row['mirror_error'] = safe_error(exc)


def report(day, days=1, account='code', show_drafts=False, connector=connect):
    if type(days) is not int or not 1 <= days <= 14 or account not in (*ACCOUNTS, None):
        raise ValueError('valid_account_and_one_to_14_days_required')
    lower = datetime.combine(date.fromisoformat(day), time(), TZ)
    upper = lower + timedelta(days=days)
    horizon = upper + timedelta(days=14)
    started = monotime.monotonic(); snapshot = Snapshot(connector)
    out = {'contract': CONTRACT, 'timezone': str(TZ), 'time_min': lower.isoformat(), 'time_max': upper.isoformat(),
           'related_search_until': horizon.isoformat(), 'checked_at': datetime.now(timezone.utc).isoformat(),
           'read_only': True, 'coverage': [], 'events': [], 'related_events': [], 'complete': True}
    for alias in ([account] if account else ACCOUNTS):
        coverage = {'account': alias, 'calendars': [], 'errors': []}; out['coverage'].append(coverage)
        try:
            api = snapshot.api(alias); selected = _selected_calendars(api, alias, coverage)
            try:
                drafts = list(pages(api, 'gmail/v1/users/me/drafts', field='drafts', maxResults=500))
            except Exception:
                drafts = None; coverage['errors'].append('draft_inventory_unavailable')
            records = []
            for cal in selected:
                record = {'calendar_id': cal['id'], 'summary': cal.get('summary'), 'complete': False}
                coverage['calendars'].append(record)
                try:
                    events = list(pages(api, 'calendar/v3/calendars/' + urllib.parse.quote(cal['id'], safe='') + '/events',
                                  timeMin=lower.isoformat(), timeMax=horizon.isoformat(), singleEvents='true', maxResults=2500))
                    records.extend((cal['id'], event) for event in events if event.get('status') != 'cancelled')
                    record['complete'] = True
                except Exception as exc:
                    record['error'] = safe_error(exc); coverage['errors'].append('calendar_read_incomplete')
            day_rows = []
            for cid, event in records:
                try:
                    begin, finish = _interval(event)
                except (KeyError, ValueError, TypeError):
                    coverage['errors'].append('event_interval_unavailable'); continue
                if not begin < upper or not finish > lower:
                    continue
                row, sources = classify(api, event, drafts or [], alias, show_drafts)
                row.update(account=alias, calendar_id=cid)
                if drafts is None and row['kind'] == 'proposal_hold':
                    row['draft_coverage'] = 'unknown'; row['status'] = 'unknown'
                _mirror(row, event, snapshot)
                if row.get('mirror_error'):
                    coverage['errors'].append('mirror_source_incomplete')
                if mapping(event) and (row.get('mail_error') or row.get('mail_errors')):
                    coverage['errors'].append('mapped_mail_incomplete')
                if row['kind'] == 'proposal_hold' and sources and not row.get('mail_errors') and drafts is not None:
                    matches = []
                    for other_cid, candidate in records:
                        if candidate.get('id') == event.get('id') and cid == other_cid:
                            continue
                        relation = proposal_relation(event, candidate, sources)
                        if relation['state'] in ('selected_proposal', 'obsolete_proposal'):
                            matches.append((other_cid, candidate, relation))
                    if len(matches) == 1:
                        other_cid, chosen, relation = matches[0]
                        row['status'] = relation['state']; row['reconciliation'] = relation
                        row['confirmed_alternative'] = {'account': alias, 'calendar_id': other_cid,
                            'event_id': chosen['id'], 'start': chosen['start'], 'end': chosen['end'], 'iCalUID': chosen.get('iCalUID')}
                        key = (alias, other_cid, chosen['id'])
                        if not any((r['account'], r['calendar_id'], r['event_id']) == key for r in out['related_events']):
                            out['related_events'].append({'account': alias, 'calendar_id': other_cid,
                                'event_id': chosen['id'], 'summary': chosen.get('summary'), 'start': chosen['start'],
                                'end': chosen['end'], 'status': 'confirmed', 'attendees': chosen.get('attendees', []),
                                'outside_requested_window': not (_interval(chosen)[0] < upper and _interval(chosen)[1] > lower)})
                    elif len(matches) > 1:
                        row.update(status='conflict', reconciliation_error='several_accepted_alternatives')
                day_rows.append(row)
            out['events'].extend(day_rows)
        except Exception as exc:
            coverage['errors'].append(safe_error(exc))
        coverage['errors'] = sorted(set(coverage['errors']))
        if coverage['errors']:
            out['complete'] = False
    out['events'].sort(key=lambda r: (str(r.get('start')), r['calendar_id'], r['event_id']))
    out['metrics'] = {'api_requests_excluding_connect_profile': len(snapshot.calls),
                      'api_seconds': round(sum(c['seconds'] for c in snapshot.calls), 4),
                      'elapsed_seconds': round(monotime.monotonic()-started, 4), 'model_calls': 0, 'saas_writes': 0}
    return out


def source_read(account, message_id, connector=connect):
    if account not in ACCOUNTS or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', message_id):
        raise ValueError('exact_source_identity_required')
    api = connector(account)
    message = api.get('gmail/v1/users/me/messages/' + message_id, format='raw')
    if message.get('id') != message_id or not message.get('raw'):
        raise Unavailable('raw_original_identity_unavailable')
    raw = decode(message['raw'])
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    if parsed.defects:
        raise Unavailable('raw_original_mime_defect')
    try:
        body = plain(message, api); text_error = None
    except (Unavailable, UnicodeError, LookupError):
        body = None; text_error = 'original_text_unavailable_raw_preserved'
    return {'contract': CONTRACT, 'account': account, 'read_only': True, 'message_id': message_id,
            'thread_id': message['threadId'], 'timestamp_ms': stamp(message), 'labels': message.get('labelIds', []),
            'headers': list(parsed.items()), 'body': body, 'text_error': text_error, 'raw_base64url': message['raw'],
            'raw_sha256': hashlib.sha256(raw).hexdigest(), 'raw_complete': True, 'complete': True}


def safe_error(exc):
    from gtm.mail_observer.sources import SourceBlocked
    if isinstance(exc, SourceBlocked) and re.fullmatch(r'[a-z][a-z0-9_]{0,80}', exc.code):
        return exc.code
    return str(exc) if isinstance(exc, Unavailable) else 'evidence_unavailable'


def human(value):
    if 'raw_complete' in value:
        return value['body'] if value['body'] is not None else 'Originaltext nicht lesbar; vollständige RFC-Quelle im JSON verfügbar.'
    lines = ['Coverage: ' + ('complete' if value['complete'] else 'INCOMPLETE — unavailable sources may contain meetings')]
    for row in value['events']:
        lines.append(f"{row['start']} | {row['summary']} | {row['kind']} | {row['status']}")
        for draft in row.get('current_drafts', []):
            if 'text' in draft:
                lines.append('Current unsent draft:\n' + draft['text'])
    for coverage in value['coverage']:
        if coverage['errors']:
            lines.append(coverage['account'] + ': ' + ', '.join(coverage['errors']))
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('date', nargs='?', help='YYYY-MM-DD in Europe/Berlin')
    parser.add_argument('--days', type=int, default=1)
    parser.add_argument('--account', choices=ACCOUNTS, default='code')
    parser.add_argument('--source', help='Read exact full raw original by Gmail message ID')
    parser.add_argument('--show-drafts', action='store_true', help='Include exact current unquoted draft text (private)')
    parser.add_argument('--format', choices=('json', 'human'), default='json')
    args = parser.parse_args()
    try:
        value = source_read(args.account, args.source) if args.source else report(args.date, args.days, args.account, args.show_drafts)
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    except Unavailable as exc:
        value = {'contract': CONTRACT, 'complete': False, 'read_only': True, 'error': safe_error(exc)}
    print(json.dumps(value, ensure_ascii=False, separators=(',', ':')) if args.format == 'json' else human(value))

if __name__ == '__main__':
    main()
