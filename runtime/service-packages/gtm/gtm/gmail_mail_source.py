"""Trusted reader contract: Gmail thread -> MailSource for the owner's own sent mail.

Read-only: users.getProfile (the authenticated account) and users.threads.get format=full.
Field shapes follow the Gmail REST reference (Message.labelIds, internalDate epoch ms string,
MessagePart.mimeType/headers/body.data base64url/parts). Provenance fields are copied from the
API objects here, never from a model. internalDate can be client-set for API-migrated mail, and
an inserted message can carry SENT; both stay a documented residual risk.
"""
from __future__ import annotations
import base64
import datetime as dt
import hashlib
import json

from gtm.mail_commitments import MailSource, bare_address

MAX_PARTS = 40
MAX_BODY = 200_000


class MailSourceError(ValueError):
    pass


def _text(part, depth=0):
    if depth > 6 or not isinstance(part, dict):
        return None
    if part.get('mimeType') == 'text/plain' and not part.get('filename'):
        data = (part.get('body') or {}).get('data')
        if not isinstance(data, str):
            return None
        try:
            raw = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))
            return raw[:MAX_BODY].decode('utf-8')
        except (ValueError, UnicodeDecodeError):
            return None
    for child in (part.get('parts') or [])[:MAX_PARTS]:
        found = _text(child, depth + 1)
        if found:
            return found
    return None


def verified_profile(service, owner_address):
    """The authenticated account address, only if it equals the owner.

    `service` is a Gmail API client for the account being read; its profile address must equal
    the owner."""
    profile = service.users().getProfile(userId='me').execute().get('emailAddress')
    if bare_address(profile) is None or bare_address(profile) != bare_address(owner_address):
        raise MailSourceError('mailbox_identity_mismatch')
    return profile


def thread_sources(service, owner_address, thread_id):
    """(sources, gaps): SENT messages with readable text. Any gap makes the thread incomplete, and
    plan_mail_commitments refuses an incomplete thread."""
    profile = verified_profile(service, owner_address)
    thread = service.users().threads().get(userId='me', id=thread_id, format='full').execute()
    messages = thread.get('messages')
    if thread.get('id') != thread_id or not isinstance(messages, list) or not messages:
        raise MailSourceError('thread_unreadable')
    sources, gaps = [], []
    for msg in messages:
        mid = msg.get('id') if isinstance(msg, dict) else None
        stamp = str(msg.get('internalDate', '')) if isinstance(msg, dict) else ''
        if not mid or msg.get('threadId') != thread_id or not stamp.isdigit():
            gaps.append(f'unreadable:{mid or "?"}'); continue
        labels = tuple(msg.get('labelIds') or ())
        if 'SENT' not in {str(x).upper() for x in labels}:
            continue  # not the owner's mail: context only, never a commitment source
        headers = {h.get('name', '').lower(): h.get('value', '') for h in (msg.get('payload') or {}).get('headers', [])
                   if isinstance(h, dict)}
        body = _text(msg.get('payload'))
        if not body or not body.strip():
            gaps.append(f'no_text_body:{mid}'); continue
        sent = dt.datetime.fromtimestamp(int(stamp) / 1000, dt.timezone.utc).isoformat()
        sources.append(MailSource(message_id=mid, thread_id=thread_id, from_header=headers.get('from', ''),
                                  sent_at=sent, body=body, thread_complete=not gaps,
                                  labels=labels, mailbox_account=profile))
    complete = not gaps
    sources = [MailSource(**{**s.__dict__, 'thread_complete': complete}) for s in sources]
    return sources, gaps


def thread_fingerprint(service, owner_address, thread_id):
    """Digest of the whole current thread, inbound included: ids, internalDate, labels and body
    (or the full payload when there is no text body). A newer recipient reply, a label change or an
    edited body changes it. Returns (digest, message_count). Unreadable thread raises, so unknown
    coverage can never look unchanged."""
    verified_profile(service, owner_address)
    thread = service.users().threads().get(userId='me', id=thread_id, format='full').execute()
    messages = thread.get('messages')
    if thread.get('id') != thread_id or not isinstance(messages, list) or not messages:
        raise MailSourceError('thread_unreadable')
    rows = []
    for msg in messages:
        if not isinstance(msg, dict) or not msg.get('id') or not str(msg.get('internalDate', '')).isdigit():
            raise MailSourceError('thread_message_unreadable')
        body = _text(msg.get('payload'))
        content = body if body is not None else json.dumps(msg.get('payload'), sort_keys=True, default=str)
        rows.append([msg['id'], str(msg['internalDate']), sorted(str(x) for x in msg.get('labelIds') or ()),
                     hashlib.sha256(content.encode()).hexdigest()])
    rows.sort()
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest(), len(rows)


def recent_sent_thread_ids(service, owner_address, *, days=2, limit=10):
    """Threads with an own sent message in the last `days` (users.messages.list, labelIds SENT, documented q syntax).
    Bounded to `limit`. Only identifies candidates; every thread is read in full again before use."""
    verified_profile(service, owner_address)
    if type(days) is not int or not 1 <= days <= 30 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('bounded_days_and_limit_required')
    listing = service.users().messages().list(userId='me', labelIds=['SENT'], q=f'newer_than:{days}d', maxResults=limit).execute()
    seen = []
    for ref in listing.get('messages', []) if isinstance(listing, dict) else []:
        tid = ref.get('threadId') if isinstance(ref, dict) else None
        if tid and tid not in seen:
            seen.append(tid)
    return seen[:limit]
