#!/usr/bin/env python3
"""Scoped Gmail draft edits; never sends mail. Run with Hermes' Python runtime."""
import argparse
import base64
import hashlib
import html
import json
import re
from email.utils import getaddresses
from email import policy
from email.parser import BytesParser
from pathlib import Path

MAX_DRAFTS = 10
ACCOUNTS = {'code': 'operator@primary.example.invalid', 'stealth': 'operator@secondary.example.invalid'}

def raw_bytes(draft):
    raw = draft['message']['raw']
    return base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))

def digest(draft):
    return hashlib.sha256(raw_bytes(draft)).hexdigest()

def mime(draft):
    return BytesParser(policy=policy.default).parsebytes(raw_bytes(draft))

def text_parts(msg):
    # Ignore whole attachment subtrees, including attached emails.
    if msg.get_content_disposition() == 'attachment' or msg.get_filename():
        return []
    if msg.is_multipart():
        return [part for child in msg.iter_parts() for part in text_parts(child)]
    return [msg] if msg.get_content_type() in ('text/plain', 'text/html') else []

def protected(msg):
    headers = tuple((name.lower(), str(value)) for name, value in msg.items()
                    if not name.lower().startswith('content-') and name.lower() != 'mime-version')
    attachments = tuple((part.get_content_type(), part.get_filename(),
                         hashlib.sha256(part.get_payload(decode=True) or b'').hexdigest())
                        for part in msg.walk() if not part.is_multipart()
                        and (part.get_filename() or part.get_content_disposition() == 'attachment'))
    return headers, attachments

def normalized(text):
    return text.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n')

def stage(draft, body, expected, slots=()):
    if digest(draft) != expected:
        raise ValueError('Draft changed since inspection; inspect again before editing')
    labels = set(draft['message'].get('labelIds', []))
    if 'DRAFT' not in labels or 'SENT' in labels:
        raise ValueError('Message is not an unsent draft')
    msg = mime(draft)
    if any(part.get_content_type() in ('multipart/signed', 'multipart/encrypted') for part in msg.walk()):
        raise ValueError('Signed/encrypted MIME requires manual review')
    parts = text_parts(msg)
    if not parts:
        raise ValueError('No supported editable body')
    from mail_policy import format_body, split_quote
    proposed, supplied_quote = split_quote(body)
    body = normalized(format_body(proposed, slots))
    if not body.strip():
        raise ValueError('Empty body refused')
    plain_quotes = {split_quote(part.get_content())[1] for part in parts if part.get_content_subtype() == 'plain'}
    html_quotes = set()
    for part in parts:
        if part.get_content_subtype() == 'html':
            old = part.get_content()
            match = re.search(r'<(?:blockquote\b|div\b[^>]*class=[\"\'][^\"\']*gmail_quote)', old, re.I)
            if not match and re.search(r'gmail_attr|moz-cite-prefix|divRplyFwdMsg|On .+wrote:|Am .+schrieb|(?:Von|From):', old, re.I):
                raise ValueError('HTML quote boundary ambiguous; manual review required')
            html_quotes.add(old[match.start():] if match else '')
    if len(plain_quotes) > 1 or len(html_quotes) > 1:
        raise ValueError('Different body histories require manual review')
    plain_quote = next(iter(plain_quotes), '')
    html_quote = next(iter(html_quotes), '')
    if supplied_quote and supplied_quote != plain_quote:
        raise ValueError('Quoted history differs; provide only the new own body')
    if plain_quote and any(p.get_content_subtype() == 'html' for p in parts) and not html_quote:
        raise ValueError('HTML quote boundary ambiguous; manual review required')
    html_body = '<html><body>' + html.escape(body).replace('\n', '<br>\n') + ('<br>\n<br>\n' + html_quote if html_quote else '</body></html>')
    body += ('\n\n' + plain_quote if plain_quote else '')
    before = protected(msg)
    for part in parts:
        subtype = part.get_content_subtype()
        part.set_content(body if subtype == 'plain' else html_body, subtype=subtype, charset='utf-8')
    if protected(msg) != before:
        raise ValueError('Protected headers or attachments changed')
    request = {'message': {'threadId': draft['message']['threadId'],
                          'raw': base64.urlsafe_b64encode(msg.as_bytes()).decode()}}
    return request, body, html_body, before

def read_many(service, ids):
    results, errors = {}, {}
    batch = service.new_batch_http_request()
    def callback(key, result, error):
        if error:
            errors[key] = type(error).__name__
        else:
            results[key] = result
    for did in ids:
        batch.add(service.users().drafts().get(userId='me', id=did, format='raw'),
                  request_id=did, callback=callback)
    batch.execute()
    if errors or len(results) != len(ids):
        raise RuntimeError('Draft read failed: ' + str(errors))
    return results

def _apply(service, edits, *, account=None, slots=None, calendar=None):
    slots = slots or {}
    ids = [edit[0] for edit in edits]
    if not 1 <= len(ids) <= MAX_DRAFTS or len(set(ids)) != len(ids):
        raise ValueError('Provide 1–10 distinct draft IDs')
    current = read_many(service, ids)
    # Preflight every draft before any writes. Gmail has no conditional update;
    # do not edit simultaneously in Gmail between this read and write.
    staged = {did: stage(current[did], Path(path).read_text(encoding='utf-8'), sha, slots.get(did, ()))
              for did, path, sha in edits}
    if any(slots.values()) and (not account or calendar is None):
        raise ValueError('Scheduling batch edits require authenticated account and calendar')
    owners = []
    for did in ids:
        msg = mime(current[did])
        recipients = [addr for _, addr in getaddresses([str(msg.get('To', '')), str(msg.get('Cc', ''))]) if addr]
        if slots.get(did):
            owners.append((account, current[did]['message']['threadId'], recipients, slots[did]))
    from mail_policy import scheduling_batch
    with scheduling_batch(calendar, owners):
        latest = read_many(service, ids)
        if any(digest(latest[did]) != digest(current[did]) for did in ids):
            raise ValueError('Draft changed during reservation; no Gmail updates performed, holds retained')
        batch = service.new_batch_http_request()
        errors = {}
        def callback(key, result, error):
            if error:
                errors[key] = type(error).__name__
        for did in ids:
            batch.add(service.users().drafts().update(userId='me', id=did, body=staged[did][0]),
                      request_id=did, callback=callback)
        try:
            batch.execute()
        except Exception as error:
            errors['transport'] = type(error).__name__
        # Never retry uncertain writes. Inspect the saved state instead.
        saved = read_many(service, ids)
        statuses = []
        for did in ids:
            request, body, html_body, preserved = staged[did]
            msg = mime(saved[did])
            parts = text_parts(msg)
            labels = set(saved[did]['message'].get('labelIds', []))
            valid = (saved[did]['message']['threadId'] == request['message']['threadId']
                     and protected(msg) == preserved and 'DRAFT' in labels and 'SENT' not in labels
                     and len(parts) == len(text_parts(mime(current[did])))
                     and all(normalized(part.get_content()) == (body if part.get_content_subtype() == 'plain' else html_body)
                             for part in parts))
            statuses.append({'draft_id': did, 'status': 'verified' if valid else 'needs_review'})
        result = {'results': statuses, 'write_errors': errors,
                  'verified': all(item['status'] == 'verified' for item in statuses)}
        if not result['verified']:
            raise BatchUnverified(result)
        return result


class BatchUnverified(RuntimeError):
    def __init__(self, result):
        super().__init__('Draft write readback unverified; holds retained')
        self.result = result

def apply(service, edits, *, account=None, slots=None, calendar=None):
    try:
        return _apply(service, edits, account=account, slots=slots, calendar=calendar)
    except BatchUnverified as error:
        return error.result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', choices=ACCOUNTS, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect')
    scope = inspect.add_mutually_exclusive_group(required=True)
    scope.add_argument('--query')
    scope.add_argument('--draft-id', action='append')
    edit = commands.add_parser('apply')
    edit.add_argument('--edit', nargs=3, action='append', required=True,
                      metavar=('DRAFT_ID', 'BODY_FILE', 'EXPECTED_SHA256'))
    edit.add_argument('--slot', nargs=2, action='append', default=[], metavar=('DRAFT_ID', 'ISO_START'))
    args = parser.parse_args()
    from email_watcher import service
    api = service(args.account)
    profile = api.users().getProfile(userId='me').execute()
    if profile['emailAddress'].lower() != ACCOUNTS[args.account]:
        raise ValueError('Authenticated mailbox does not match requested account')
    if args.command == 'apply':
        slots = {}
        for did, value in args.slot:
            if did not in [e[0] for e in args.edit]:
                raise ValueError('Slot draft ID is outside edit scope')
            slots.setdefault(did, []).append(value)
        from email_realtime import calendar_service
        result = apply(api, args.edit, account=args.account, slots=slots, calendar=calendar_service() if slots else None)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['verified'] else 1
    if args.draft_id:
        ids = args.draft_id
    else:
        listing = api.users().drafts().list(userId='me', q=args.query, maxResults=MAX_DRAFTS).execute()
        if listing.get('nextPageToken'):
            raise ValueError('More than 10 matches; narrow the query')
        ids = [item['id'] for item in listing.get('drafts', [])]
    if len(ids) > MAX_DRAFTS or len(set(ids)) != len(ids):
        raise ValueError('Provide at most 10 distinct draft IDs')
    drafts = read_many(api, ids) if ids else {}
    result = []
    for did in ids:
        msg = mime(drafts[did])
        parts = text_parts(msg)
        result.append({'draft_id': did, 'thread_id': drafts[did]['message']['threadId'],
                       'sha256': digest(drafts[did]), 'to': str(msg.get('To', '')),
                       'cc': str(msg.get('Cc', '')), 'subject': str(msg.get('Subject', '')),
                       'body': '\n'.join(part.get_content() for part in parts if part.get_content_subtype() == 'plain'),
                       'html_present': any(part.get_content_subtype() == 'html' for part in parts),
                       'attachment_count': len(protected(msg)[1])})
    print(json.dumps(result, ensure_ascii=False))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
