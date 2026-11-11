"""One call analysis feeds CRM, archive and Monolith's human review.

No extractor/model runs here. Quotes and explicit actor roles accompany every
result; incomplete sources and invented evidence fail before any SaaS write.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib

from gtm.coordinator import Identity
from gtm.twenty import NotePlan, Outcome, Statement, OUTCOME_LABELS


def evidence_statements(value):
    """One legacy statement or every unchanged statement of a nonempty group."""
    values = (value,) if isinstance(value, Statement) else value
    if not isinstance(values, (tuple, list)) or not values or any(
        not isinstance(item, Statement) for item in values
    ):
        raise ValueError('One or more attributed evidence statements required')
    return tuple(values)


def review_statements(items):
    return tuple(quote for item in items for quote in evidence_statements(item.evidence))


def evidence_text(value):
    quotes = evidence_statements(value)
    if len(quotes) == 1:
        return quotes[0].quote  # Existing transport bytes remain unchanged.
    return '\n'.join(f'{q.speaker}: „{q.quote}“ ({q.evidence})' for q in quotes)


@dataclass(frozen=True)
class TranscriptPart:
    offset: int
    text: str
    next_offset: int | None


def full_transcript(parts, *, completion_confirmed, offset_unit='codepoints'):
    if completion_confirmed is not True or not parts:
        raise ValueError('A completed call and nonempty complete source are required')
    if offset_unit not in ('codepoints', 'utf16'):
        raise ValueError('Provider offset unit must be explicit and supported')
    cursor = 0; text = []
    for index, part in enumerate(parts):
        if type(part.offset) is not int or part.offset != cursor or not isinstance(part.text, str) or not part.text:
            raise ValueError('Transcript source has a gap, overlap or empty page')
        text.append(part.text)
        cursor += len(part.text) if offset_unit == 'codepoints' else len(part.text.encode('utf-16-le')) // 2
        if index < len(parts) - 1:
            if part.next_offset != cursor:
                raise ValueError('Use the exact provider continuation offset')
        elif part.next_offset is not None:
            raise ValueError('Final transcript continuation has not been read')
    result = ''.join(text)
    if not result.strip(): raise ValueError('Transcript is empty')
    return result


@dataclass(frozen=True)
class ReviewItem:
    key: str
    title: str
    evidence: Statement | tuple[Statement, ...]
    kind: str = 'action'  # action or a still-open question/research issue
    assignee: str = 'unclear'
    assignee_name: str | None = None
    due_date: str | None = None
    deadline_basis: str | None = None
    estimated_minutes: int | None = None


@dataclass(frozen=True)
class CoachingPoint:
    kind: str  # strength, improvement, missed_followup; suggestions, never a score
    evidence: Statement | tuple[Statement, ...]
    reason: str
    suggestion: str
    timestamp: str | None = None


@dataclass(frozen=True)
class CallAnalysis:
    identity: Identity
    company_id: str | None
    company: str | None
    title: str
    occurred_at: str
    transcript: str
    summary: str
    demo: Outcome
    followup_permission: Outcome
    statements: tuple[Statement, ...]
    review_items: tuple[ReviewItem, ...]
    source_url: str | None = None
    coaching: tuple[CoachingPoint, ...] = ()
    crm_review: dict | None = None
    crm_schema: dict | None = None

    def checked(self):
        self.identity.checked()
        if self.identity.kind != 'call' or hashlib.sha256(self.transcript.encode()).hexdigest() != self.identity.source_hash:
            raise ValueError('Call identity requires the complete unchanged original hash')
        stamp = datetime.fromisoformat(self.occurred_at.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('Actual conversation time with timezone required')
        # Reuse exact quote/party/five-state validation, including the prohibition
        # on inferring their interest from our own proposal.
        NotePlan(self.identity, self.company_id or '00000000-0000-4000-8000-00000000000b',
                 self.transcript, self.summary, self.statements + review_statements(self.review_items), self.demo,
                 self.followup_permission, occurred_at=self.occurred_at).checked()
        if len(self.coaching)>3:raise ValueError('At most three evidenced coaching suggestions')
        for point in self.coaching:
            if point.kind not in ('strength','improvement','missed_followup'):
                raise ValueError('Unsupported coaching kind')
            if not isinstance(point.reason,str) or not point.reason.strip() or len(point.reason)>1200:
                raise ValueError('Concrete coaching reason required')
            if not isinstance(point.suggestion,str) or not point.suggestion.strip() or len(point.suggestion)>1200:
                raise ValueError('Concrete suggested question/formulation required')
            if point.timestamp is not None:raise ValueError('No actual source timestamps; coaching timestamp must be null')
            for quote in evidence_statements(point.evidence):
                if (not quote.quote.strip() or len(quote.quote)>1200 or quote.quote not in self.transcript
                        or not quote.speaker.strip() or not quote.evidence.strip()
                        or quote.party not in ('ole','piet','counterparty','other','unknown')):
                    raise ValueError('Exact attributed original coaching evidence required')
        keys = set()
        for item in self.review_items:
            if not item.key or item.key in keys or not item.title.strip() or item.kind not in ('action', 'open_question'):
                raise ValueError('Distinct evidenced review items required')
            keys.add(item.key)
            quotes = evidence_statements(item.evidence)
            for quote in quotes:
                if quote.quote not in self.transcript or not quote.quote.strip() or not quote.evidence.strip():
                    raise ValueError('Exact original evidence required for open questions too')
            if item.assignee not in ('ole', 'other', 'unclear'):
                raise ValueError('Explicit assignee or unclear required')
            if item.estimated_minutes is not None and (type(item.estimated_minutes) is not int or item.estimated_minutes <= 0):
                raise ValueError('Unknown effort is null; no guessed zero or invalid effort')
            if item.due_date is not None:
                # Relative dates require a separate reviewed resolution; this
                # transport accepts only an actual absolute date in the quote.
                parsed = datetime.strptime(item.due_date, '%Y-%m-%d')
                if not item.deadline_basis or not any(item.deadline_basis in quote.quote for quote in quotes):
                    raise ValueError('Deadline needs its exact original basis')
                candidates = (parsed.strftime('%Y-%m-%d'), parsed.strftime('%d.%m.%Y'), f'{parsed.day}.{parsed.month}.{parsed.year}')
                if not any(date in item.deadline_basis for date in candidates):
                    raise ValueError('Absolute deadline not evidenced; keep null for review')
            elif item.deadline_basis is not None:
                raise ValueError('A missing deadline has no asserted date basis')
        return self

    def note_plan(self, *, notion_url=None, existing_note=None, existing_targets=None):
        self.checked()
        if not self.company_id:
            raise ValueError('CRM mapping unresolved; preserve archive/Monolith and defer CRM')
        return NotePlan(self.identity, self.company_id, self.transcript, self.summary,
                        self.statements + (review_statements(self.review_items) if self.crm_review else ()), self.demo, self.followup_permission,
                        occurred_at=self.occurred_at, archive_url=notion_url, crm_review=self.crm_review,
                        action_review=self.action_markdown() if self.crm_review else None,
                        existing_note=existing_note,existing_targets=existing_targets,crm_schema=self.crm_schema)

    def monolith_payload(self, *, notion_url):
        self.checked()
        if not self.review_items: return None
        return {'meetingId': self.identity.source_id, 'version': self.identity.version,
                'title': self.title, 'company': self.company, 'category': self.identity.category,
                'occurredAt': self.occurred_at, 'notionUrl': notion_url, 'sourceUrl': self.source_url,
                'transcriptHash': self.identity.source_hash, 'actionItems': [
                    {'key': item.key, 'title': ('Klären: ' if item.kind == 'open_question' else '') + item.title,
                     'evidence': evidence_text(item.evidence), 'assignee': item.assignee,
                     'assigneeName': item.assignee_name, 'dueDate': item.due_date,
                     'deadlineBasis': item.deadline_basis, 'estimatedMinutes': item.estimated_minutes}
                    for item in self.review_items]}

    def action_markdown(self):
        if not self.review_items:return 'Nächste Schritte: keine belegte Zusage erfasst.'
        rows=['Nächste Schritte und offene Fragen:']
        for item in self.review_items:
            owner=item.assignee_name or {'ole':'Alex','other':'andere Person','unclear':'Zuständigkeit offen'}[item.assignee]
            rows.append(f'- {item.title} — {owner}; Frist: {item.due_date or "offen"}.')
            for quote in evidence_statements(item.evidence):
                rows.append(f'  {quote.speaker}: „{quote.quote}“ ({quote.evidence})')
        return '\n'.join(rows)

    def archive_markdown(self):
        self.checked()
        # The actual final transcript string is opaque and remains byte-identical.
        before = (f'---\ntags: [status/entwurf]\nwispr_id: "{self.identity.source_id}"\n'
                  f'gespraech: "{self.occurred_at}"\n---\n\n# {self.title}\n\n{self.summary}\n\n'
                  f'Demo: {OUTCOME_LABELS[self.demo.state]}\n\nRückfragen: {OUTCOME_LABELS[self.followup_permission.state]}\n\n')
        notes = '\n'.join(f'- {s.speaker}: „{s.quote}“ ({s.evidence})' for s in self.statements)
        if self.crm_review:
            for label,outcome in (('Demo-Belege',self.demo),('Rückfrage-Belege',self.followup_permission)):
                notes+='\n'+label+':\n'+'\n'.join(f'- {s.speaker}: „{s.quote}“ ({s.evidence})' for s in outcome.evidence)
            notes+='\n\n'+self.action_markdown()
        if self.source_url: before += f'Quelle: {self.source_url}\n\n'
        from gtm.call_flow.crm import review_markdown
        prose = before + notes + review_markdown(self.crm_review,self.crm_schema)
        if '\n## Originaltranskript\n' in prose:
            raise ValueError('Analysis text collides with archive source marker; review before writing')
        return prose + '\n\n## Originaltranskript\n\n' + self.transcript
