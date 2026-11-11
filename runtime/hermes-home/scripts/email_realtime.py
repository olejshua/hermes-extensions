#!/usr/bin/env python3
"""Zero-LLM eight-second Gmail poll; only important saved drafts trigger Telegram.

Never sends email. The only Gmail mutation is drafts.create via email_watcher.make_draft.
"""
import json
import argparse
import configparser
import fcntl
import hashlib
import importlib
import stat
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from html import unescape
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

import email_watcher as w

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mail_workflow import MailWorkflowError, prepare
from mail_model_status import failure_from_stderr

HOME = w.HOME
DATA = HOME / 'email_watcher'
FILE = DATA / 'realtime.json'
CHAT = 'telegram:5644072286'
HERMES = '/home/hermes/.local/bin/hermes'
INTERVAL = 8
WORKFLOW_VERSION = 'mail-workflow-20261104-semantic-write-v4'
MODEL_PYTHON = HOME / 'tools/python-3.14.7+20260901-linux-x64/bin/python3.14'
# These are deliberately closed conversations, not a blanket contact blacklist.
CLOSED_THREADS = {'code': {'e000000000000001', 'e000000000000002',
                           'e000000000000003', 'e000000000000004'}}
REFERRAL = re.compile(r'empfehl|ansprechpartner|in cc|weiterleit|introduc|recommend|copied|cc.d', re.I)
BERLIN = ZoneInfo('Europe/Berlin')
QUOTE_HEADER = re.compile(r'(?ims)^\s*(?:>|Von:|From:|-----Ursprüngliche Nachricht-----|-----Original Message-----|Am\b.{0,240}?\bschrieb\s*:|On\b.{0,240}?\bwrote\s*:)', re.I)

class SchedulingReviewRequired(ValueError):
    """Do not create a draft when an incoming appointment cannot be verified."""
    def __init__(self, message, code='scheduling_review'):
        super().__init__(message)
        self.code = code


def unquoted_sender_text(text):
    """Only authored prose: quoted history and signature hours are not offers."""
    own = QUOTE_HEADER.split(text, maxsplit=1)[0]
    return re.split(r'(?im)^\s*(?:Beste Grüße|Viele Grüße|Liebe Grüße|Herzliche Grüße|'
                    r'Mit (?:freundlichen|besten) Grüßen|Kind regards|Best regards|'
                    r'Sent (?:from|via)\b|--\s*$)(?:[^\n]*)', own, maxsplit=1)[0].strip()


def state():
    return json.loads(FILE.read_text()) if FILE.exists() else {'seq': 0, 'alerted': [], 'queue': [], 'notifications': []}


def save(s):
    w._atomic_json(FILE, s)
    sync_file(FILE)


def sync_file(path):
    with open(path, 'rb') as source:
        os.fsync(source.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def case_path(item):
    if item.get('account') not in ('code', 'stealth') or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', item.get('id', '')):
        raise ValueError('invalid_mail_identity')
    return DATA / 'cases' / (item['account'] + '-' + item['id'] + '.json')


def read_case(item):
    path = case_path(item)
    value = json.loads(path.read_text()) if path.exists() else {}
    if value and (value.get('account'), value.get('message_id'), value.get('thread_id')) != (
            item['account'], item['id'], item['threadId']):
        raise ValueError('mail_case_identity_changed')
    return value


def remember_case(item, **changes):
    path = case_path(item)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    value = read_case(item)
    value.update(contract=WORKFLOW_VERSION, account=item['account'], message_id=item['id'],
                 thread_id=item['threadId'], updated_at=time.time(), **changes)
    w._atomic_json(path, value)
    sync_file(path)
    return value


def model_json(phase, context):
    packet = json.dumps({'phase': phase, 'context': context}, ensure_ascii=False)
    worker = Path(__file__).resolve().with_name('mail_model_worker.py')
    result = subprocess.run([str(MODEL_PYTHON), str(worker)], input=packet,
                            text=True, capture_output=True, timeout=180)
    if result.returncode:
        failure = failure_from_stderr(result.stderr)
        record_model_failure(phase,context,result,failure=failure)
        raise failure
    if len(result.stdout) > 25000:
        record_model_failure(phase,context,result)
        raise MailWorkflowError('model_output_size')
    try:
        answer = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        record_model_failure(phase,context,result)
        raise MailWorkflowError('model_output_format') from error
    if not isinstance(answer, dict):
        record_model_failure(phase,context,result)
        raise MailWorkflowError('model_output_format')
    return answer


def record_model_failure(phase,context,result,*,failure=None):
    # Only model data and an integer exit code, never SDK stderr/auth headers.
    item={'account':context['account'],'id':context['current_message'],
          'threadId':context['thread'][-1]['thread_id']}
    path=case_path(item).with_name(item['account']+'-'+item['id']+'.'+phase+'-failure.json')
    safe_failure = ({'code':failure.code,'availability':failure.availability,'stage':failure.stage}
                    if failure else None)
    w._atomic_json(path,{'phase':phase,'context':context,'output':result.stdout,'returncode':result.returncode,
                         'failure':safe_failure})
    sync_file(path)


def notify(text):
    result = subprocess.run([HERMES, 'send', '--to', CHAT, '--json', '--file', '-'],
                            input=text, text=True, capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError('telegram_delivery_unconfirmed')
    receipt = json.loads(result.stdout)
    if receipt.get('success') is not True or receipt.get('skipped') or not receipt.get('message_id'):
        raise RuntimeError('telegram_delivery_unconfirmed')
    return {'message_id': str(receipt['message_id'])}


def safe(value, limit=150):
    return re.sub(r'[\x00-\x1f\x7f]+', ' ', str(value or ''))[:limit]


def referral_addressing(cc_header, requested, sender_header):
    """Only address explicitly selected contacts already copied on the incoming mail."""
    allowed = {addr.lower() for _, addr in getaddresses([cc_header or '']) if '@' in addr}
    sender = parseaddr(sender_header or '')[1].lower()
    if not isinstance(requested, list) or not requested or len(requested) > 5:
        raise ValueError('Referral recipients must be a nonempty list of at most five CC contacts')
    selected = []
    for item in requested:
        if not isinstance(item, str):
            raise ValueError('Referral recipient is not an email address')
        addr = item.strip().lower()
        if addr not in allowed or addr == sender or addr in selected:
            raise ValueError('Referral recipient is not a unique copied contact')
        selected.append(addr)
    if not sender or '@' not in sender:
        raise ValueError('Introducer address is missing')
    return selected, [sender]


def get_thread(alias, tid):
    svc = w.service(alias)
    expected = {'code': 'operator@primary.example.invalid', 'stealth': 'operator@secondary.example.invalid'}[alias]
    if w.verified_profile(svc).casefold() != expected:
        raise ValueError('mailbox_identity_mismatch')
    raw = svc.users().threads().get(userId='me', id=tid, format='full').execute()
    messages = w.complete_thread_sources(alias,raw.get('messages', []),svc)
    compact = []
    for msg in messages:
        h = w.headers(msg)
        text = w.body(msg.get('payload', {}))
        compact.append({'id': msg['id'], 'thread_id': msg.get('threadId')or tid, 'labels': msg.get('labelIds', []),
                        'reference_source':bool(msg.get('hermes_reference_source')),
                        'from': h.get('from'), 'to': h.get('to'), 'cc': h.get('cc'),
                        'internalDate': msg.get('internalDate'),
                        'subject': h.get('subject'), 'rfc_message_id': h.get('message-id'),
                        'in_reply_to': h.get('in-reply-to'), 'references': h.get('references'),
                        'text': text, 'truncated': False})
    return compact, False


def excluded_platform_mail(sender):
    """Luma event mail is outside the personal reply/appointment workflow."""
    address = parseaddr(sender or '')[1].casefold()
    domain = address.rpartition('@')[2]
    return domain == 'luma-mail.com' or domain.endswith('.luma-mail.com')


def calendar_service():
    """CODE calendar uses the same account-bound locked OAuth writer as Gmail."""
    return w.build('calendar', 'v3', credentials=w.credentials_for_account('code'), cache_discovery=False)


def check_incoming_availability(start, owner=None):
    """One native exact check, including the source-bound room decision."""
    import mail_policy as policy
    svc = calendar_service()
    owner = owner or policy.source_owner()
    try:
        if owner and policy.own_hold_available(svc, *owner, start) is True:
            return True
        policy._check(svc, start, policy._calendars(svc))
    except policy.MailPolicyReviewRequired:
        raise
    except ValueError as error:
        if str(error).startswith('Slot conflicts with '): return False
        raise SchedulingReviewRequired('Incoming slot could not be verified in every required calendar') from error
    if any(start-timedelta(minutes=15) < end and start+timedelta(minutes=30) > begin
           for begin,end in policy.pending_busy()): return False
    return True


def calendar_options(now=None, window=None, availability=None):
    """Pre-verified 15-minute starts (15-minute buffers), never event titles."""
    now = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    from mail_workflow import timestamp
    bounds = availability or {}
    earliest = timestamp(bounds['not_before']) if bounds.get('not_before') else now
    latest = timestamp(bounds['not_after']) if bounds.get('not_after') else None
    excluded = [(timestamp(value['start']), timestamp(value['end']))
                for value in bounds.get('excluded', [])]
    first = max(window[0] if window else now, earliest, now)
    end = window[1] if window else first + timedelta(days=15)
    if latest is not None:
        end = min(end, latest)
    if end <= first:
        return []
    begin = first-timedelta(minutes=15)
    svc = calendar_service()
    from mail_policy import _calendars
    ids = _calendars(svc)
    result = svc.freebusy().query(body={
        'timeMin': begin.isoformat(), 'timeMax': (end+timedelta(minutes=15)).isoformat(), 'timeZone': 'Europe/Berlin',
        'items': [{'id': cid} for cid in ids],
    }).execute().get('calendars', {})
    from mail_policy import pending_busy,source_owner,source_hold_starts,own_hold_available,start as hold_start
    pending = pending_busy()
    hold_owner=source_owner();own_starts=source_hold_starts()if hold_owner else set()
    busy = list(pending)
    for cid in ids:
        if cid not in result or result[cid].get('errors') or not isinstance(result[cid].get('busy'), list):
            raise RuntimeError('Calendar freebusy lookup failed')
        busy.extend((datetime.fromisoformat(b['start']).astimezone(BERLIN),
                     datetime.fromisoformat(b['end']).astimezone(BERLIN)) for b in result[cid]['busy'])
    weekdays = ('Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag')
    options = []
    for days in range((end.date()-first.date()).days+1):
        day = (first + timedelta(days=days)).date()
        if day.weekday() >= 5 and not window:
            continue
        for starts in (range(0, 24*60, 15) if window else (12*60, 13*60, 14*60, 15*60, 16*60),):
            added = 0
            for stamp in starts:
                hour, minute = divmod(stamp, 60)
                start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=BERLIN)
                if start.utcoffset()!=start.replace(fold=1).utcoffset():
                    continue
                finish = start + timedelta(minutes=15)
                if start < now + timedelta(minutes=45) or finish > end:
                    continue
                if start < first or (latest is not None and start+timedelta(minutes=30) > latest):
                    continue
                if any(start < stop and start+timedelta(minutes=30) > begin for begin, stop in excluded):
                    continue
                pending_overlap=any(start-timedelta(minutes=15) < stop and start+timedelta(minutes=30) > begin
                                    for begin,stop in pending)
                busy_overlap=any(start-timedelta(minutes=15) < stop and
                                 finish+timedelta(minutes=15) > begin for begin,stop in busy)
                reused=False
                if hold_start(start)in own_starts:
                    from mail_policy import MailPolicyReviewRequired
                    try:reused=own_hold_available(svc,*hold_owner,start)is True
                    except MailPolicyReviewRequired:raise
                    except ValueError as error:
                        if not str(error).startswith('Slot conflicts with '):raise
                if pending_overlap and not reused:continue
                if busy_overlap and not reused:
                    from mail_policy import room_range_allowed, _check, MailPolicyReviewRequired
                    if not room_range_allowed(start-timedelta(minutes=15),start+timedelta(minutes=30)): continue
                    try: _check(svc,start,ids)
                    except MailPolicyReviewRequired: raise
                    except ValueError as error:
                        if str(error).startswith('Slot conflicts with '): continue
                        raise
                if options and start - datetime.fromisoformat(options[-1]['start']) < timedelta(minutes=45):
                    continue
                options.append({'start': start.isoformat(),
                                'label': f'{weekdays[day.weekday()]}, {day:%d.%m.%Y}, {hour:02d}:{minute:02d} Uhr'})
                added += 1
                if added == 3:
                    break
    return options


def _reply_context(alias, row):
    """Load only the private reviewed release selected by the observer policy.

    Bootstrap checks precede importing executable package code. The package's
    own reviewed()/read_policy() then enforce the complete dependency binding.
    """
    policy = HOME / 'ole-reply-observer' / 'reply-observer.ini'
    if alias != 'code' or row is None or not policy.is_file():
        return None
    def private(path):
        path = Path(path)
        info = path.lstat()
        if (not path.is_absolute() or not stat.S_ISREG(info.st_mode) or
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise SchedulingReviewRequired('Reply policy requires private review')
        return path
    cfgfile = private(policy)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(cfgfile.read_text())
    cfg = parser['reply_observer']
    if not cfg.getboolean('model_reuse_enabled', fallback=False):
        return None
    if (cfg.get('parent_review') != 'approved' or
            cfg.get('account') != 'operator@primary.example.invalid' or cfg.get('category') != 'acribic'):
        raise SchedulingReviewRequired('Reply activation binding requires review')
    root = Path(cfg['package_root'])
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise SchedulingReviewRequired('Reply package path requires review')
    root = root.resolve()
    raw = private(cfg['review_manifest']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != cfg['review_manifest_sha256']:
        raise SchedulingReviewRequired('Reply manifest changed')
    manifest = json.loads(raw)
    if manifest.get('contract') != 'ole-acribic-reply-observer-review-v1' or manifest.get('revision') != 'R2':
        raise SchedulingReviewRequired('Reply review contract changed')
    reviewed_paths = set()
    for record in manifest['runtime_files']:
        relative = Path(record['path'])
        source = root / relative
        if (relative.is_absolute() or '..' in relative.parts or source.is_symlink() or
                not source.is_file() or not source.resolve().is_relative_to(root) or
                hashlib.sha256(source.read_bytes()).hexdigest() != record['sha256']):
            raise SchedulingReviewRequired('Reply runtime source changed')
        reviewed_paths.add(source.resolve())
    if root / 'gtm/mail_observer/integration.py' not in reviewed_paths:
        raise SchedulingReviewRequired('Reply integration is not reviewed')
    for name, module in list(sys.modules.items()):
        if (name == 'gtm' or name.startswith('gtm.') or name == 'hermes_second_brain'
                or name.startswith('hermes_second_brain.')):
            origin = getattr(module, '__file__', None)
            namespace_root = (root / name.replace('.', '/') if name.startswith('gtm')
                              else root / 'second-brain/src' / name.replace('.', '/'))
            namespace_paths = [Path(p).resolve() for p in getattr(module, '__path__', [])]
            if origin:
                valid = Path(origin).resolve() in reviewed_paths
            else:
                valid = (namespace_paths == [namespace_root] and
                         not (namespace_root / '__init__.py').exists())
            if not valid:
                raise SchedulingReviewRequired('Reply import binding changed')
    sys.path[:0] = [str(root), str(root / 'second-brain/src')]
    try:
        module = importlib.import_module('gtm.mail_observer.integration')
        if Path(module.__file__).resolve() != root / 'gtm/mail_observer/integration.py':
            raise SchedulingReviewRequired('Reply integration import changed')
        bridge = module.integration_from_config(policy)
        try:
            data = bridge.before(w.service('code'), row) if bridge else None
        except ValueError as exc:
            # An unrelated sender is outside this Acribic hook; retain its
            # ordinary draft path. Ambiguous/malformed qualified sources fail closed.
            if str(exc) in ('Unique exact existing person-to-company match absent',
                            'Unique exact existing source mailbox-to-company match absent',
                            'newsletter_or_autoreply_ignored', 'outreach_direction_review'):
                return None
            raise
        return (bridge, data) if data else None
    finally:
        del sys.path[:2]


def generate(alias, mid, thread, older_omitted, now=None, scheduling_owner=None,
             reply_row=None, reply_context=None, cached=None, cached_plan=None, journal=None,
             calendar_options_reader=None, cached_standard=None):
    now = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    # Preserve complete decoded originals, including inline/bottom replies and
    # postscripts. Authorship is interpreted, never guessed by cutting a body.
    compact = [dict(message) for message in thread]
    if older_omitted or any(message.get('truncated') for message in thread):
        raise MailWorkflowError('incomplete_source')
    if len(json.dumps(compact, ensure_ascii=False)) > 300000:
        raise MailWorkflowError('thread_size_review')
    from mail_policy import project_from_thread
    from mail_business import research_context, SOURCE_ID, THREAD_ID
    try:project = project_from_thread(alias, compact)
    except ValueError as error:
        if getattr(error,'code',None)=='mail_business_project_review':
            raise MailWorkflowError('business_project_unverified')from error
        raise
    def business():
        def fetch():
            sources, omitted = get_thread('code', THREAD_ID)
            if omitted: raise ValueError('acribic_research_original_incomplete')
            matches = [m for m in sources if m['id'] == SOURCE_ID]
            if len(matches) != 1: raise ValueError('acribic_research_original_missing')
            return matches[0]
        return research_context(fetch, compact)
    from mail_outcomes import assess, OutcomeReviewError
    facts = {}
    attempts = []
    feedback = None
    def remember(**values):
        facts.update(values)
        if journal: journal(**values)
    def model(phase, context):
        if feedback is not None:
            context = {**context, 'outcome_feedback': feedback,
                       'instruction': 'Korrigiere die konkret belegte Lücke aus outcome_feedback. Nutze alle Originale; keine Fakten erfinden.'}
        return model_json(phase, context)
    from mail_policy import CalendarSource
    tid = scheduling_owner[1] if scheduling_owner else compact[-1].get('thread_id')
    calendar_source = CalendarSource(alias,tid,compact)
    exact_reader = calendar_source.wrap(check_incoming_availability)
    options_reader = calendar_source.wrap(calendar_options_reader or calendar_options)
    if cached and cached.get('invitation_requested') and 'invitation_recipients' not in cached: cached=None
    for attempt in range(2):
        answer = prepare(alias, mid, compact, now=now, authored=compact[-1]['text'], model=model,
                       exact=exact_reader, options=options_reader, calendar_evidence=calendar_source.evidence,
                       owner=scheduling_owner, cached=cached if attempt == 0 else None,
                       cached_plan=cached_plan if attempt == 0 else None, remember=remember,
                       project=project, business_context=business if project == 'acribic' else None,
                       reply_context=(lambda: reply_context) if reply_context else
                           ((lambda: _reply_context(alias, reply_row)) if reply_row else None))
        received = datetime.fromtimestamp(int(compact[-1]['internalDate'])/1000, BERLIN) if compact[-1].get('internalDate') else now
        from mail_policy import format_body
        reviewed_body=format_body(reply_signature(compact,answer['body']),answer.get('proposed_slots',[]))if answer.get('body')else None
        context = {'account': alias, 'project': project, 'current_message': mid, 'thread': compact,'review_phase':'candidate',
                   'received_at': received.isoformat(), 'processing_at': now.isoformat(),
                   'draft': reviewed_body, 'user_question': answer.get('question'),'require_time_claims':True,
                   'business_context': facts.get('business_context'),
                   'hard_facts': {'calendar': facts.get('calendar'), 'saved_in_gmail': False,
                                  'proposed_slots': answer.get('proposed_slots', [])},
                   'candidate_understanding': facts.get('understanding')}
        remember(phase='result_review')
        try: receipt = assess(context, model_json, cached=cached_standard if attempt==0 else None)
        except OutcomeReviewError as error:
            remember(standard_attempts=attempts, standard_review_error=error.code,
                     standard_review_failure={'context':context,'rejected':getattr(error,'rejected',[]),
                                              'input_key':getattr(error,'input_key',None)})
            raise MailWorkflowError(error.code) from error
        verdict = receipt['review']; attempts.append({'receipt': receipt, 'candidate': answer})
        remember(standard_attempts=attempts, result_standard=receipt, standard_context=context)
        accepted = (verdict['result'] == 'ready' or
                    verdict['result'] == 'not_required' and not answer.get('body') and not answer.get('question') or
                    verdict['result'] == 'needs_user' and bool(answer.get('question')) and not answer.get('body'))
        if accepted:
            remember(phase='prepared', standard_verified=True)
            return {**answer, 'standard_verified': True}
        feedback = {'review': verdict, 'previous_candidate': answer}
    # A purposeful correction is allowed once. A persistent disagreement becomes
    # a durable repair case; no unusable draft has been written to Gmail.
    remember(standard_verified=False)
    raise MailWorkflowError('reply_standard_failed')


def reply_signature(thread, body):
    incoming = thread[-1]['text'][:700].split('-----Ursprüngliche Nachricht-----')[0]
    incoming = re.split(r'(?m)^\s*(?:Von|From):', incoming)[0]
    informal = bool(re.search(r'\b(?:Hallo|Lieber|Hi)\s+Alex\b|\b(?:du|dir|dich|dein(?:e|en|em|es)?|euch|euer(?:e|en|em|es)?)\b',
                              incoming[:500], re.I))
    formal = bool(re.search(r'\bHerr(?:n)?\s+Beispiel\b|\b[Ss]ehr geehrt|\b(?:Sie|Ihnen|Ihre|Ihren|Ihrem)\b',
                            incoming[:500])) or bool(re.search(r'\b(?:Herr|Frau)\s+[A-ZÄÖÜ]|\b(?:Sie|Ihnen|Ihre|Ihren|Ihrem)\b', body[:600]))
    name = 'Alex' if informal and not formal else 'Alex Beispiel'
    clean = re.sub(r'(?is)(?:^|\n+)(?:Beste Grüße|Mit besten Grüßen|Mit freundlichen Grüßen|Liebe Grüße|Viele Grüße|Herzliche Grüße|VG)\s*'
                   r'(?:Alex(?: Beispiel)?\s*)*$', '', body.strip())
    return clean.rstrip() + '\n\nBeste Grüße\n' + name

def draft_one(item, *, retry_readonly=False):
    # The automatic worker and an explicit Telegram request can target the same
    # incoming mail. Serialize that case across processes before reading its state.
    path = case_path(item).with_suffix('.lock')
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _draft_one(item, retry_readonly=retry_readonly)


def closed_source_lineage(account,thread_id,thread=()):
    """Exact verified ancestors retain deliberate closure; new unlinked mail does not."""
    closed=CLOSED_THREADS.get(account,set())
    return thread_id in closed or any(message.get('reference_source')is True
        and message.get('thread_id')in closed and'SENT'in message.get('labels',[])
        and'DRAFT'not in message.get('labels',[])for message in thread)

def _draft_one(item, *, retry_readonly=False):
    alias, mid, tid = item['account'], item['id'], item['threadId']
    key = alias + ':' + mid
    def finish(outcome, phase='completed'):
        outcome['case_key'] = key
        remember_case(item, phase=phase, outcome=outcome)
        return outcome
    if tid in CLOSED_THREADS.get(alias, set()):
        return finish({'status': 'deliberately_closed', 'priority': 'normal'})
    if excluded_platform_mail(item.get('from')):
        return finish({'status': 'out_of_scope_platform_mail', 'priority': 'normal'})
    case = read_case(item)
    safe_retry = (retry_readonly and case.get('phase') == 'review_required'
                  and case.get('error_code') in {'calendar_unavailable', 'no_verified_free_slot', 'RuntimeError', 'TimeoutExpired',
                                               'requested_multiple_options_missing'}
                  and not case.get('prepared_body'))
    if safe_retry:
        remember_case(item, phase='retry_pending', outcome=None, error_code=None)
    if not safe_retry and case.get('phase') in {'completed', 'awaiting_approval', 'review_required'} and case.get('outcome'):
        return case['outcome']
    if case.get('phase') in {'draft_write_pending', 'draft_update_pending'}:
        # The old process may have died after the request reached Gmail. A
        # missing local success is not permission to write another draft.
        return finish({'status': 'needs_manual_review_draft_state', 'priority': 'normal'}, 'review_required')
    thread, omitted = get_thread(alias, tid)
    if closed_source_lineage(alias,tid,thread):
        return finish({'status':'deliberately_closed','priority':'normal'})
    if not thread or thread[-1]['id'] != mid or 'SENT' in thread[-1].get('labels', []):
        return finish({'status': 'not_worthy_or_already_handled', 'priority': 'normal'})
    if excluded_platform_mail(thread[-1].get('from')):
        return finish({'status': 'out_of_scope_platform_mail', 'priority': 'normal'})
    # Manual Telegram calls supply identities, not trusted sender/header claims.
    item = {**item, **{key: thread[-1].get(key) for key in ('from', 'to', 'cc', 'subject', 'internalDate')}}
    mailbox = {'code': 'operator@primary.example.invalid', 'stealth': 'operator@secondary.example.invalid'}[alias]
    direct = {a.casefold() for _, a in getaddresses([thread[-1].get('to') or ''])}
    copied = {a.casefold() for _, a in getaddresses([thread[-1].get('cc') or ''])}
    own = unquoted_sender_text(thread[-1]['text'])
    addressed = re.search(r'\b(?:Hallo|Hi|Lieber|Liebe|Guten Tag)\s+(?:Alex|Herr(?:n)? Beispiel)\b|\bOle[,?!:]', own, re.I)
    if mailbox in copied and mailbox not in direct and not addressed:
        return finish({'status': 'cc_information', 'priority': 'normal'})
    if any('DRAFT' in entry.get('labels', []) for entry in thread):
        return finish({'status': 'existing_draft_review', 'priority': 'normal'}, 'review_required')
    source_hash = hashlib.sha256(json.dumps(thread, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cached = case.get('understanding') if (case.get('source_hash') == source_hash and case.get('contract') == WORKFLOW_VERSION) else None
    remember_case(item, phase='source_loaded', source_hash=source_hash, original_thread=thread)
    sender = parseaddr(thread[-1].get('from', ''))[1]
    owner = (alias, tid, [sender]) if sender and not thread[-1].get('cc') else None
    answer = generate(alias, mid, thread, omitted, scheduling_owner=owner, reply_row=item,
                      cached=cached, cached_plan=case.get('draft_plan') if cached else None,
                      cached_standard=case.get('result_standard')if cached else None,
                      journal=lambda **values: remember_case(item, **values))
    if answer.get('ignored'):
        return finish({**answer, 'status': 'not_worthy_or_already_handled'})
    if answer.get('information_only') or (answer['body'] is None and not answer.get('question')):
        return finish({**answer, 'status': 'important_information' if answer.get('notify') else 'no_draft_needed'})
    if answer['body'] is None:
        return finish({**answer, 'status': 'needs_user_input'}, 'awaiting_approval')
    body = reply_signature(thread, answer['body'])
    to, cc = None, None
    if answer.get('referral_recipients'):
        to, cc = referral_addressing(thread[-1].get('cc'), answer['referral_recipients'], thread[-1].get('from'))
    write_owner=uuid.uuid4().hex
    remember_case(item, phase='draft_write_pending', write_owner=write_owner,prepared_body=body,
                  prepared_slots=answer.get('proposed_slots', []), recipients=to or [sender],
                  copied=cc or [])
    try:
        kwargs = {'write_owner':write_owner,**({'slots': answer['proposed_slots']} if answer.get('proposed_slots') else {})}
        if answer.get('project'):
            kwargs['project'] = answer['project']
        if kwargs.get('slots') and answer.get('company'):
            kwargs['company'] = answer['company']
        if to is not None:
            kwargs.update(to=to, cc=cc)
        result = w.make_draft(alias, mid, body, **kwargs)
    except w.SchedulingDraftReviewRequired as error:
        code = getattr(error, 'code', 'slot_reservation_unavailable')
        remember_case(item, error_code=code, draft_failure={
            'contract': 'mail-prewrite-failure-v1', 'gmail_create_started': False,
            'code': code, 'stage': getattr(error, 'stage', 'reservation'),
            'cause_type': getattr(error, 'cause_type', None)})
        return finish({**answer, 'status': 'workflow_blocked', 'review_reason': code}, 'review_required')
    except w.DraftStateUnverified:
        return finish({**answer, 'status': 'needs_manual_review_draft_state'}, 'review_required')
    except getattr(w, 'SourceChangedBeforeDraft', type('UnavailableSourceChange', (Exception,), {})):
        return finish({'status': 'superseded_by_new_reply', 'priority': 'normal'})
    # Unknown exceptions retain draft_write_pending. The queue cannot replay it.
    outcome = {**answer, 'body': None, 'status': result['status'],
               'to': result.get('to'), 'draft_id': result.get('draftId'),
               'draft_message_id': result.get('draft_message_id'), 'body_sha256': result.get('body_sha256'),
               'appointment_start': (answer.get('proposed_slots') or [None])[0]}
    if result['status'] == 'created' and answer.get('invitation_approval_required'):
        remember_case(item, pending_decision={'action': 'create_invitation', 'authorized': False,
                      'start': outcome['appointment_start'], 'recipients': answer.get('invitation_recipients') or to or [sender],
                      'source_evidence':answer.get('invitation_recipient_evidence',[]),
                      'request_source':{'account':alias,'id':mid,'threadId':tid,
                          'rfc_message_id':thread[-1].get('rfc_message_id'),'text':thread[-1]['text']},
                      'requires_fresh_original_read':True})
        return finish(outcome, 'awaiting_approval')
    return finish(outcome)


def poll_event():
    p = subprocess.run([sys.executable, str(HOME / 'scripts' / 'email_watcher.py'), 'poll'],
                       capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise RuntimeError(f'Gmail poll failed: {p.stderr[-500:]}')
    return json.loads(p.stdout)


def tick(s):
    event = poll_event()
    if update_account_health(s, event.get('errors', {})):
        save(s)
    if event['seq'] <= s['seq']:
        return False
    # Persist each queued message before processing, suppressing duplicates after crashes.
    for m in event['messages']:
        key = m['account'] + ':' + m['id']
        if key not in s['alerted']:
            s['alerted'].append(key)
            s['alerted'] = s['alerted'][-2000:]
            s['queue'].append({'message': m, 'attempts': 0, 'after': 0})
            save(s)
    s['seq'] = event['seq']
    save(s)
    return True


def notice_text(value, limit=160):
    """Private facts only: never copy raw snippets, addresses or decoration."""
    if not isinstance(value, str):
        return ''
    value = unescape(value)
    value = re.sub(r'\b[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}\b|https?://\S+', '', value)
    value = re.sub(r'[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f\u200d`*_<>]', '', value)
    value = re.sub(r'\s+', ' ', value).strip(' ,;:-')
    if len(value) > limit:
        value = value[:limit].rsplit(' ', 1)[0].rstrip(' ,;:-') + '…'
    return value


def notice_party(message, outcome):
    display = parseaddr(message.get('from', ''))[0]
    try:
        name = str(make_header(decode_header(display))) if display else ''
    except (LookupError, UnicodeError, ValueError):
        name = ''
    name = notice_text(name, 65)
    if ',' in name:
        last, first = name.split(',', 1); name = first.strip()+' '+last.strip()
    if not name or '@' in name:
        name = 'Ein Kontakt'
    company = notice_text(outcome.get('company'), 40)
    return name + (' ('+company+')' if company and company.casefold() not in name.casefold() else '')


def notice_summary(outcome, party):
    summary = notice_text(outcome.get('notification_summary'), 120).rstrip(' .')
    name = party.split(' (', 1)[0]
    for prefix in (party, name, notice_text(outcome.get('company'), 40)):
        if prefix:
            summary = re.sub(r'^' + re.escape(prefix) + r'(?=\s|[:,;-]|$)\s*[:,;-]?\s*', '', summary, flags=re.I)
    return summary


def verified_repair_assignment(item):
    """Read the existing exact source/job binding; never create or promise work."""
    import sqlite3
    case=read_case(item);code=case.get('error_code')
    if not code:return False
    source_key=hashlib.sha256(json.dumps(['mail',item['account'],item['id'],case.get('source_hash'),code],
        sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    path=HOME/'ole-repair/state.sqlite3'
    if not path.is_file()or path.is_symlink():return False
    try:
        with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=1)as database:
            row=database.execute('''SELECT j.state,j.attempts,j.max_attempts FROM repair_sources s
                JOIN repair_incidents i USING(signature)JOIN lifecycle_jobs j USING(job_id)
                WHERE s.source_key=? AND i.component='mail' AND i.code=?''',(source_key,code)).fetchone()
        return bool(row and(row[0]=='running'or row[0]=='queued'and row[1]<row[2]))
    except (OSError,sqlite3.Error):return False

def queue_draft_notification(s, m, outcome):
    """A source-bound outbox: useful result/decision, never a copy of the mail."""
    status = outcome.get('status')
    party = notice_party(m, outcome)
    important = outcome.get('notify') is True or outcome.get('priority') == 'high'
    if status == 'important_information':
        if not important:
            return
        summary = notice_summary(outcome, party)
        if not summary:
            return
        message = party + ' ' + summary + '.'
    elif status == 'created':
        if not important and not outcome.get('appointment'):
            return
        if outcome.get('crm_review_required'):
            message = 'Der Antwortentwurf für ' + party + ' ist gespeichert. Die CRM-Zuordnung muss ich noch prüfen.'
        elif outcome.get('invitation_approval_required') and outcome.get('appointment_start'):
            stamp = datetime.fromisoformat(outcome['appointment_start']).astimezone(BERLIN)
            label = f'{stamp:%d.%m.} um {stamp:%H:%M} Uhr'
            message = 'Der Entwurf für ' + party + ' zum Termin am ' + label + ' ist bereit. Soll ich die Einladung erstellen?'
        else:
            summary = notice_summary(outcome, party)
            message = (party + ' ' + summary + '. Der Entwurf ist bereit.' if summary
                       else 'Der Antwortentwurf für ' + party + ' ist bereit.')
    elif status == 'needs_user_input':
        question = notice_text(outcome.get('question'), 160)
        if not question:
            return
        message = party + ': ' + question
    elif status == 'needs_manual_review_draft_state':
        message = 'Ich konnte den gespeicherten Entwurf für ' + party + ' noch nicht bestätigen. Der Fall bleibt zur Prüfung erhalten.'
    elif status == 'workflow_blocked':
        if not important and not outcome.get('appointment'):
            return
        if verified_repair_assignment(m):return
        summary=notice_summary(outcome,party)
        message=(party+' '+summary+'. 'if summary else'Für '+party+' ')
        message+=('Der geprüfte Antwortentwurf fehlt noch.'if summary else'fehlt noch ein geprüfter Antwortentwurf.')
    else:
        return
    key = outcome.get('case_key')
    if key:
        cases = s.setdefault('notification_cases', {})
        if key in cases:
            return
        cases[key] = 'queued'
        s.setdefault('notifications', []).append({'case_key': key, 'message': {
            'account': m['account'], 'id': m['id'], 'threadId': m['threadId']},
            'text': message, 'state': 'pending'})
    else:
        s.setdefault('notifications', []).append(message)


def flush_notifications(s):
    """Persist the send boundary; an uncertain Telegram send is never replayed."""
    queue = s.setdefault('notifications', [])
    while queue:
        item = queue[0]
        if isinstance(item, str):
            item = {'text': item, 'state': 'pending'}
            queue[0] = item
        if item.get('state') in {'sending', 'uncertain'}:
            item['state'] = 'uncertain'
            s.setdefault('delivery_reviews', []).append(item)
            if item.get('case_key'):
                s.setdefault('notification_cases', {})[item['case_key']] = 'uncertain'
                remember_case(item['message'], delivery={'state': 'uncertain', 'attempted_at': item.get('attempted_at')})
            queue.pop(0)
            save(s)
            continue
        item.update(state='sending', attempted_at=time.time())
        save(s)
        try:
            receipt = notify(item['text'])
        except Exception:
            item['state'] = 'uncertain'
            save(s)
            continue
        item['state'] = 'accepted'
        if item.get('case_key'):
            s.setdefault('notification_cases', {})[item['case_key']] = 'accepted'
            remember_case(item['message'], delivery={'state': 'accepted', 'accepted_at': time.time(),
                                                   'receipt': receipt})
        queue.pop(0)
        save(s)


def processing_failed(s, active, error):
    item = active['message']
    case = read_case(item)
    generation=getattr(error,'write_generation',None)
    newer=generation is not None and case.get('write_generation',0)!=generation
    saved=((case.get('outcome')or{}).get('status')=='created'
           and(case.get('write_commit')or{}).get('state')in{'verified','completed'})
    if newer or saved:
        # A late failed reader must not turn another writer's result into a
        # missing reply or emit a misleading failure notice.
        if saved and not newer and case.get('phase')not in{'completed','awaiting_approval'}:
            approval=case.get('pending_decision')or{}
            remember_case(item,phase='awaiting_approval'if approval and not approval.get('authorized')else'completed',
                last_processing_failure={'code':getattr(error,'code',None)or type(error).__name__})
        s['queue']=[row for row in s.get('queue',[])if row is not active]
        save(s);return
    active['attempts'] += 1
    code = getattr(error, 'code', None) or type(error).__name__
    # Before a draft write, only transport/model availability failures may retry.
    provider_wait = code in {'mail_model_quota','mail_model_auth','mail_model_unavailable','mail_model_output_invalid','mail_model_runtime_failed'}
    if provider_wait:
        remember_case(item, model_failure={'code':code,'availability':getattr(error,'availability',None)})
    retryable = (not provider_wait and ((isinstance(error, (RuntimeError, subprocess.TimeoutExpired, ConnectionError))
                  and not isinstance(error, MailWorkflowError)) or code == 'calendar_unavailable'))
    uncertain = case.get('phase') in {'draft_write_pending', 'draft_update_pending'}
    if not uncertain and retryable and active['attempts'] < 2:
        active['after'] = time.time() + 300
        remember_case(item, phase='retry_pending', error_code=code)
    else:
        understanding = case.get('understanding') or {}
        outcome = {'status': 'needs_manual_review_draft_state' if uncertain else 'workflow_blocked',
                   'priority': 'normal', 'case_key': item['account'] + ':' + item['id'],
                   'notify': understanding.get('notify', False), 'company': understanding.get('company'),
                   'appointment': understanding.get('scheduling', 'none') != 'none', 'review_reason': code}
        remember_case(item, phase='review_required', error_code=code, outcome=outcome)
        s.setdefault('open_reviews', {})[outcome['case_key']] = {'reason': code, 'message': item}
        queue_draft_notification(s, item, outcome)
        s['queue'] = [row for row in s['queue'] if row is not active]
    save(s)
    print('mail processing failed: ' + str(code), file=sys.stderr, flush=True)


def reconcile_open_reviews(s):
    """A verified manual/repair result closes its old index entry on the next poll."""
    resolved=[]
    for key,review in list(s.get('open_reviews',{}).items()):
        try:
            case=read_case(review['message']);outcome=case.get('outcome') or {}
        except (OSError,ValueError,KeyError):continue
        if (case.get('phase') in {'completed','awaiting_approval'} and
                outcome.get('status') in {'created','not_worthy_or_already_handled','deliberately_closed'}):
            resolved.append(key)
    for key in resolved:s['open_reviews'].pop(key)
    return bool(resolved)


def update_account_health(s, errors):
    """One alert per continuous per-mailbox outage; one recovery on reconnection."""
    counts = s.setdefault('account_failure_counts', {})
    reported = set(s.get('reported_account_outages', []))
    before = (dict(counts), set(reported), len(s.setdefault('notifications', [])))
    for alias in w.ACCOUNTS:
        if alias in errors:
            counts[alias] = min(3, counts.get(alias, 0) + 1)
            if counts[alias] == 3 and alias not in reported:
                name = 'Stealth' if alias == 'stealth' else 'CODE'
                if errors[alias] == 'authorization_expired':
                    message = f'Der Zugriff auf das {name}-Postfach ist abgelaufen. Bitte erneut verbinden.'
                else:
                    message = f'Das {name}-Postfach kann ich gerade nicht abrufen.'
                s['notifications'].append(message)
                reported.add(alias)
        else:
            counts.pop(alias, None)
            if alias in reported:
                name = 'Stealth' if alias == 'stealth' else 'CODE'
                s['notifications'].append(f'Der Zugriff auf das {name}-Postfach funktioniert wieder.')
                reported.remove(alias)
    s['reported_account_outages'] = sorted(reported)
    return before != (counts, reported, len(s['notifications']))


def update_poll_health(s, failed):
    """Avoid periodic re-alerts for the same process-level poll failure."""
    before = (s.get('poll_failure_count', 0), s.get('poll_outage_reported', False),
              len(s.setdefault('notifications', [])))
    if failed:
        s['poll_failure_count'] = min(3, before[0] + 1)
        if s['poll_failure_count'] == 3 and not before[1]:
            s['notifications'].append('Ich kann gerade keine neuen Mails abrufen. Bitte vorübergehend selbst nachsehen.')
            s['poll_outage_reported'] = True
    else:
        s['poll_failure_count'] = 0
        if before[1]:
            s['notifications'].append('Der Mailabruf funktioniert wieder.')
            s['poll_outage_reported'] = False
    return before != (s['poll_failure_count'], s.get('poll_outage_reported', False), len(s['notifications']))


def log_draft_result(message, result):
    """Keep the sender's mail text out of service logs."""
    print(f"draft {safe(message.get('account'), 20)}:{safe(message.get('id'), 50)}: "
          f"{safe(result.get('status'), 50)}, priority={safe(result.get('priority'), 10)}, reason={safe(result.get('review_reason'), 40)}",
          flush=True)


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    s = state()
    # Existing monitor baseline may have emitted historical mail; never replay it on migration.
    current = json.loads(w.STATE.read_text())['event']['seq']
    if not FILE.exists():
        s['seq'] = current
        save(s)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future, active = None, None
        while True:
            start = time.monotonic()
            if future and future.done():
                try:
                    result = future.result()
                    queue_draft_notification(s, active['message'], result)
                    save(s)
                    s['queue'] = [i for i in s['queue'] if i['message']['id'] != active['message']['id'] or i['message']['account'] != active['message']['account']]
                    save(s)
                    log_draft_result(active['message'], result)
                except Exception as exc:
                    processing_failed(s, active, exc)
                future, active = None, None
            try:
                if reconcile_open_reviews(s):save(s)
                flush_notifications(s)
            except Exception as exc:
                print(f'draft notification failed: {exc}', file=sys.stderr, flush=True)
            try:
                tick(s)
                if update_poll_health(s, failed=False):
                    save(s)
            except Exception as exc:
                print(f'poll failed: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
                if update_poll_health(s, failed=True):
                    save(s)
            if future is None:
                due = next((i for i in s['queue'] if i['after'] <= time.time()), None)
                if due:
                    active = due
                    future = pool.submit(draft_one, due['message'])
            time.sleep(max(0.5, INTERVAL - (time.monotonic() - start)))


def cli(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return main()
    parser = argparse.ArgumentParser(description='Managed incoming-mail reply preparation; never sends mail.')
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('prepare')
    command.add_argument('--account', choices=['code', 'stealth'], required=True)
    command.add_argument('--message-id', required=True)
    command.add_argument('--thread-id', required=True)
    availability = commands.add_parser('availability', help='Read-only exact calendar starts; no model or writes.')
    availability.add_argument('--starts-json', required=True)
    args = parser.parse_args(argv)
    if args.command == 'availability':
        from calendar_lookup import check
        from mail_policy import _calendars, pending_busy
        result = check(json.loads(args.starts_json), service=calendar_service,
                       calendar_ids=_calendars, pending=pending_busy, clock=lambda:datetime.now(BERLIN))
    else:
        result = draft_one({'account': args.account, 'id': args.message_id, 'threadId': args.thread_id}, retry_readonly=True)
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    cli()
