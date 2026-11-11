"""Managed draft boundary: deterministic mail text and durable scheduling claims.
All managed scheduling writers must hold scheduling() across Gmail write/readback.
Manual Gmail edits and other calendar clients cannot participate in this lock.
"""
import fcntl
import copy
import base64
import hashlib
import html
import json
import os
import re
import tempfile
import unicodedata
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from urllib.parse import quote
try:
    from calendar_routing import verified_calendars, calendar_id_for, claim_calendar, project_from_thread
except ModuleNotFoundError as error:
    if error.name != 'calendar_routing':
        raise
    from gtm.calendar.routing import verified_calendars, calendar_id_for, claim_calendar, project_from_thread

PRIMARY = 'operator@primary.example.invalid'
BERLIN = ZoneInfo('Europe/Berlin')
HOME = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
STATE_DIR = HOME / 'email_watcher' / 'slot_claims'
QUOTE = re.compile(r'(?m)^(?:>.*|On .+wrote:|Am .+schrieb.+:|-----Ursprüngliche Nachricht-----|Von:|From:)')
CLOCK = re.compile(r'\b\d{1,2}[:：﹕]\d{2}\b|\b\d{1,2}(?:\.\d{2})?\s*(?:Uhr|h|a\.?m\.?|p\.?m\.?)\b|\b(?:um|at)\s+\d{1,2}\b', re.I)
DATE = re.compile(r'\b\d{1,2}[./]\d{1,2}[./](?:\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b')
RELATIVE = re.compile(r'\b(?:morgen|übermorgen|heute|nächste Woche|tomorrow|next week|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)\b', re.I)
PROPOSAL = re.compile(r'\b(?:Termin|Call|Meeting|Gespräch|treffen|Austausch|passt|verfügbar|frei|Vorschlag|schlage|schedule|available)\b', re.I)
CLOSING = re.compile(r'(?is)(?:^|\n+)(?:Beste Grüße|Mit besten Grüßen|Mit freundlichen Grüßen|Liebe Grüße|Viele Grüße|Herzliche Grüße|VG)\s*(?:Alex(?: Beispiel)?\s*)*$')

def split_quote(text):
    match = QUOTE.search(text)
    return (text[:match.start()], text[match.start():]) if match else (text, '')

def start(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('Slot requires an explicit timezone')
    if result.second or result.microsecond:
        raise ValueError('Slot requires whole minutes')
    return result.astimezone(timezone.utc)

def label(value):
    dt = start(value).astimezone(BERLIN)
    day = ('Montag','Dienstag','Mittwoch','Donnerstag','Freitag','Samstag','Sonntag')[dt.weekday()]
    return f'{day}, {dt:%d.%m.%Y}, {dt:%H:%M} Uhr'

class MailPolicyReviewRequired(ValueError):
    pass

def without_timezone_label(text):
    # Internal datetime offsets are untouched. Only redundant recipient-facing labels.
    labels = r'\b(?:Berliner[ \t]+Zeit|Berlin[ \t]+time|Europe/Berlin|ME(?:S)?Z|CE(?:S)?T)\b'
    text = re.sub(r'[ \t]*\([ \t]*(?:alle[ \t]+(?:Zeiten|Uhrzeiten)[ \t]+(?:sind[ \t]+)?(?:in[ \t]+)?)?' + labels + r'[ \t]*\)', '', text, flags=re.I)
    text = re.sub(r'(?im)^[ \t]*(?:Alle[ \t]+(?:Zeiten|Uhrzeiten|Termine|Terminvorschläge)|Die[ \t]+(?:Zeiten|Uhrzeiten))[ \t]+(?:(?:sind|gelten|verstehen sich)[ \t]+)?(?:in[ \t]+|nach[ \t]+)?' + labels + r'[.!]?[ \t]*\n?', '', text)
    text = re.sub(r'(?<=Uhr)[ \t]+(?:nach[ \t]+|in[ \t]+)?' + labels + r'\b', '', text, flags=re.I)
    if re.search(labels, text, re.I):
        raise MailPolicyReviewRequired('Recipient timezone prose requires rewrite before draft creation')
    return text.strip()


def format_body(text, slots=(), *, force_formal=False):
    # Quoted history is retained verbatim, including its original dash characters.
    own, quoted = split_quote(text)
    own = own.replace('\r\n', '\n').replace('\r', '\n').strip()
    own = ''.join('-' if unicodedata.category(c) == 'Pd' or c in '\u2212\u2043' else c for c in own)
    # Only our new prose is normalized; quoted correspondence remains verbatim.
    own = without_timezone_label(own)
    slot_values = [start(s).isoformat() for s in slots]
    if len(set(slot_values)) != len(slot_values):
        raise MailPolicyReviewRequired('Duplicate slot metadata')
    # Language intent belongs to the source/body-bound semantic write gate.
    # Rendering only binds declared starts to their exact displayed labels.
    for value in slot_values:
        expected = label(value)
        if own.count(expected) != 1:
            raise MailPolicyReviewRequired('Proposed date/time must match exact verified start label: ' + expected)
    match = CLOSING.search(own)
    existing_full = bool(match and 'Alex Beispiel' in match.group())
    formal = bool(re.search(r'\b(?:[Ss]ehr geehrt\w*|Herrn?|Frau|Sie|Ihnen|Ihre[nmrs]?)\b', own))
    informal = bool(re.search(r'\b(?:du|dir|dich|dein\w*|euch|euer\w*)\b', own, re.I))
    existing_short = bool(match and re.search(r'\bOle\b', match.group()))
    name = 'Alex Beispiel' if force_formal or formal or existing_full else 'Alex' if informal or existing_short else 'Alex Beispiel'
    clean = own[:match.start()].rstrip() if match else own
    result = clean + '\n\nBeste Grüße\n' + name
    return result + ('\n\n' + quoted if quoted else '')

def body_html(text):
    """Render authored prose and signoff together, before any quoted history.

    A line break is a BR, never a paragraph margin or a separate signature div.
    This controls our MIME layout, not Gmail's undocumented trimming heuristic.
    Input is formatted plain text; supplied markup is escaped, never executed.
    """
    own, quoted = split_quote(text)
    rendered = '<div dir="ltr">' + html.escape(own.rstrip()).replace('\n', '<br>\n') + '</div>'
    if quoted:
        rendered += '<blockquote>' + html.escape(quoted).replace('\n', '<br>\n') + '</blockquote>'
    return '<html><body>' + rendered + '</body></html>'

def mime_body_matches(payload, plain, rendered):
    """Verify both alternatives from Gmail full readback, ignoring attachments."""
    found = {'text/plain': [], 'text/html': []}
    def visit(part):
        disposition = next((h.get('value', '') for h in part.get('headers', [])
                            if h.get('name', '').lower() == 'content-disposition'), '')
        if part.get('filename') or disposition.lower().startswith('attachment'):
            return
        kind = part.get('mimeType')
        if kind in found:
            data = part.get('body', {}).get('data')
            if not isinstance(data, str):
                raise ValueError('Missing MIME body data')
            found[kind].append(base64.b64decode(data + '=' * (-len(data) % 4),
                                             altchars=b'-_', validate=True).decode('utf-8'))
        for child in part.get('parts', []):
            visit(child)
    def normalized(value):
        return value.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n')
    try:
        visit(payload)
        return all(len(found[kind]) == 1 and normalized(found[kind][0]) == normalized(expected)
                   for kind, expected in [('text/plain', plain), ('text/html', rendered)])
    except (ValueError, TypeError, UnicodeError):
        return False

def _read():
    path = STATE_DIR / 'claims.json'
    if not path.exists():
        return {}
    claims = json.loads(path.read_text())
    if not isinstance(claims, dict):
        raise ValueError('Invalid claim journal; manual review required')
    return claims

def _save(claims):
    fd, temporary = tempfile.mkstemp(dir=STATE_DIR, prefix='claims.')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(claims, f, sort_keys=True)
            f.flush(); os.fsync(f.fileno())
        os.replace(temporary, STATE_DIR / 'claims.json')
        directory = os.open(STATE_DIR, os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def verified_release(claim):
    """Only a source-bound, attempted and read-back verified release is inactive."""
    proof=claim.get('release') or {}
    try:
        owner=json.loads(claim['owner'])
        cid=claim_calendar(claim)
        slot=start(claim['start']).astimezone(timezone.utc).isoformat()
        key=(owner[0]+'|'+owner[1]+'|'+slot if claim.get('kind')=='legacy_slot_hold' else claim['owner']+'|'+slot)
        expected=hashlib.sha256(key.encode()).hexdigest()[:40]
        if claim.get('kind')=='description_slot_hold':
            # Random-ID old blockers are accepted only with the frozen exact
            # description UUID, original SENT hash and event-revision proof.
            event_id=claim.get('event_id');check_id=claim.get('description_check_id')
            event_hash=claim.get('description_event_hash');source_hash=claim.get('legacy_source_hash')
            if (owner[0]!='code' or not isinstance(event_id,str) or not re.fullmatch(r'[0-9a-v]{5,1024}',event_id) or
                not isinstance(check_id,str) or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',check_id) or
                not isinstance(event_hash,str) or not re.fullmatch(r'[a-f0-9]{64}',event_hash) or
                not isinstance(source_hash,str) or not re.fullmatch(r'[a-f0-9]{64}',source_hash) or
                not isinstance(claim.get('legacy_source_message'),str) or not claim['legacy_source_message'] or
                proof.get('description_event_hash')!=event_hash or proof.get('description_check_id')!=check_id or
                proof.get('description_source_hash')!=source_hash):return False
            expected=event_id
    except (ValueError,KeyError,TypeError,IndexError):return False
    if claim.get('kind')=='legacy_slot_hold' and (not claim.get('legacy_source_message') or
        not isinstance(claim.get('legacy_source_hash'),str) or not re.fullmatch(r'[a-f0-9]{64}',claim['legacy_source_hash'])):return False
    return (claim.get('state')=='released_after_verified_invitation' and
        proof.get('attempted') is True and proof.get('verified') is True and
        proof.get('calendar_id',PRIMARY)==cid and
        proof.get('account')==PRIMARY and proof.get('hold_id')==claim.get('event_id')==expected and
        isinstance(owner,list) and len(owner)==3 and owner[0] in ('code','stealth') and bool(owner[1]) and
        owner[2]==[proof.get('recipient')] and
        proof.get('source_account',PRIMARY)==({'code':PRIMARY,'stealth':'operator@secondary.example.invalid'}[owner[0]]) and
        isinstance(proof.get('source_hash'),str) and bool(re.fullmatch(r'[a-f0-9]{64}',proof['source_hash'])) and
        isinstance(proof.get('invitation_event_id'),str) and bool(re.fullmatch(r'_?[a-v0-9]{5,1024}',proof['invitation_event_id'])))

def pending_busy():
    # Atomic journal reads. All intents remain busy, including uncertain/orphan writes.
    return [(start(c['start']).astimezone(BERLIN), start(c['start']).astimezone(BERLIN)+timedelta(minutes=30)) for c in _read().values() if not verified_release(c)]

def _get(calendar, event_id, calendar_id=PRIMARY):
    try:
        return calendar.events().get(calendarId=calendar_id, eventId=event_id).execute()
    except Exception as e:
        if getattr(getattr(e, 'resp', None), 'status', None) == 404: return None
        raise

def _calendars(calendar):
    return verified_calendars(calendar)

# One reviewed room reservation, never a role inferred from an event title.
# Example-only room reservation. Configure a verified private record locally.
ROOM_RESERVATION = {
    'calendar_id': PRIMARY, 'event_id': 'example-room-reservation',
    'start': '2000-01-01T13:00:00+01:00', 'end': '2000-01-01T15:30:00+01:00',
    'projects': ['acribic', 'stealth'],
    'material_sha256': '0000000000000000000000000000000000000000000000000000000000000000',
    'decision_thread': '', 'decision_question': '', 'decision_quote': '',
}
ROOM_POLICY_EPOCH = hashlib.sha256(json.dumps(ROOM_RESERVATION, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
_CALENDAR_SOURCE = ContextVar('verified_calendar_source', default=None)
_HOLD_SOURCE = ContextVar('native_hold_source', default=None)
HOLD_POLICY_EPOCH = hashlib.sha256(b'native-owned-hold-lineage-v2').hexdigest()
ROOM_RECHECK_SOURCE = {'account':'code','id':'e000000000000008','threadId':'e000000000000009'}


class CalendarSource:
    """Internal context constructed only from native full-source reads."""
    def __init__(self, account, thread, messages, *, primary_recipients=None):
        self.scope = None
        active = [m for m in messages if 'DRAFT' not in m.get('labels', [])]
        self.hold_source = self._hold_source(account,thread,active) if active else None
        if self.hold_source is not None:
            self.hold_source['reply_targets']=(None if primary_recipients is None else
                {a.casefold()for a in primary_recipients})
        if (not thread or not active or active[-1].get('thread_id') != thread
                or any(m.get('truncated') or not m.get('id') for m in active)):
            return
        try: project = project_from_thread(account, active)
        except (ValueError, KeyError, TypeError): return
        if project not in ROOM_RESERVATION['projects']: return
        if project == 'stealth' and not self._stealth_original(thread, active): return
        keys = ('id', 'thread_id', 'from', 'to', 'cc', 'internalDate', 'text',
                'rfc_message_id', 'in_reply_to', 'references', 'reference_source')
        material = [{**{k:m.get(k) for k in keys}, 'sent':'SENT' in m.get('labels', [])} for m in active]
        self.scope = dict(account=account, thread=thread, current_message=active[-1]['id'], project=project,
            source_sha256=hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest(), checks={})
    @staticmethod
    def _hold_source(account,thread,messages):
        source={'valid':False,'account':account,'thread':thread,'messages':messages}
        mailbox={'code':PRIMARY,'stealth':'operator@secondary.example.invalid'}.get(account)
        latest=messages[-1]
        if (not mailbox or not thread or latest.get('thread_id')!=thread
                or any(m.get('truncated')or not m.get('id')or
                       m.get('thread_id')!=thread and m.get('reference_source')is not True for m in messages)
                or len({m['id']for m in messages})!=len(messages)
                or {'SPAM','TRASH'}&set(latest.get('labels',[]))):return source
        sender=parseaddr(latest.get('from')or'')[1].casefold()
        targets={a.casefold()for _,a in getaddresses([latest.get('to')or'',latest.get('cc')or''])if a}
        incoming=bool(sender and sender!=mailbox and mailbox in targets and'SENT'not in latest.get('labels',[]))
        outgoing=sender==mailbox and'SENT'in latest.get('labels',[])and bool(targets-{mailbox})
        return {**source,'valid':incoming or outgoing,'contact':sender if incoming else None,
                'outgoing_targets':targets-{mailbox}if outgoing else set(),'mailbox':mailbox}
    @staticmethod
    def _stealth_original(thread, messages):
        # Native Stealth routing identifies its mailbox, not a reply's lineage.
        # Require the actual incoming original to reach an owned SENT ancestor.
        mailbox = 'operator@secondary.example.invalid'
        latest = messages[-1]
        addresses = lambda m: {a.casefold() for _,a in getaddresses([m.get('to') or '',m.get('cc') or '']) if a}
        if ('SENT' in latest.get('labels', []) or mailbox not in addresses(latest)
                or parseaddr(latest.get('from') or '')[1].casefold() in {'',mailbox}): return False
        indexed = {}
        for message in messages:
            rfc = message.get('rfc_message_id')
            if (not rfc or rfc in indexed or message.get('thread_id') != thread
                    and message.get('reference_source') is not True): return False
            indexed[rfc] = message
        current, visited = latest, set()
        while True:
            links = [current.get('in_reply_to')] + list(reversed(re.findall(r'<[^<>\s]+>',current.get('references') or '')))
            parent = next((indexed[rfc] for rfc in links if rfc in indexed and rfc not in visited),None)
            if parent is None: return False
            visited.add(parent['rfc_message_id'])
            try:
                if int(parent['internalDate']) >= int(current['internalDate']): return False
            except (KeyError,TypeError,ValueError): return False
            if ('SENT' in parent.get('labels', [])
                    and parseaddr(parent.get('from') or '')[1].casefold() == mailbox
                    and addresses(parent) - {mailbox}): return True
            current = parent
    @contextmanager
    def activate(self):
        token = _CALENDAR_SOURCE.set(self.scope)
        hold_token = _HOLD_SOURCE.set(self.hold_source)
        try: yield
        finally:
            _HOLD_SOURCE.reset(hold_token)
            _CALENDAR_SOURCE.reset(token)
    def wrap(self, callback):
        @wraps(callback)
        def read(*args, **kwargs):
            with self.activate(): return callback(*args, **kwargs)
        return read
    def evidence(self):
        if not self.scope or not self.scope['checks']: return None
        return {**{k:v for k,v in self.scope.items() if k != 'checks'},
                'contract':'exact-room-reservation-v1', 'policy_epoch':ROOM_POLICY_EPOCH,
                'calendar_id':PRIMARY, 'event_id':ROOM_RESERVATION['event_id'],
                'material_sha256':ROOM_RESERVATION['material_sha256'],
                'decision_question':ROOM_RESERVATION['decision_question'],
                'checks':[copy.deepcopy(v) for _,v in sorted(self.scope['checks'].items())]}


def room_range_allowed(lower, upper):
    scope = _CALENDAR_SOURCE.get()
    return bool(scope and scope['project'] in ROOM_RESERVATION['projects']
                and start(ROOM_RESERVATION['start']) <= lower < upper <= start(ROOM_RESERVATION['end']))


def _event_interval(event):
    def instant(value):
        if value.get('dateTime'):
            dt = datetime.fromisoformat(value['dateTime'].replace('Z', '+00:00'))
            if dt.tzinfo is None or dt.utcoffset() is None: raise ValueError('Calendar event timezone missing')
            return dt.astimezone(timezone.utc)
        return datetime.fromisoformat(value['date']).replace(tzinfo=BERLIN).astimezone(timezone.utc)
    a, b = instant(event['start']), instant(event['end'])
    if a >= b: raise ValueError('Calendar event interval invalid')
    return a, b


def _merged(intervals, lower, upper):
    result = []
    for a,b in sorted((max(a,lower),min(b,upper)) for a,b in intervals if a < upper and b > lower):
        if result and a <= result[-1][1]: result[-1] = (result[-1][0], max(result[-1][1],b))
        else: result.append((a,b))
    return result


def calendar_busy(calendar, cid, lower, upper, entry, *, own_id=None):
    """Fresh complete evidence; only an exact owned hold/approved room may leave."""
    if not isinstance(entry,dict) or entry.get('errors') or not isinstance(entry.get('busy'),list):
        raise ValueError('Calendar availability could not be verified')
    intervals = []
    for value in entry['busy']:
        a = datetime.fromisoformat(value['start'].replace('Z','+00:00'))
        b = datetime.fromisoformat(value['end'].replace('Z','+00:00'))
        if a.tzinfo is None or b.tzinfo is None or a >= b: raise ValueError('Calendar busy interval invalid')
        intervals.append((a.astimezone(timezone.utc), b.astimezone(timezone.utc)))
    room = cid == PRIMARY and room_range_allowed(lower,upper)
    if not own_id and not room: return _merged(intervals,lower,upper)
    page = calendar.events().list(calendarId=cid,timeMin=lower.isoformat(),timeMax=upper.isoformat(),
        singleEvents=True,showDeleted=False,maxResults=2500).execute()
    if page.get('nextPageToken') or not isinstance(page.get('items'),list):
        raise ValueError('Calendar overlap read incomplete')
    events = page['items']; ids = [e.get('id') for e in events]
    if any(not value for value in ids) or len(set(ids)) != len(ids): raise ValueError('Calendar overlap identities invalid')
    excluded = set()
    if own_id:
        own = [e for e in events if e['id'] == own_id]
        matches=[c for c in _read().values()if c.get('event_id')==own_id and not verified_release(c)]
        claim=matches[0]if len(matches)==1 else None
        if len(own) != 1 or not claim: raise ValueError('Own calendar overlap absent')
        _verify(own[0],claim); excluded.add(own_id)
    if room:
        matches = [e for e in events if e['id'] == ROOM_RESERVATION['event_id']]
        if len(matches) != 1: raise MailPolicyReviewRequired('Reviewed room reservation source absent')
        material = {k:v for k,v in matches[0].items() if k not in {'etag','updated'}}
        digest = hashlib.sha256(json.dumps(material,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if digest != ROOM_RESERVATION['material_sha256']:
            raise MailPolicyReviewRequired('Reviewed room reservation material changed')
        excluded.add(ROOM_RESERVATION['event_id'])
    opaque = [e for e in events if e.get('status') != 'cancelled' and e.get('transparency') != 'transparent']
    if _merged([_event_interval(e) for e in opaque],lower,upper) != _merged(intervals,lower,upper):
        raise ValueError('Calendar busy/event evidence changed or incomplete')
    remaining = _merged([_event_interval(e) for e in opaque if e['id'] not in excluded],lower,upper)
    if room:
        scope = _CALENDAR_SOURCE.get()
        key = lower.isoformat()+'|'+upper.isoformat()
        scope['checks'][key] = {'start':lower.isoformat(),'end':upper.isoformat(),'other_overlap':bool(remaining)}
    return remaining


def _check(calendar, dt, ids, existing_id=None, existing_calendar_id=PRIMARY):
    lower, upper = dt-timedelta(minutes=15), dt+timedelta(minutes=30)
    if existing_calendar_id not in ids:
        raise ValueError('Existing hold calendar not in verified availability scope')
    checked_ids = ids
    result = calendar.freebusy().query(body={'timeMin':lower.isoformat(), 'timeMax':upper.isoformat(), 'items':[{'id':i} for i in checked_ids]}).execute().get('calendars', {})
    for cid in checked_ids:
        if calendar_busy(calendar,cid,lower,upper,result.get(cid),
                         own_id=existing_id if cid == existing_calendar_id else None):
            raise ValueError('Slot conflicts with calendar')

def _verify(event, claim):
    if not event: raise ValueError('Hold readback absent; pending claim retained')
    props = event.get('extendedProperties', {}).get('private', {})
    valid = (event.get('id') == claim['event_id'] and start(event['start']['dateTime']) == start(claim['start'])
             and start(event['end']['dateTime']) == start(claim['start'])+timedelta(minutes=30)
             and event.get('visibility') == 'private' and event.get('transparency') in (None, 'opaque')
             and event.get('status') != 'cancelled' and not event.get('attendees')
             and owner_parts(props.get('hermesOwner')) == owner_parts(claim['owner']) and props.get('hermesSlotHold') == '1')
    if not valid: raise ValueError('Hold readback mismatched; pending claim retained')
    if claim.get('presentation') and any(event.get(k)!=v for k,v in claim['presentation'].items()):
        raise ValueError('Hold identity presentation readback mismatched; pending claim retained')

_LOCKED = ContextVar('managed_scheduling_locked', default=False)

@contextmanager
def _locked():
    if _LOCKED.get():
        yield
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE_DIR / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        token = _LOCKED.set(True)
        try: yield
        finally: _LOCKED.reset(token)

def owner_parts(value):
    """Compare native owner identity, never a serialization or display name."""
    value=json.loads(value)if isinstance(value,str)else value
    if (not isinstance(value,list)or len(value)!=3 or value[0]not in{'code','stealth'}
            or not isinstance(value[1],str)or not value[1]or not isinstance(value[2],list)
            or not value[2]or any(not isinstance(a,str)or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',a)for a in value[2])):
        raise MailPolicyReviewRequired('Native hold owner identity invalid')
    return value[0],value[1],tuple(sorted({a.casefold()for a in value[2]}))

def source_owner():
    source=_HOLD_SOURCE.get()
    if source and source['valid']and source.get('contact'):
        return source['account'],source['thread'],[source['contact']]
    return None

def source_hold_starts():
    """Local candidates only; availability still needs fresh event readback."""
    owner=source_owner()
    if not owner:return set()
    claims=_read();encoded=json.dumps(owner,separators=(',',':'));result=set()
    for claim in claims.values():
        if claim.get('state')!='held_draft_verified':continue
        dt=start(claim['start']);key=_owned_claim_key(claims,encoded,dt)
        if key in claims and claims[key].get('state')=='held_draft_verified':result.add(dt)
    return result

def _source_matches_owner(source,expected):
    return (source['valid']and (source['account'],source['thread'])==expected[:2]
            and (source.get('contact')in expected[2]or
                 bool(source.get('outgoing_targets'))and set(expected[2])<=source['outgoing_targets']))

def _source_targets_owned(source,original):
    targets=source.get('reply_targets')
    if targets is None:return True
    targets=targets-{source['mailbox']}
    return bool(targets)and targets<=set(original[2])

def _linked_hold_origin(source,expected,original):
    if (not _source_matches_owner(source,expected)or not _source_targets_owned(source,original)or not source.get('contact')
            or original[0]!=expected[0]or source['contact']not in original[2]):return False
    # The original SENT is fetched by exact RFC ID in the verified mailbox.
    # Its real historical Gmail thread must own this exact native claim.
    for message in linked_identity_sources(source['messages'])[1:]:
        targets={a.casefold()for _,a in getaddresses([message.get('to')or'',message.get('cc')or''])if a}
        if ('SENT'in message.get('labels',[])and message.get('thread_id')==original[1]
                and parseaddr(message.get('from')or'')[1].casefold()==source['mailbox']
                and source['contact']in targets):return True
    return False

def _owned_claim_key(claims, owner, dt):
    """Reuse exact identity or a verified native SENT lineage; preserve keys."""
    expected = owner_parts(owner)
    source=_HOLD_SOURCE.get()
    matches = []
    blocked_exact=False
    for key,claim in claims.items():
        if verified_release(claim): continue
        try: original=owner_parts(claim['owner']);same_time=start(claim['start'])==start(dt)
        except (ValueError,KeyError,TypeError): continue
        if not same_time:continue
        if original==expected:
            if source is not None and(not _source_matches_owner(source,expected)or not _source_targets_owned(source,original)):
                blocked_exact=True;continue
            matches.append(key)
        elif (source and claim.get('state')=='held_draft_verified'
              and _linked_hold_origin(source,expected,original)):matches.append(key)
    if len(matches) > 1: raise MailPolicyReviewRequired('Multiple exact-owner holds require reconciliation')
    if not matches and blocked_exact:raise MailPolicyReviewRequired('Current native source does not own this hold')
    return matches[0] if matches else hashlib.sha256((owner+'|'+dt.isoformat()).encode()).hexdigest()[:40]


def own_hold_available(calendar, account, thread, recipients, value):
    """Read-only exact-owner acceptance check; never creates or updates holds."""
    owner = json.dumps([account, thread, sorted(set(r.lower() for r in recipients))], separators=(',',':'))
    dt = start(value)
    with _locked():
        claims = _read()
        key = _owned_claim_key(claims,owner,dt)
        claim = claims.get(key)
        if claim is None:
            return None
        if claim.get('state')!='held_draft_verified':
            raise MailPolicyReviewRequired('Prior hold write is unresolved; inspect its native draft state')
        for other_key, other in claims.items():
            if other_key == key or verified_release(other): continue
            begin = start(other['start'])
            if dt-timedelta(minutes=15) < begin+timedelta(minutes=30) and dt+timedelta(minutes=30) > begin:
                raise ValueError('Slot conflicts with durable pending/owned claim')
        cid = claim_calendar(claim)
        _verify(_get(calendar,claim['event_id'],cid),claim)
        _check(calendar,dt,_calendars(calendar),claim['event_id'],cid)
        return True

def linked_identity_sources(messages):
    """Only actual RFC ancestors may supply another mailbox's firm evidence."""
    active=[message for message in messages if'DRAFT'not in message.get('labels',[])]
    if not active:return []
    indexed={message['rfc_message_id']:message for message in active if message.get('rfc_message_id')}
    if len(indexed)!=sum(bool(message.get('rfc_message_id'))for message in active):return [active[-1]]
    current=active[-1];result=[];seen=set()
    while current and current.get('id')not in seen and len(result)<100:
        result.append(current);seen.add(current.get('id'))
        parent=indexed.get(current.get('in_reply_to'))
        if parent is None:
            candidates=[indexed[value]for value in re.findall(r'<[^<>\s]+>',current.get('references')or'')if value in indexed]
            parent=candidates[-1]if candidates else None
        if parent is None:break
        try:
            if int(parent['internalDate'])>=int(current['internalDate']):break
        except (KeyError,TypeError,ValueError):break
        current=parent
    return result

def proposal_identity(recipients,messages,company=None):
    """Use native mail headers and literal source evidence, never invented names."""
    addresses=sorted(set(x.casefold() for x in recipients));names={}
    evidence=[];name_sources={}
    shared={'gmail.com','googlemail.com','outlook.com','hotmail.com','yahoo.com','icloud.com','gmx.de','gmx.net','web.de','primary.example.invalid'}
    domains={address.rsplit('@',1)[-1]for address in addresses if address.rsplit('@',1)[-1]not in shared}
    linked={message.get('id')for message in linked_identity_sources(messages)}
    for m in messages:
        for field in ('from','to','cc'):
            try:value=str(make_header(decode_header(m.get(field) or '')))
            except (LookupError,UnicodeError,ValueError):value=m.get(field) or ''
            for name,address in getaddresses([value]):
                if address.casefold() in addresses and name.strip():
                    names[address.casefold()]=name.strip();name_sources[address.casefold()]=m.get('id')
        sender=parseaddr(m.get('from') or '')[1].casefold()
        targets={a.casefold() for _,a in getaddresses([(m.get('to') or ''),(m.get('cc') or '')])}
        direct=sender in addresses or('SENT'in m.get('labels',[])and targets.intersection(addresses))
        firm_actor=sender.rsplit('@',1)[-1]in domains
        own_firm_source=('SENT'in m.get('labels',[])and sender in{PRIMARY,'operator@secondary.example.invalid'}
                         and any(address.rsplit('@',1)[-1]in domains for address in targets))
        if direct or(m.get('id')in linked and(firm_actor or own_firm_source)):
            evidence.append((m.get('id'),(m.get('from')or'')+' '+split_quote(m.get('text')or'')[0]))
    norm=lambda value:' '.join(value.casefold().split())
    matches=[mid for mid,text in evidence if isinstance(company,str)and len(company.strip())>=2 and norm(company)in norm(text)]
    verified=company.strip()if matches else None
    sources=list(dict.fromkeys([*name_sources.values(),*matches]))
    return {'names':names,'company':verified,'source_message_ids':[source for source in sources if source]}

def proposal_presentation(account,thread,recipients,project=None,identity=None):
    identity=identity or {};addresses=sorted(set(x.casefold() for x in recipients))
    def clean(value):return re.sub(r'[\x00-\x1f\x7f]+',' ',str(value)).strip()[:160]
    project_name={'acribic':'Acribic','stealth':'Stealth'}.get(project,'Stealth' if account=='stealth' else 'Projekt nicht belegt')
    names=identity.get('names') or {};labels=[];contacts=[];missing=[]
    if project is None and account!='stealth':missing.append('Projekt nicht belegt')
    for address in addresses:
        name=clean(names.get(address) or '')
        if ',' in name:
            last,first=name.split(',',1);name=first.strip()+' '+last.strip()
        labels.append(name or address);contacts.append((name+' <'+address+'>') if name else address)
        if not name:missing.append('Name für '+address+' nicht belegt')
    company=clean(identity.get('company') or '')
    if not company:missing.append('Unternehmen nicht belegt')
    title='['+project_name+'] Terminvorschlag - '+' / '.join(labels)+' - '+(company or 'Unternehmen nicht belegt')
    mailbox={'code':PRIMARY,'stealth':'operator@secondary.example.invalid'}[account]
    description=('Privater Terminvorschlag für den Antwortentwurf, noch kein bestätigter Termin.\n'
                 'Projekt: '+project_name+'\nKontakt: '+'; '.join(contacts)+'\nUnternehmen: '+(company or 'nicht belegt')+
                 '\nMailthread: '+thread+'\nQuelle: https://mail.google.com/mail/u/?authuser='+quote(mailbox,safe='')+'#all/'+quote(thread,safe=''))
    if identity.get('source_message_ids'):description+='\nQuellnachrichten: '+', '.join(clean(x) for x in identity['source_message_ids'])
    if missing:description+='\nFehlende Angaben: '+'; '.join(missing)
    return {'summary':title,'description':description}

@contextmanager
def scheduling_batch(calendar, owners):
    if not owners:
        yield
        return
    with _locked(), ExitStack() as stack:
        for account, thread, recipients, slots in owners:
            stack.enter_context(scheduling(calendar, account, thread, recipients, slots, allow_unverified=True))
        yield

@contextmanager
def scheduling(calendar, account, thread, recipients, slots, *, allow_unverified=False, project=None, identity=None, source_messages=None,primary_recipients=None):
    if not slots:
        yield []
        return
    calendar_id_for(account, project=project)  # Preserve verified account/project grants.
    target = PRIMARY  # New private proposals always belong to the CODE main calendar.
    owner = json.dumps([account, thread, sorted(set(r.lower() for r in recipients))], separators=(',',':'))
    if not thread or not recipients: raise ValueError('Scheduling requires thread and recipients')
    with ExitStack() as stack:
        stack.enter_context(CalendarSource(account,thread,source_messages or [],primary_recipients=primary_recipients).activate())
        stack.enter_context(_locked())
        claims = _read()
        ids = _calendars(calendar)
        selected = []
        for value in slots:
            dt = start(value)
            key = _owned_claim_key(claims,owner,dt)
            for other_key, c in claims.items():
                if verified_release(c): continue
                begin = start(c['start'])
                if other_key != key and dt-timedelta(minutes=15) < begin+timedelta(minutes=30) and dt+timedelta(minutes=30) > begin:
                    raise ValueError('Slot conflicts with durable pending/owned claim')
            if key in claims and claims[key]['state'] in ('held_pending_draft', 'held_draft_unverified') and not allow_unverified:
                raise ValueError('Prior draft write may exist; inspect draft state before any new create')
            if key in claims and claims[key].get('project') not in (None, project):
                raise ValueError('Existing hold belongs to another project; no calendar migration')
            claim = claims.get(key) or {'owner':owner, 'start':dt.isoformat(), 'event_id':key, 'state':'intent',
                                        'calendar_id':target, 'project':project,
                                        'presentation':proposal_presentation(account,thread,recipients,project,identity)}
            cid = claim_calendar(claim)
            existing = _get(calendar, claim['event_id'], cid)
            if existing:
                _verify(existing, claim)
                _check(calendar, dt, ids, claim['event_id'], cid)
            else:
                if key in claims:
                    raise ValueError('Prior pending/held event absent; manual reconciliation required, no automatic reinsertion')
                _check(calendar, dt, ids)
                claims[key] = claim
                _save(claims) # Durable intent BEFORE the first external write.
                body = {'id':key, **claim['presentation'], 'start':{'dateTime':dt.isoformat()},
                        'end':{'dateTime':(dt+timedelta(minutes=30)).isoformat()}, 'visibility':'private', 'transparency':'opaque',
                        'extendedProperties':{'private':{'hermesSlotHold':'1','hermesOwner':owner}}}
                try: calendar.events().insert(calendarId=cid, body=body, sendUpdates='none').execute()
                except Exception:
                    # Exactly one insert. Uncertain outcome recovered ONLY by exact ID.
                    existing = _get(calendar, key, cid)
                else: existing = _get(calendar, key, cid)
                _verify(existing, claim)
            claim['state'] = 'held_pending_draft'
            claims[key] = claim; _save(claims)
            selected.append(key)
        try:
            yield selected
        except BaseException:
            claims = _read()
            for key in selected: claims[key]['state'] = 'held_draft_unverified'
            _save(claims)
            raise
        else:
            claims = _read()
            for key in selected: claims[key]['state'] = 'held_draft_verified'
            _save(claims)
