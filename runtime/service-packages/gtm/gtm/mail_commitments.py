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

from gtm.commitments import CommitmentStore

_ADDR = re.compile(r'^[^\s<>@,;]+@[^\s<>@,;]+$')


@dataclass(frozen=True)
class MailSource:
    message_id: str
    thread_id: str
    sender: str
    sent_at: str
    body: str
    thread_complete: bool


@dataclass(frozen=True)
class MailCommitment:
    key: str
    title: str
    quote: str
    due_date: str | None = None
    deadline_basis: str | None = None


def _dates(day):
    return (day.strftime('%Y-%m-%d'), day.strftime('%d.%m.%Y'), f'{day.day}.{day.month}.{day.year}')


def record_mail_commitments(store: CommitmentStore, owner_address, source: MailSource, items, *, tz=None, due_hour=18):
    if source.thread_complete is not True:
        raise ValueError('Complete mail source required')
    if not _ADDR.match(source.sender or '') or source.sender.lower() != owner_address.lower():
        raise ValueError('Only the owner\'s own sent mail creates commitments')
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
    for title, ref, due in planned:  # validate everything before the first write
        store.add(title, ref, due, 'date' if due else 'time')
    return [ref for _, ref, _ in planned]
