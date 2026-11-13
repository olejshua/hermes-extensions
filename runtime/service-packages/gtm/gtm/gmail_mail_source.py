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
