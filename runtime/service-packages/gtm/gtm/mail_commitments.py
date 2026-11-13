"""Owner's own sent mail as a commitment source.

No extractor or model runs here. A reviewed extraction supplies items with the
exact quote from the mail body; this module validates and records them. Only
mail whose bare sender address equals the owner's address counts, so incoming
mail can never create or change an obligation. A date is accepted only when
the quote itself carries that absolute date; weekdays stay undated.
"""
from __future__ import annotations
import datetime as dt
import re
from dataclasses import dataclass
from email.utils import getaddresses

from gtm.commitments import CommitmentStore

_ADDR = re.compile(r'^[A-Za-z0-9._%+\'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')


def bare_address(header):
    """One RFC 5322 address (display name allowed) or None. Several or none is ambiguous."""
    if not isinstance(header, str) or re.search(r'[\x00-\x1f\x7f]', header):
        return None
    found = [a for _, a in getaddresses([header]) if a]
    if len(found) != 1 or not _ADDR.match(found[0]):
        return None
    return found[0].lower()


@dataclass(frozen=True)
class MailSource:
    message_id: str
    thread_id: str
    from_header: str          # the From header as read from the mailbox
    sent_at: str
    body: str
    thread_complete: bool
    labels: tuple = ()        # mailbox labels of this message (Gmail labelIds)
    mailbox_account: str = '' # the authenticated account the message was read from


@dataclass(frozen=True)
class MailCommitment:
    key: str
    title: str
    quote: str
    due_date: str | None = None
    deadline_basis: str | None = None


def _dates(day):
    return (day.strftime('%Y-%m-%d'), day.strftime('%d.%m.%Y'), f'{day.day}.{day.month}.{day.year}')


def own_sent(owner_address, source):
    """Own-sent provenance: read from the owner's authenticated mailbox, carrying the SENT label (and not
    DRAFT), with a From header that normalizes to the owner address. A header alone is spoofable and never
    enough. A mailbox that accepts imported mail could still plant a SENT label; that residual risk stays."""
    owner = bare_address(owner_address)
    labels = {str(x).upper() for x in source.labels}
    if (owner is None or bare_address(source.mailbox_account) != owner or bare_address(source.from_header) != owner
            or 'SENT' not in labels or 'DRAFT' in labels):
        raise ValueError('Only mail sent from the owner\'s own authenticated mailbox creates commitments')


def plan_mail_commitments(owner_address, source: MailSource, items, *, tz=None, due_hour=18):
    """Validate everything and return [(title, ref, due)]. Writes nothing."""
    if source.thread_complete is not True:
        raise ValueError('Complete mail source required')
    own_sent(owner_address, source)
    if not source.message_id.strip() or not source.body.strip():
        raise ValueError('Mail identity and body required')
    sent = dt.datetime.fromisoformat(source.sent_at.replace('Z', '+00:00'))
    if sent.tzinfo is None:
        raise ValueError('Send time with offset required')
    items = tuple(items)
    if not items or len({i.key for i in items}) != len(items):
        raise ValueError('Distinct evidenced items required')
    import zoneinfo
    zone = tz or zoneinfo.ZoneInfo('Europe/Berlin')
    planned = []
    for item in items:
        if not item.key.strip() or not item.title.strip() or not item.quote.strip() or item.quote not in source.body:
            raise ValueError('Exact quote from the mail body required')
        due = None
        if item.due_date:
            day = dt.datetime.strptime(item.due_date, '%Y-%m-%d')
            if not item.deadline_basis or item.deadline_basis not in item.quote or not any(d in item.deadline_basis for d in _dates(day)):
                raise ValueError('Absolute deadline not evidenced in the quote')
            due = day.replace(hour=due_hour, tzinfo=zone).isoformat()
        elif item.deadline_basis is not None:
            raise ValueError('A missing deadline has no basis')
        ref = f'mail:{source.thread_id}:{source.message_id}:{item.key}'
        planned.append((item.title, ref, due))
    return planned


def record_mail_commitments(store: CommitmentStore, owner_address, source: MailSource, items, *, tz=None, due_hour=18):
    planned = plan_mail_commitments(owner_address, source, items, tz=tz, due_hour=due_hour)
    for title, ref, due in planned:  # everything validated before the first write
        store.add(title, ref, due, 'date' if due else 'time')
    return [ref for _, ref, _ in planned]
