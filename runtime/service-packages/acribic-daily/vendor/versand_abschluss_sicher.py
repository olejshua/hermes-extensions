#!/usr/bin/env python3
"""Fail-closed CRM completion. Never sends mail. Journal schema is defined below.

Journal: UTF-8 JSONL; every event has schema_version=1, event, batch_id,
batch_sha256, page_id, email, content_sha256, attempt_id. An attempt precedes
send_ack and sent_readback. Both latter events carry the same message_id.
sent_readback additionally carries sender, to, subject, labels=['SENT', ...]. The body fingerprint binds the
exact local content before sending; it does not claim server body readback.

Lost-ACK recovery (no retry/send): after one unresolved attempt, append
send_ack_recovered with the same common identity fields and message_id, sender,
to, subject, labels containing SENT, unique_match_count=1 (integer),
evidence_source='gmail_sent_read_only', recovery_reason='lost_ack'. These fields
must come from a read-only exact unique Gmail Sent match. Then append the usual
sent_readback with the same message_id. Recovery alone never marks CRM sent.
Only append records; never rewrite earlier journal records.
Fingerprints use canonical JSON: sort_keys=True, ensure_ascii=False,
separators=(',', ':'). Batch hash covers the entire batch.json; content hash
covers {page_id, to, subject, body}, with page_id dehyphenated/lowercase and
to stripped/lowercase. No other content normalization is permitted.
"""
import argparse
from collections import Counter
import csv
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path

SENDER = 'operator@primary.example.invalid'


class UnsafeCompletion(ValueError):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def mailbox(value):
    if not isinstance(value, str) or value.count('@') != 1 or any(c.isspace() for c in value.strip()):
        raise UnsafeCompletion('Invalid single mailbox')
    normalized=value.strip().lower()
    local,domain=normalized.split('@')
    if (not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+",local)
            or local.startswith('.') or local.endswith('.') or '..' in local
            or len(domain.split('.'))<2
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in domain.split('.'))):
        raise UnsafeCompletion('Invalid single mailbox syntax')
    return normalized


def page_id(value):
    value = str(value).replace('-', '').lower()
    if len(value) != 32 or any(c not in 'e00000000000000d' for c in value):
        raise UnsafeCompletion('Invalid page_id')
    return value


def content(mail):
    for field in ('subject', 'body'):
        if not isinstance(mail.get(field), str) or not mail[field].strip():
            raise UnsafeCompletion('Missing exact subject/body')
    return dict(page_id=page_id(mail['page_id']), to=mailbox(mail['to']),
                subject=mail['subject'], body=mail['body'])


def read_journal(path):
    events = []
    for number, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            raise UnsafeCompletion(f'Empty journal record at line {number}')
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise UnsafeCompletion(f'Broken journal at line {number}') from exc
        if not isinstance(event, dict):
            raise UnsafeCompletion('Journal record must be an object')
        events.append(event)
    return events


def validate(mails, batch, events, expected, liste, details=False):
    """Return confirmed set (default) or confirmed/uncertain/not_attempted sets.

    Valid unresolved attempts remain held. Structural ambiguity stops the whole
    closure, while proven sent members may complete independently.
    """
    if type(expected) is not int or expected <= 0:
        raise UnsafeCompletion('expected must be a positive user-specified integer')
    if not isinstance(mails, list) or len(mails) != expected:
        raise UnsafeCompletion('Prepared mail count must exactly equal expected N')
    if not isinstance(batch, dict) or batch.get('liste', 'it') != liste:
        raise UnsafeCompletion('Wrong batch/list')
    if not isinstance(batch.get('batch_id'), str) or not batch['batch_id'].strip():
        raise UnsafeCompletion('Missing batch_id; legacy batch cannot be completed')
    candidate_keys = [(page_id(c['page_id']), mailbox(c['email'])) for c in batch['kandidaten']]
    if len(candidate_keys) != len(set(candidate_keys)):
        raise UnsafeCompletion('Duplicate batch candidate identity')
    candidates = set(candidate_keys)
    by_key = {}
    used_emails, used_pages = set(), set()
    for mail in mails:
        c = content(mail)
        key = (c['page_id'], c['to'])
        if c['to'] in used_emails or c['page_id'] in used_pages:
            raise UnsafeCompletion('Duplicate mailbox or CRM page')
        if key not in candidates:
            raise UnsafeCompletion('Mail is not in current batch')
        # cmd_abschluss relies on these fields and claims SMTP confirmation.
        for field in ('vorname', 'gekuerzt'):
            if field not in mail:
                raise UnsafeCompletion('Incomplete mails.json')
        candidate = next(ca for ca in batch['kandidaten'] if (page_id(ca['page_id']), mailbox(ca['email'])) == key)
        if candidate.get('smtp') != 'OK' or candidate.get('smtp_probe', True):
            raise UnsafeCompletion('Mail lacks real SMTP confirmation')
        by_key[key] = c
        used_emails.add(c['to']); used_pages.add(c['page_id'])
    states, message_ids, attempt_ids = {}, set(), set()
    batch_hash = fingerprint(batch)
    for event in events:
        if event.get('schema_version') != 1:
            raise UnsafeCompletion('Old or unknown journal schema')
        if event.get('batch_id') != batch['batch_id'] or event.get('batch_sha256') != batch_hash:
            raise UnsafeCompletion('Journal belongs to another/changed batch')
        key = (page_id(event.get('page_id')), mailbox(event.get('email')))
        if key not in by_key or event.get('content_sha256') != fingerprint(by_key[key]):
            raise UnsafeCompletion('Journal mail/content mismatch')
        attempt = event.get('attempt_id')
        if not isinstance(attempt, str) or not attempt.strip():
            raise UnsafeCompletion('Missing attempt_id')
        kind = event.get('event')
        prior = states.get(key)
        if kind == 'attempt':
            if prior is not None or attempt in attempt_ids:
                raise UnsafeCompletion('Repeated attempt/attempt ID: no blind retry')
            attempt_ids.add(attempt)
            states[key] = ('attempt', attempt, None)
        elif kind in ('send_ack', 'send_ack_recovered'):
            msg = event.get('message_id')
            if prior != ('attempt', attempt, None) or not isinstance(msg, str) or not msg.strip() or msg in message_ids:
                raise UnsafeCompletion('Invalid/duplicate send acknowledgment')
            if kind == 'send_ack_recovered':
                if (event.get('evidence_source') != 'gmail_sent_read_only'
                        or event.get('recovery_reason') != 'lost_ack'
                        or type(event.get('unique_match_count')) is not int
                        or event['unique_match_count'] != 1
                        or mailbox(event.get('sender')) != SENDER
                        or mailbox(event.get('to')) != key[1]
                        or event.get('subject') != by_key[key]['subject']
                        or not isinstance(event.get('labels'), list) or 'SENT' not in event['labels']):
                    raise UnsafeCompletion('Lost-ACK recovery lacks exact unique read-only Sent evidence')
            message_ids.add(msg)
            states[key] = (kind, attempt, msg)
        elif kind == 'sent_readback':
            if (prior is None or prior[0] not in ('send_ack', 'send_ack_recovered')
                    or prior[1:] != (attempt, event.get('message_id'))):
                raise UnsafeCompletion('Readback without matching acknowledgment')
            if (mailbox(event.get('sender')) != SENDER or mailbox(event.get('to')) != key[1]
                    or event.get('subject') != by_key[key]['subject']
                    or not isinstance(event.get('labels'), list) or 'SENT' not in event['labels']):
                raise UnsafeCompletion('Readback sender/content/SENT mismatch')
            states[key] = ('sent_readback', attempt, prior[2])
        else:
            raise UnsafeCompletion('Unknown journal event')
    confirmed = {key[1] for key, state in states.items() if state[0] == 'sent_readback'}
    uncertain = {key[1] for key, state in states.items() if state[0] != 'sent_readback'}
    if details:
        return dict(confirmed=confirmed, uncertain=uncertain,
                    not_attempted=used_emails - confirmed - uncertain)
    return confirmed


def atomic_record(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush(); os.fsync(handle.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def sent_history_counts(path):
    """Count every E-Mail-raus history row across dates/batches per mailbox."""
    counts = Counter()
    with Path(path).open(encoding='utf-8', newline='') as handle:
        for row in csv.reader(handle, delimiter=';', strict=True):
            if len(row) > 1 and row[1].strip() == 'E-Mail raus':
                counts[mailbox(row[0])] += 1
    return counts


def complete(v, journal, expected, probe=False):
    """External reads/writes belong to injected legacy backend v, testable offline."""
    journal = Path(journal)
    lock_path = Path(getattr(v, 'SENDER_LOCK', Path(v.STATE) / 'abschluss-sicher.lock'))
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UnsafeCompletion('Another completion is running') from exc
        mails, batch = v.load(v.MAILS), v.load(v.BATCH)
        if isinstance(batch,dict) and batch.get('crm_backend','notion') != getattr(v,'BACKEND','notion'):
            raise UnsafeCompletion('Prepared completion belongs to another CRM backend')
        journal_bytes = journal.read_bytes()
        validated = validate(mails, batch, read_journal(journal), expected, v.LISTE, details=True)
        confirmed = validated['confirmed']
        uncertain = validated['uncertain']
        already_committed = set()
        marker = Path(v.STATE) / ('abschluss-' + fingerprint({'batch_id': batch['batch_id']}) + '.json')
        if marker.exists():
            prior = json.loads(marker.read_text(encoding='utf-8'))
            if prior.get('status') != 'complete':
                raise UnsafeCompletion('Completion outcome uncertain; inspect partial CRM commits, never rerun blindly')
            prefix_length = prior.get('journal_bytes')
            if (prior.get('batch_id') != batch['batch_id']
                    or prior.get('batch_sha256') != fingerprint(batch)
                    or prior.get('mails_sha256') != fingerprint(mails)
                    or type(prefix_length) is not int or prefix_length <= 0
                    or len(journal_bytes) < prefix_length
                    or hashlib.sha256(journal_bytes[:prefix_length]).hexdigest() != prior.get('journal_sha256')):
                raise UnsafeCompletion('Previous complete marker/state/journal prefix mismatch')
            previous = prior.get('confirmed')
            if (not isinstance(previous, list) or not previous
                    or len(previous) != len(set(previous))
                    or any(not isinstance(email, str) or mailbox(email) != email for email in previous)):
                raise UnsafeCompletion('Invalid previous completion marker')
            already_committed = set(previous)
            if not already_committed <= confirmed:
                raise UnsafeCompletion('Previously committed Sent evidence is missing')
        pending = confirmed - already_committed
        # Unrecorded prior commits remain ambiguous. A proven completed subset may
        # be skipped only after its marker, journal prefix and live stage match.
        historical = sent_history_counts(v.NACHTRAG)
        for email in confirmed:
            expected_count = 1 if email in already_committed else 0
            if historical[email] != expected_count:
                raise UnsafeCompletion('Local sent history count mismatch before completion')
        for mail in mails:
            if mailbox(mail['to']) not in confirmed:
                continue
            live = v.api('GET', '/pages/' + mail['page_id'])
            if live.get('archived') or live.get('in_trash'):
                raise UnsafeCompletion('CRM page archived/deleted')
            props = live['properties']
            expected_stage = 'E-Mail raus' if mailbox(mail['to']) in already_committed else 'Nicht kontaktiert'
            if (v.txt(props['Stage']) != expected_stage
                    or mailbox(v.txt(props['E-Mail'])) != mailbox(mail['to'])
                    or v.txt(props.get('Gesprochen', {})) in (True, 'True')
                    or v.NICHT_ANSCHREIBEN.search(v.txt(props.get('Notizen', {})) or '')):
                raise UnsafeCompletion('CRM page changed or already/partially committed')
        if v.load(v.MAILS) != mails or v.load(v.BATCH) != batch or journal.read_bytes() != journal_bytes:
            raise UnsafeCompletion('State changed during preflight')
        excluded = {mailbox(m['to']) for m in mails} - pending
        result = dict(confirmed=sorted(pending), already_committed=sorted(already_committed),
                      excluded=sorted(excluded), uncertain=sorted(uncertain),
                      not_attempted=sorted(validated['not_attempted']), probe=probe)
        if probe or not pending:
            return result
        atomic_record(marker, {'status': 'started', 'batch_id': batch['batch_id'],
                               'batch_sha256': fingerprint(batch), 'mails_sha256': fingerprint(mails),
                               'confirmed': sorted(confirmed), 'journal_sha256': hashlib.sha256(journal_bytes).hexdigest(),
                               'journal_bytes': len(journal_bytes)})
        # Legacy function still performs local logs and index refresh. After each patch,
        # verify the resulting CRM stage; a failed/unknown write leaves started marker.
        original_patch = v.patch
        def checked_patch(pid, stage=None, notiz=None):
            matching = [m for m in mails if page_id(m['page_id']) == page_id(pid) and mailbox(m['to']) in pending]
            if len(matching) != 1 or stage != 'E-Mail raus':
                raise UnsafeCompletion('Legacy completion requested unconfirmed CRM mutation')
            if v.load(v.MAILS) != mails or v.load(v.BATCH) != batch:
                raise UnsafeCompletion('State changed during completion')
            original_patch(pid, stage, notiz)
            readback = v.api('GET', '/pages/' + pid)
            if (readback.get('archived') or readback.get('in_trash')
                    or v.txt(readback['properties']['Stage']) != stage
                    or mailbox(v.txt(readback['properties']['E-Mail'])) != mailbox(matching[0]['to'])):
                raise UnsafeCompletion('CRM write readback mismatch; stop, inspect partial commits')
        v.patch = checked_patch
        previous_authorization = getattr(v, '_CONFIRMED_COMPLETION', None)
        v._CONFIRMED_COMPLETION = frozenset(pending)
        try:
            v.cmd_abschluss(excluded, False)
        finally:
            v.patch = original_patch
            if previous_authorization is None:
                del v._CONFIRMED_COMPLETION
            else:
                v._CONFIRMED_COMPLETION = previous_authorization
        # A successful backend return is insufficient: require both durable
        # history exactly once and current live CRM stage for every confirmed mail.
        if journal.read_bytes() != journal_bytes or v.load(v.MAILS) != mails or v.load(v.BATCH) != batch:
            raise UnsafeCompletion('Journal/prepared files changed during completion')
        final_history = sent_history_counts(v.NACHTRAG)
        if any(final_history[email] != 1 for email in confirmed):
            raise UnsafeCompletion('Local sent history missing/duplicated after completion; inspect started marker')
        for mail in mails:
            if mailbox(mail['to']) not in confirmed:
                continue
            final_page = v.api('GET', '/pages/' + mail['page_id'])
            if (final_page.get('archived') or final_page.get('in_trash')
                    or v.txt(final_page['properties']['Stage']) != 'E-Mail raus'
                    or mailbox(v.txt(final_page['properties']['E-Mail'])) != mailbox(mail['to'])):
                raise UnsafeCompletion('Final CRM state mismatch; inspect started marker')
        if journal.read_bytes() != journal_bytes:
            raise UnsafeCompletion('Journal changed during final verification')
        verified_history = sent_history_counts(v.NACHTRAG)
        if any(verified_history[email] != 1 for email in confirmed):
            raise UnsafeCompletion('Sent history changed during final verification')
        atomic_record(marker, {'status': 'complete', 'batch_id': batch['batch_id'],
                               'batch_sha256': fingerprint(batch), 'mails_sha256': fingerprint(mails),
                               'confirmed': sorted(confirmed), 'journal_sha256': hashlib.sha256(journal_bytes).hexdigest(),
                               'journal_bytes': len(journal_bytes)})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected', type=int, required=True)
    parser.add_argument('--journal', required=True)
    parser.add_argument('--liste', choices=('it', 'ingenieur', 'medizin', 'beratung'), default='it')
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    # legacy import chooses list from argv; set it explicitly before import.
    import sys
    sys.argv = [sys.argv[0], '--liste', args.liste]
    import versand as v
    try:
        print(json.dumps(complete(v, args.journal, args.expected, args.probe), ensure_ascii=False))
    except (UnsafeCompletion, KeyError, TypeError, OSError, ValueError, csv.Error) as exc:
        raise SystemExit(f'STOP: {exc}') from exc


if __name__ == '__main__':
    main()
