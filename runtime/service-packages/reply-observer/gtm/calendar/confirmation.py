"""Acribic invitation plan from an adapter-verified acceptance, never a write.

The caller must resolve acceptance from trusted thread/proposal association,
not from free-form model text. Private slot holds are not meeting invitations.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

CODE='operator@primary.example.invalid'
PIET='contact-3@primary.example.invalid'
TITLE='Austausch zu öffentlichen Ausschreibungen'

@dataclass(frozen=True)
class Binding:
    account: str
    thread_id: str
    recipient: str
    start: datetime
    duration_minutes: int
    proposal_message_id: str

@dataclass(frozen=True)
class Acceptance:
    binding: Binding
    incoming_message_id: str
    verified_sender: str
    verified_incoming_labels: tuple[str, ...]
    verified_exact_agreement: bool

def plan_acribic_invitation(expected: Binding, accepted: Acceptance, *, availability,
                            own_hold_readback_verified, existing_invitation_id=None):
    """Return body plus provenance, requiring every association to match.

    `availability` must be the adapter's fresh exact two-calendar outcome after
    excluding only the validated own hold. This planning function has no client
    object, so it cannot insert events, release holds, send mail, or invite anyone.
    """
    if expected.account!='code' or accepted.binding!=expected:
        raise ValueError('Acceptance must match exact Acribic proposal binding')
    if (not expected.thread_id or not expected.proposal_message_id or
        not accepted.incoming_message_id or accepted.incoming_message_id==expected.proposal_message_id or
        accepted.verified_sender.casefold()!=expected.recipient.casefold() or
        not accepted.verified_exact_agreement or 'SENT' in accepted.verified_incoming_labels or
        'DRAFT' in accepted.verified_incoming_labels):
        raise ValueError('Acceptance source and exact agreement require review')
    if (expected.start.tzinfo is None or expected.start.utcoffset() is None or
        isinstance(expected.duration_minutes,bool) or not isinstance(expected.duration_minutes,int) or
        not 1<=expected.duration_minutes<=120 or '@' not in expected.recipient or
        expected.recipient.casefold() in (CODE,PIET)):
        raise ValueError('Exact agreed start, duration and external recipient required')
    if availability!='free' or own_hold_readback_verified is not True:
        raise ValueError('Fresh availability and exact own hold readback required')
    if existing_invitation_id:
        raise ValueError('Existing invitation requires reconciliation, no duplicate plan')
    return {'calendar_id':CODE,'body':{'summary':TITLE,
        'start':{'dateTime':expected.start.isoformat()},
        'end':{'dateTime':(expected.start+timedelta(minutes=expected.duration_minutes)).isoformat()},
        'attendees':[{'email':CODE},{'email':PIET},{'email':expected.recipient}]},
        'source':{'thread_id':expected.thread_id,'proposal_message_id':expected.proposal_message_id,
                  'acceptance_message_id':accepted.incoming_message_id},'requires_live_write_adapter':True}

# A boolean from a model is never sufficient for the write adapter. These
# immutable mail records must come from full Gmail messages under a checked profile.
@dataclass(frozen=True)
class MailEvidence:
    account_email: str
    message_id: str
    thread_id: str
    sender: str
    recipients: tuple[str, ...]
    body: str
    labels: tuple[str, ...]
    rfc_message_id: str
    in_reply_to: str=''
    complete: bool=True

    def source_text(self):
        import json
        # Labels are mutable mailbox state, not immutable source content.
        headers=json.dumps({'account':self.account_email,'id':self.message_id,'thread':self.thread_id,
            'sender':self.sender,'recipients':self.recipients,
            'rfc_message_id':self.rfc_message_id,'in_reply_to':self.in_reply_to},
            ensure_ascii=False,sort_keys=True,separators=(',',':'))
        return 'GMAIL-SOURCE-V1\n'+headers+'\n\n'+self.body

@dataclass(frozen=True)
class ResolvedAcceptance(Acceptance):
    incoming: MailEvidence
    proposal: MailEvidence


def authored_text(body):
    """Conservative quote boundary; unsupported mail layouts remain review cases."""
    import re
    lines=body.splitlines(keepends=True);selected=[]
    for index,line in enumerate(lines):
        # Gmail can wrap the real sender header before 'wrote:'. Only the
        # bounded full address-bearing header is a quote boundary; arbitrary
        # body markers keep their ordinary data meaning.
        header=''.join(lines[index:index+3])
        wrapped=re.match(r'^\s*(?:On\s+.+?\bwrote\s*:|Am\s+.+?\bschrieb\s*:)',header,re.I|re.S)
        if wrapped and re.search(r'<[^<>\s]+@[^<>\s]+>',wrapped.group(0)):break
        if re.match(r'^\s*(?:>|Von:|From:|-----Original Message-----|-----Ursprüngliche Nachricht-----|Am .+schrieb\s*:|On .+wrote\s*:)',line,re.I):break
        selected.append(line)
    return ''.join(selected).strip()


def resolve_single_proposal_acceptance(binding,proposal,incoming):
    """Non-LLM strict acceptance of one sent, explicitly timed/duration-bound offer.

    Unsupported wording, multiple starts, altered times, signatures, extra prose,
    incomplete sources or missing RFC parent association require human review.
    """
    import re
    if not isinstance(proposal,MailEvidence) or not isinstance(incoming,MailEvidence):raise ValueError('Full mail evidence required')
    if not proposal.complete or not incoming.complete:raise ValueError('Incomplete mail cannot establish agreement')
    if (proposal.account_email!=CODE or incoming.account_email!=CODE or proposal.sender.casefold()!=CODE or
        proposal.message_id!=binding.proposal_message_id or proposal.thread_id!=binding.thread_id or
        incoming.thread_id!=binding.thread_id or incoming.sender.casefold()!=binding.recipient.casefold() or
        set(r.casefold() for r in proposal.recipients)!={binding.recipient.casefold()} or
        set(r.casefold() for r in incoming.recipients)!={CODE} or 'SENT' not in proposal.labels or
        'DRAFT' in proposal.labels or 'SENT' in incoming.labels or 'DRAFT' in incoming.labels or
        not proposal.rfc_message_id or incoming.in_reply_to!=proposal.rfc_message_id or
        incoming.message_id==proposal.message_id or not incoming.message_id):
        raise ValueError('Exact account/thread/recipient/RFC proposal association required')
    original=authored_text(proposal.body)
    from zoneinfo import ZoneInfo
    start=binding.start.astimezone(ZoneInfo('Europe/Berlin'))
    slot=f'{start:%d.%m.%Y}, {start:%H:%M}'
    duration_tokens=(f'{binding.duration_minutes} Minuten',f'{binding.duration_minutes}-minütigen',f'{binding.duration_minutes}-minütiges',f'{binding.duration_minutes}-minütiger',f'{binding.duration_minutes}-minütige')
    weekdays=('Montag','Dienstag','Mittwoch','Donnerstag','Freitag','Samstag','Sonntag')
    written_days=re.findall(r'\b(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)\b',original,re.I)
    if (any(day.casefold()!=weekdays[start.weekday()].casefold() for day in written_days) or
        re.search(r'\b(?:nicht|leider|belegt|besetzt|stattdessen|alternativ)\b',original,re.I) or
        slot not in original or not any(token in original for token in duration_tokens) or
        re.findall(r'\b\d{1,2}:\d{2}\b',original)!=[f'{start:%H:%M}']):
        raise ValueError('One exact authored start and agreed duration required')
    reply=' '.join(authored_text(incoming.body).split()).casefold()
    supported={'ja, der termin passt.','der termin passt.','ja, passt.','passt, danke.','ja, gerne.'}
    if reply not in supported:raise ValueError('Acceptance wording unsupported or ambiguous; review required')
    accepted=ResolvedAcceptance(binding,incoming.message_id,incoming.sender,incoming.labels,True,incoming,proposal)
    plan_acribic_invitation(binding,accepted,availability='free',own_hold_readback_verified=True)
    return accepted
