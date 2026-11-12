#!/usr/bin/env python3
"""Explicit, fail-closed sender for prepared Acribic batches; never schedules.

Default is read-only preflight. --send requires human authorization for this run.
--expected must match the exact prepared mail count. Any shortfall sends zero.
Uses the journal schema of versand_abschluss_sicher. Legacy batches without
batch_id are rejected. Shared host reservation is still required for Mac/VPS.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from email.utils import getaddresses
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import versand_abschluss_sicher as close
from versand_betreff import validate_subject

STOP = close.UnsafeCompletion


def decode_cli(value):
    """Handle explicit Composio output spill pointers; reject ambiguous responses."""
    for _ in range(3):
        if not isinstance(value, dict):
            raise STOP('Composio response is not an object')
        if 'successful' in value and value['successful'] is not True:
            raise STOP('Composio explicitly rejected response')
        pointer = value.get('storedInFile')
        if pointer:
            if isinstance(pointer, str):
                path = pointer
            elif isinstance(pointer, dict):
                path = pointer.get('outputFilePath') or pointer.get('filePath') or pointer.get('path')
            elif pointer is True:
                path = value.get('outputFilePath') or value.get('filePath') or value.get('path')
            else:
                path = None
            if not isinstance(path, str) or not Path(path).is_file() or Path(path).stat().st_size > 20_000_000:
                raise STOP('Invalid Composio storedInFile pointer')
            value = json.loads(Path(path).read_text(encoding='utf-8'))
            continue
        if value.get('successful') is not True:
            raise STOP('Composio did not explicitly confirm successful response')
        data = value.get('data')
        if not isinstance(data, dict):
            raise STOP('Composio response data is missing')
        if data.get('storedInFile'):
            # The already verified envelope may spill only its data payload.
            pointer = data['storedInFile']
            path = pointer if isinstance(pointer, str) else (pointer.get('outputFilePath') or pointer.get('filePath') or pointer.get('path')) if isinstance(pointer, dict) else data.get('outputFilePath') or data.get('filePath') or data.get('path')
            if not isinstance(path, str) or not Path(path).is_file() or Path(path).stat().st_size > 20_000_000:
                raise STOP('Invalid Composio data spill pointer')
            loaded = json.loads(Path(path).read_text(encoding='utf-8'))
            value = loaded if isinstance(loaded, dict) and 'successful' in loaded else {'successful': True, 'data': loaded}
            continue
        return data
    raise STOP('Recursive Composio storedInFile response')


class Gmail:
    def __init__(self, account):
        if not account:
            raise STOP('Explicit Gmail account is required')
        self.account = account

    def __call__(self, slug, data):
        # Never retry this subprocess: a timeout during SEND leaves an uncertain attempt.
        result = subprocess.run(['composio', 'execute', slug, '--account', self.account,
                                 '-d', json.dumps(data, ensure_ascii=False)],
                                capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        if result.returncode:
            raise STOP(f'{slug}: CLI failed; no retry')
        try:
            return decode_cli(json.loads(result.stdout))
        except (ValueError, OSError) as exc:
            raise STOP(f'{slug}: invalid response; no retry') from exc


def fetch(gmail, query, limit=1):
    result = gmail('GMAIL_FETCH_EMAILS', dict(query=query, max_results=limit,
                      verbose=False, include_payload=False, include_spam_trash=True))
    if 'messages' not in result or not isinstance(result['messages'], list):
        raise STOP('Gmail history response is missing messages; absence is not proven')
    return result


def fetch_queries(gmail, queries, limit=1):
    """Independent read-only history checks; all complete before any write."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(lambda query: fetch(gmail, query, limit), queries))


def single_address(value):
    if not isinstance(value, str):
        raise STOP('Missing Gmail address header')
    addresses = getaddresses([value])
    if len(addresses) != 1:
        raise STOP('Gmail header is not exactly one mailbox')
    return close.mailbox(addresses[0][1])


def message_id(value):
    candidates = [value[k] for k in ('messageId', 'id') if value.get(k)]
    if not candidates or any(not isinstance(c, str) for c in candidates) or len(set(candidates)) != 1:
        raise STOP('Missing or ambiguous Gmail message ID')
    return candidates[0]


def verify_sent(message, wanted_id, mail=None):
    if message_id(message) != wanted_id or single_address(message.get('sender')) != close.SENDER:
        raise STOP('Sent readback ID/sender mismatch')
    if not isinstance(message.get('labelIds'), list) or 'SENT' not in message['labelIds']:
        raise STOP('Sent readback lacks SENT label')
    if mail is not None and (single_address(message.get('to')) != close.mailbox(mail['to'])
                             or message.get('subject') != mail['subject']):
        raise STOP('Sent readback recipient/subject mismatch')


def profile_and_control(gmail, known_id, known_query):
    if single_address(gmail('GMAIL_GET_PROFILE', {}).get('emailAddress')) != close.SENDER:
        raise STOP('Wrong Gmail account')
    if not known_id or not known_query or 'in:sent' not in known_query:
        raise STOP('Explicit known Sent ID and in:sent control query required')
    result = fetch(gmail, known_query, 100)
    matches = [m for m in result['messages'] if message_id(m) == known_id]
    if len(matches) != 1:
        raise STOP('Known Sent positive control missing/duplicate; no send')
    verify_sent(matches[0], known_id)


def no_history(gmail, mail, v):
    email = close.mailbox(mail['to']); domain = v.basis(v.dom(email))
    company = mail['firma']
    if any(c in company for c in ('"', '\n', '\r')):
        raise STOP('Company cannot safely form an exact Gmail query')
    queries = [f'in:anywhere (to:{email} OR from:{email})',
                  f'in:anywhere (to:{domain} OR from:{domain})',
                  f'in:anywhere "{company}"',
                  f'in:drafts to:{email}',
                  f'in:scheduled to:{email}']
    if mail.get('vorname'):
        import re
        parts = re.split(r'[._]', email.split('@')[0])
        if len(parts) == 2 and all(re.fullmatch(r'[a-zäöüß-]{2,}', p) for p in parts):
            # Conservative person-name search also catches a prior conversation
            # under a different employer/domain. A hit is reviewed, never ignored.
            queries.append(f'in:anywhere "{parts[0]} {parts[1]}"')
    for response in fetch_queries(gmail, queries):
        if response['messages'] or response.get('nextPageToken') or response.get('resultSizeEstimate', 0):
            raise STOP(f'Gmail prior history/draft for {email}')


def validate_prepared(v, mails, batch, expected, smtp_status, today):
    if isinstance(batch, dict) and batch.get('crm_backend', 'notion') != getattr(v, 'BACKEND', 'notion'):
        raise STOP('Prepared batch belongs to a different CRM backend')
    if type(expected) is not int or expected <= 0 or not isinstance(mails, list) or len(mails) != expected:
        raise STOP('Prepared mail count differs from requested N; zero sends')
    if (not isinstance(batch, dict) or batch.get('liste') != v.LISTE or batch.get('datum') != today
            or not isinstance(batch.get('batch_id'), str) or not batch['batch_id'].strip()):
        raise STOP('Batch date/list/unique batch_id missing or wrong')
    if getattr(v, 'BACKEND', 'notion') == 'twenty':
        exit_proof = batch.get('smtp_exit') or {}
        ip = exit_proof.get('ip') or ''
        if exit_proof.get('verified') is not True or not (ip.startswith('83.135.') or ip == '45.89.127.191'):
            raise STOP('Twenty batch lacks a proven SMTP exit')
    candidates = {}
    for candidate in batch['kandidaten']:
        key = (close.page_id(candidate['page_id']), close.mailbox(candidate['email']))
        if key in candidates:
            raise STOP('Duplicate batch candidate')
        candidates[key] = candidate
    seen_email, seen_page, seen_domain, seen_company = set(), set(), set(), set()
    for mail in mails:
        exact = close.content(mail)
        domain = v.basis(v.dom(exact['to'])); company = v.firma_key(mail.get('firma') or '')
        if not domain or not company:
            raise STOP('Missing firm identity')
        for key, seen in ((exact['to'], seen_email), (exact['page_id'], seen_page), (domain, seen_domain), (company, seen_company)):
            if key in seen: raise STOP('Duplicate mailbox/page/domain/company in prepared mails')
            seen.add(key)
        smtp = candidates.get((exact['page_id'], exact['to']))
        if (not smtp or smtp.get('smtp') != 'OK' or smtp.get('smtp_probe', True)
                or smtp.get('smtp_geprueft_am') != today
                or smtp_status(smtp.get('smtp_antwort', ''), smtp.get('smtp_zufall', '')) != 'OK'):
            raise STOP('SMTP real/random-reject confirmation missing or stale')
        if (not isinstance(mail.get('ted_publication'), str) or not mail['ted_publication'].strip()
                or not isinstance(mail.get('ted_buyer'), str) or not mail['ted_buyer'].strip()
                or mail.get('flags') != [] or mail['subject'] in ('Ihre Zuschläge', 'Eure Zuschläge')
                or 'an der CODE University' not in mail['body']
                or 'vorname' not in mail or type(mail.get('gekuerzt')) is not bool):
            raise STOP('TED enrichment/content review missing')
        try:
            validate_subject(mail)
        except ValueError as exc:
            raise STOP(str(exc)) from exc


def assemble_crm_snapshot(v, groups):
    if any(not isinstance(group, list) for group in groups.values()):
        raise STOP('Incomplete seven-list CRM snapshot')
    v._ANDERE = [r for name, rows in groups.items() if name != v.LISTE for r in rows]
    blocked, contacted = v.gesperrte_domains(groups[v.LISTE])
    protected = {v.basis(d) for d in blocked | contacted if d}
    all_rows = [r for rows in groups.values() for r in rows]
    return groups, all_rows, protected


def crm_snapshot(v):
    if getattr(v, 'TWENTY', None):
        return assemble_crm_snapshot(v, v.TWENTY.refresh(complete=True))
    databases = {**v.LISTEN, **v.SCHUTZ_LISTEN}
    if len(databases) != 7 or v.LISTE not in v.LISTEN:
        raise STOP('Expected seven-list protection unavailable')
    groups = {}
    for name, db in databases.items():
        print(f'Full CRM list: {name}', file=sys.stderr, flush=True)
        groups[name] = v.hole(db)
    return assemble_crm_snapshot(v, groups)


def crm_changed_rows(v, db, anchor):
    """Read every page edited since an anchor predating the full snapshot.

    Notion's timestamp filter covers new pages and edits. An archived old page
    remains in the baseline as a conservative block; the selected page gets a
    separate live GET before every send.
    """
    rows, cursor, seen = [], None, set()
    while True:
        body = {'page_size': 100, 'filter': {'timestamp': 'last_edited_time',
                'last_edited_time': {'on_or_after': anchor}}}
        if cursor: body['start_cursor'] = cursor
        response = v.api('POST', f'/databases/{db}/query', body)
        if (not isinstance(response, dict) or not isinstance(response.get('results'), list)
                or type(response.get('has_more')) is not bool):
            raise STOP('Incomplete Notion CRM change response')
        for page in response['results']:
            if not isinstance(page, dict) or not isinstance(page.get('id'), str) or not isinstance(page.get('properties'), dict):
                raise STOP('Invalid Notion CRM changed page')
            if page.get('archived') or page.get('in_trash'):
                raise STOP('Archived CRM page in change response')
            rows.append({'id': page['id'].replace('-', ''),
                         **{key: v.txt(value) for key, value in page['properties'].items()}})
        if not response.get('has_more'):
            if response.get('next_cursor'):
                raise STOP('Notion CRM change pagination inconsistent')
            break
        cursor = response.get('next_cursor')
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise STOP('Notion CRM change pagination incomplete/repeated')
        seen.add(cursor)
    return rows


def crm_refresh(v, prior, anchor):
    """Update a complete seven-list baseline with live, fully paginated deltas."""
    if getattr(v, 'TWENTY', None):
        return assemble_crm_snapshot(v, v.TWENTY.refresh())
    databases = {**v.LISTEN, **v.SCHUTZ_LISTEN}
    if len(databases) != 7 or set(prior[0]) != set(databases):
        raise STOP('CRM baseline/list map changed')
    groups = {}
    for name, db in databases.items():
        by_id = {row['id']: row for row in prior[0][name]}
        if len(by_id) != len(prior[0][name]):
            raise STOP('Duplicate CRM page ID in baseline')
        for row in crm_changed_rows(v, db, anchor):
            by_id[row['id']] = row
        groups[name] = list(by_id.values())
    return assemble_crm_snapshot(v, groups)


def validate_live(v, mail, snapshot):
    groups, all_rows, protected = snapshot
    email = close.mailbox(mail['to']); domain = v.basis(v.dom(email))
    candidates = [r for r in groups[v.LISTE] if close.page_id(r['id']) == close.page_id(mail['page_id'])]
    same_mailbox = [r for r in all_rows if (r.get('E-Mail') or '').strip().lower() == email]
    if len(candidates) != 1 or len(same_mailbox) != 1:
        raise STOP('Exact mailbox/page is not unique across all seven lists')
    row = candidates[0]; company = v.firma_key(mail['firma'])
    foreign = [r for name, rows in groups.items() if name != v.LISTE for r in rows]
    occupied_names = {v.firma_key(r.get('Firma') or '') for r in foreign + [r for r in all_rows if v.crm_gesperrt(r)]}
    website = v.basis(v.dom(row.get('Website') or ''))
    family = v.FAMILIE.search(mail['firma'])
    nexus_exception = mail['firma'].lower().startswith('nexus institut für kooperationsmanagement') and domain == 'nexusinstitut.de'
    if (row.get('Stage') != 'Nicht kontaktiert' or (row.get('E-Mail') or '').strip().lower() != email
            or (row.get('Firma') or '').strip() != mail['firma'] or v.crm_gesperrt(row)
            or domain in protected or (website and website in protected) or company in occupied_names
            or v.dom(email) in v.FREEMAIL or v.TECH.match(email.split('@')[0]) or v.PLATZHALTER.search(email)
            or (family and not nexus_exception) or v.NOTIZ_STOP.search((row.get('Notizen') or '').split(' | ')[0])):
        raise STOP('Live CRM/domain/family/suppression blocks mail')
    # Re-read selected page after the complete list snapshot.
    page = v.api('GET', '/pages/' + mail['page_id'])
    if page.get('archived') or page.get('in_trash'): raise STOP('Selected page archived/deleted')
    props = page['properties']
    live = {k: v.txt(val) for k, val in props.items()}
    if (live.get('Stage') != 'Nicht kontaktiert' or (live.get('E-Mail') or '').strip().lower() != email
            or (live.get('Firma') or '').strip() != mail['firma'] or v.crm_gesperrt(live)):
        raise STOP('Selected page changed during preflight')


def append_event(path, event):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        handle.flush(); os.fsync(handle.fileno())
    directory = os.open(Path(path).parent, os.O_RDONLY)
    try: os.fsync(directory)
    finally: os.close(directory)


def readback(gmail, mail, ack_id, pause=time.sleep):
    for attempt in range(4):
        result = fetch(gmail, f'in:sent to:{close.mailbox(mail["to"])} newer_than:1d', 100)
        matches = [m for m in result['messages'] if message_id(m) == ack_id]
        if len(matches) > 1: raise STOP('Duplicate sent readback ID')
        if matches:
            verify_sent(matches[0], ack_id, mail)
            return matches[0]
        if attempt < 3: pause(2)
    raise STOP('Sent readback missing; attempted send remains uncertain; never retry')


def run(v, gmail, expected, journal, known_id, known_query, smtp_status, send=False, pause=time.sleep,
        authorized_drafts=None):
    authorized_drafts = authorized_drafts or {}
    if authorized_drafts:
        import versand_bestandsentwuerfe_sicher as existing
    def live(mail, snapshot):
        return existing.validate_live(v, mail, snapshot, authorized_drafts) if authorized_drafts else validate_live(v, mail, snapshot)
    def history(mail):
        return existing.no_history(gmail, mail, v, authorized_drafts) if authorized_drafts else no_history(gmail, mail, v)
    journal = Path(journal).resolve()
    if not journal.parent.is_dir(): raise STOP('Journal directory does not exist')
    lock_path = Path(getattr(v, 'SENDER_LOCK', Path(v.STATE) / 'abschluss-sicher.lock'))
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc: raise STOP('Another sender/completion is running') from exc
        mails, batch = v.load(v.MAILS), v.load(v.BATCH)
        if not set(authorized_drafts) <= {close.mailbox(m['to']) for m in mails}:
            raise STOP('Authorized draft is outside this explicit batch')
        today = dt.date.today().isoformat()
        validate_prepared(v, mails, batch, expected, smtp_status, today)
        if journal.exists(): raise STOP('Journal already exists; no blind resume/retry')
        registry = Path(getattr(v, 'ATTEMPT_REGISTRY', Path(v.STATE) / 'send-attempts.jsonl'))
        previous = close.read_journal(registry) if registry.exists() else []
        previous_mailboxes = set()
        for record in previous:
            if record.get('event') != 'attempt' or record.get('schema_version') != 1:
                raise STOP('Invalid local reservation registry')
            previous_mailboxes.add(close.mailbox(record.get('email')))
        draft_registry = registry.with_name('draft-attempts.jsonl')
        for record in close.read_journal(draft_registry) if draft_registry.exists() else []:
            if record.get('event') != 'draft_attempt' or record.get('schema_version') != 1:
                raise STOP('Invalid draft reservation registry')
            email = close.mailbox(record.get('email'))
            if email in authorized_drafts:
                if not existing.reservation_matches(record, authorized_drafts[email]):
                    raise STOP('Different draft attempt for an authorized mailbox')
            else:
                previous_mailboxes.add(email)
        if previous_mailboxes & {close.mailbox(m['to']) for m in mails}:
            raise STOP('Mailbox already attempted locally; reconcile, never retry')
        print(f'Prepared batch verified: {len(mails)}/{expected}', file=sys.stderr, flush=True)
        profile_and_control(gmail, known_id, known_query)
        print('Gmail sender and positive control verified', file=sys.stderr, flush=True)
        # The anchor precedes the complete seven-list read. Every subsequent
        # filtered query therefore includes pages changed during that read.
        anchor = dt.datetime.now(dt.timezone.utc).date().isoformat() + 'T00:00:00Z'
        snapshot = crm_snapshot(v)
        print('Seven CRM lists and local suppression history loaded', file=sys.stderr, flush=True)
        # Crucial: complete all N checks before writing the first attempt or sending.
        for index, mail in enumerate(mails, 1):
            live(mail, snapshot)
            history(mail)
            print(f'Preflight checked {index}/{len(mails)}', file=sys.stderr, flush=True)
        snapshot = crm_refresh(v, snapshot, anchor)
        for mail in mails:
            live(mail, snapshot)
        print(f'Seven-list changes since {anchor} and all selected CRM pages rechecked', file=sys.stderr, flush=True)
        if not send: return {'preflight_ok': len(mails), 'sent': 0, 'dry_run': True}
        confirmed = []
        journal_bytes = None
        for index, mail in enumerate(mails, 1):
            if dt.date.today().isoformat() != today: raise STOP('Date changed; SMTP confirmation expired')
            if v.load(v.MAILS) != mails or v.load(v.BATCH) != batch:
                raise STOP('Prepared files changed after preflight')
            if (journal_bytes is None and journal.exists()) or (journal_bytes is not None and journal.read_bytes() != journal_bytes):
                raise STOP('Journal changed externally')
            print(f'Live send check {index}/{len(mails)}: Gmail profile/control', file=sys.stderr, flush=True)
            profile_and_control(gmail, known_id, known_query)
            print(f'Live send check {index}/{len(mails)}: seven CRM list changes/page', file=sys.stderr, flush=True)
            snapshot = crm_refresh(v, snapshot, anchor)
            live(mail, snapshot)
            print(f'Live send check {index}/{len(mails)}: Gmail history', file=sys.stderr, flush=True)
            history(mail)
            common = dict(schema_version=1, batch_id=batch['batch_id'], batch_sha256=close.fingerprint(batch),
                          page_id=close.page_id(mail['page_id']), email=close.mailbox(mail['to']),
                          content_sha256=close.fingerprint(close.content(mail)), attempt_id=str(uuid.uuid4()))
            def record(kind, **fields):
                append_event(journal, dict(common, at=dt.datetime.now(dt.timezone.utc).isoformat(), event=kind, **fields))
            # Reserve globally on this host first: if a crash happens before the
            # run journal is written, a new journal path still cannot retry it.
            append_event(registry, dict(common, event='attempt'))
            record('attempt')
            if close.mailbox(mail['to']) in authorized_drafts:
                ack = gmail('GMAIL_SEND_DRAFT', dict(user_id='me', draft_id=authorized_drafts[close.mailbox(mail['to'])]['draft_id']))
            else:
                ack = gmail('GMAIL_SEND_EMAIL', dict(recipient_email=mail['to'], subject=mail['subject'],
                                                    body=mail['body'], from_email=close.SENDER))
            ack_id = message_id(ack)
            record('send_ack', message_id=ack_id)
            message = readback(gmail, mail, ack_id, pause)
            record('sent_readback', message_id=ack_id, sender=single_address(message['sender']),
                   to=single_address(message['to']), subject=message['subject'], labels=message['labelIds'])
            confirmed.append(close.mailbox(mail['to']))
            print(f'Sent readback confirmed {len(confirmed)}/{len(mails)}', file=sys.stderr, flush=True)
            journal_bytes = journal.read_bytes()
        close.validate(mails, batch, close.read_journal(journal), expected, v.LISTE)
        return {'preflight_ok': len(mails), 'sent': len(confirmed), 'confirmed': confirmed, 'dry_run': False}


def control_from_journal(path):
    """Resolve a known Sent positive control from a previous durable readback."""
    events = close.read_journal(path)
    for event in reversed(events):
        if event.get('event') != 'sent_readback':
            continue
        if (event.get('schema_version') != 1 or event.get('sender') != close.SENDER
                or 'SENT' not in event.get('labels', []) or not event.get('message_id')):
            raise STOP('Previous control journal has an invalid Sent readback')
        email = close.mailbox(event.get('to'))
        subject = event.get('subject')
        if not isinstance(subject, str) or not subject.strip() or any(c in subject for c in ('"', '\r', '\n')):
            raise STOP('Previous Sent subject cannot form a safe control query')
        return event['message_id'], f'in:sent to:{email} subject:"{subject}"'
    raise STOP('Previous control journal has no Sent readback')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected', required=True, type=int)
    parser.add_argument('--journal', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--known-sent-id')
    parser.add_argument('--known-sent-query')
    parser.add_argument('--control-journal', help='Previous new-schema journal: resolve known Sent ID/query')
    parser.add_argument('--liste', required=True, choices=('it', 'ingenieur', 'medizin', 'beratung'))
    parser.add_argument('--send', action='store_true')
    args = parser.parse_args()
    if args.control_journal:
        if args.known_sent_id or args.known_sent_query:
            parser.error('Use control-journal OR explicit known Sent ID/query')
        args.known_sent_id, args.known_sent_query = control_from_journal(args.control_journal)
    if not args.known_sent_id or not args.known_sent_query:
        parser.error('A known Sent ID/query or control-journal is required')
    sys.argv = [sys.argv[0], '--liste', args.liste]
    import versand as v
    from smtp_pruefen import status
    try:
        print(json.dumps(run(v, Gmail(args.account), args.expected, args.journal,
                             args.known_sent_id, args.known_sent_query, status, args.send), ensure_ascii=False))
    except Exception as exc:
        raise SystemExit(f'STOP: {exc}; no automatic retry') from exc


if __name__ == '__main__': main()
