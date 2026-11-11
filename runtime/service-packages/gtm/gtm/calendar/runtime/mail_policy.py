"""Managed draft boundary: deterministic mail text and durable scheduling claims.
All managed scheduling writers must hold scheduling() across Gmail write/readback.
Manual Gmail edits and other calendar clients cannot participate in this lock.
"""
import fcntl
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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

def format_body(text, slots=(), *, force_formal=False):
    # Quoted history is retained verbatim, including its original dash characters.
    own, quoted = split_quote(text)
    own = own.replace('\r\n', '\n').replace('\r', '\n').strip()
    own = ''.join('-' if unicodedata.category(c) == 'Pd' or c in '\u2212\u2043' else c for c in own)
    slot_values = [start(s).isoformat() for s in slots]
    if len(set(slot_values)) != len(slot_values):
        raise MailPolicyReviewRequired('Duplicate slot metadata')
    remainder = own
    for value in slot_values:
        expected = label(value)
        if remainder.count(expected) != 1:
            raise MailPolicyReviewRequired('Proposed date/time must match exact verified start label: ' + expected)
        remainder = remainder.replace(expected, '')
    if CLOCK.search(remainder) or (PROPOSAL.search(remainder) and (DATE.search(remainder) or RELATIVE.search(remainder))):
        raise MailPolicyReviewRequired('Unverified or ambiguous proposed date/time; supply --slot and exact start label')
    match = CLOSING.search(own)
    existing_full = bool(match and 'Alex Beispiel' in match.group())
    formal = bool(re.search(r'\b(?:[Ss]ehr geehrt\w*|Herrn?|Frau|Sie|Ihnen|Ihre[nmrs]?)\b', own))
    informal = bool(re.search(r'\b(?:du|dir|dich|dein\w*|euch|euer\w*)\b', own, re.I))
    existing_short = bool(match and re.search(r'\bOle\b', match.group()))
    name = 'Alex Beispiel' if force_formal or formal or existing_full else 'Alex' if informal or existing_short else 'Alex Beispiel'
    clean = own[:match.start()].rstrip() if match else own
    result = clean + '\n\nBeste Grüße\n' + name
    return result + ('\n\n' + quoted if quoted else '')

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
        slot=start(claim['start']).astimezone(timezone.utc).isoformat()
        key=('code|'+owner[1]+'|'+slot if claim.get('kind')=='legacy_slot_hold' else claim['owner']+'|'+slot)
        expected=hashlib.sha256(key.encode()).hexdigest()[:40]
    except (ValueError,KeyError,TypeError,IndexError):return False
    if claim.get('kind')=='legacy_slot_hold' and (not claim.get('legacy_source_message') or
        not isinstance(claim.get('legacy_source_hash'),str) or not re.fullmatch(r'[a-f0-9]{64}',claim['legacy_source_hash'])):return False
    return (claim.get('state')=='released_after_verified_invitation' and
        proof.get('attempted') is True and proof.get('verified') is True and
        proof.get('account')==PRIMARY and proof.get('hold_id')==claim.get('event_id')==expected and
        isinstance(owner,list) and len(owner)==3 and owner[0]=='code' and bool(owner[1]) and
        owner[2]==[proof.get('recipient')] and
        isinstance(proof.get('source_hash'),str) and bool(re.fullmatch(r'[a-f0-9]{64}',proof['source_hash'])) and
        isinstance(proof.get('invitation_event_id'),str) and bool(re.fullmatch(r'_?[a-v0-9]{5,1024}',proof['invitation_event_id'])))

def pending_busy():
    # Atomic journal reads. All intents remain busy, including uncertain/orphan writes.
    return [(start(c['start']).astimezone(BERLIN), start(c['start']).astimezone(BERLIN)+timedelta(minutes=30)) for c in _read().values() if not verified_release(c)]

def _get(calendar, event_id):
    try:
        return calendar.events().get(calendarId=PRIMARY, eventId=event_id).execute()
    except Exception as e:
        if getattr(getattr(e, 'resp', None), 'status', None) == 404: return None
        raise

def _calendars(calendar):
    items, seen_tokens, token = [], set(), None
    for page in range(20):
        request = {'pageToken': token} if token else {}
        result = calendar.calendarList().list(**request).execute()
        if not isinstance(result.get('items'), list):
            raise ValueError('Calendar identity list incomplete; scheduling review required')
        items.extend(result['items'])
        token = result.get('nextPageToken')
        if not token:
            break
        if not isinstance(token, str) or token in seen_tokens:
            raise ValueError('Calendar identity pagination invalid; scheduling review required')
        seen_tokens.add(token)
    else:
        raise ValueError('Calendar identity pagination limit; scheduling review required')
    fuxam = [c['id'] for c in items if c.get('summary', '').casefold() == 'fuxam calendar']
    if not any(c.get('id') == PRIMARY for c in items) or len(fuxam) != 1:
        raise ValueError('Both CODE calendars must be available')
    return [PRIMARY, fuxam[0]]

def _check(calendar, dt, ids, existing_id=None):
    lower, upper = dt-timedelta(minutes=15), dt+timedelta(minutes=30)
    checked_ids = ids if existing_id is None else ids[1:]
    result = calendar.freebusy().query(body={'timeMin':lower.isoformat(), 'timeMax':upper.isoformat(), 'items':[{'id':i} for i in checked_ids]}).execute().get('calendars', {})
    for cid in checked_ids:
        if cid not in result or result[cid].get('errors') or not isinstance(result[cid].get('busy'), list):
            raise ValueError('Calendar availability could not be verified')
        if result[cid]['busy']: raise ValueError('Slot conflicts with calendar')
    if existing_id:
        # Freebusy cannot distinguish our own event from another overlapping event.
        result = calendar.events().list(calendarId=PRIMARY, timeMin=lower.isoformat(), timeMax=upper.isoformat(), singleEvents=True, maxResults=2500).execute()
        if result.get('nextPageToken') or 'items' not in result:
            raise ValueError('Primary calendar overlap read incomplete')
        if any(e.get('id') != existing_id and e.get('status') != 'cancelled' and e.get('transparency') != 'transparent' for e in result['items']):
            raise ValueError('Slot conflicts with another primary event')

def _verify(event, claim):
    if not event: raise ValueError('Hold readback absent; pending claim retained')
    props = event.get('extendedProperties', {}).get('private', {})
    valid = (event.get('id') == claim['event_id'] and start(event['start']['dateTime']) == start(claim['start'])
             and start(event['end']['dateTime']) == start(claim['start'])+timedelta(minutes=30)
             and event.get('visibility') == 'private' and event.get('transparency') == 'opaque'
             and event.get('status') != 'cancelled' and not event.get('attendees')
             and props.get('hermesOwner') == claim['owner'] and props.get('hermesSlotHold') == '1')
    if not valid: raise ValueError('Hold readback mismatched; pending claim retained')

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

def own_hold_available(calendar, account, thread, recipients, value):
    """Read-only exact-owner acceptance check; never creates or updates holds."""
    owner = json.dumps([account, thread, sorted(set(r.lower() for r in recipients))], separators=(',',':'))
    dt = start(value)
    key = hashlib.sha256((owner+'|'+dt.isoformat()).encode()).hexdigest()[:40]
    with _locked():
        claim = _read().get(key)
        if claim is None:
            return None
        _verify(_get(calendar,key),claim)
        _check(calendar,dt,_calendars(calendar),key)
        return True

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
def scheduling(calendar, account, thread, recipients, slots, *, allow_unverified=False):
    if not slots:
        yield []
        return
    owner = json.dumps([account, thread, sorted(set(r.lower() for r in recipients))], separators=(',',':'))
    if not thread or not recipients: raise ValueError('Scheduling requires thread and recipients')
    with _locked():
        claims = _read()
        ids = _calendars(calendar)
        selected = []
        for value in slots:
            dt = start(value)
            key = hashlib.sha256((owner+'|'+dt.isoformat()).encode()).hexdigest()[:40]
            for other_key, c in claims.items():
                if verified_release(c): continue
                begin = start(c['start'])
                if other_key != key and dt-timedelta(minutes=15) < begin+timedelta(minutes=30) and dt+timedelta(minutes=30) > begin:
                    raise ValueError('Slot conflicts with durable pending/owned claim')
            if key in claims and claims[key]['state'] in ('held_pending_draft', 'held_draft_unverified') and not allow_unverified:
                raise ValueError('Prior draft write may exist; inspect draft state before any new create')
            claim = claims.get(key) or {'owner':owner, 'start':dt.isoformat(), 'event_id':key, 'state':'intent'}
            existing = _get(calendar, key)
            if existing:
                _verify(existing, claim)
                _check(calendar, dt, ids, key)
            else:
                if key in claims:
                    raise ValueError('Prior pending/held event absent; manual reconciliation required, no automatic reinsertion')
                _check(calendar, dt, ids)
                claims[key] = claim
                _save(claims) # Durable intent BEFORE the first external write.
                body = {'id':key, 'summary':'Terminvorschlag - '+label(dt), 'start':{'dateTime':dt.isoformat()},
                        'end':{'dateTime':(dt+timedelta(minutes=30)).isoformat()}, 'visibility':'private', 'transparency':'opaque',
                        'description':'Reservierter Terminvorschlag, noch kein bestätigter Termin.',
                        'extendedProperties':{'private':{'hermesSlotHold':'1','hermesOwner':owner}}}
                try: calendar.events().insert(calendarId=PRIMARY, body=body, sendUpdates='none').execute()
                except Exception:
                    # Exactly one insert. Uncertain outcome recovered ONLY by exact ID.
                    existing = _get(calendar, key)
                else: existing = _get(calendar, key)
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
