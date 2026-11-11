"""Optional typed acceptance resolver; all write/availability logic stays native.

One source version, exact RFC parent, actual sent offer, chosen existing hold.
Several explicitly dated offers are supported only when the reply explicitly
selects one of them. A free model time, conditional wish, or counteroffer cannot
create an offered start. The model's same-run judgement still requires review of
the classifier quality; structural validation does not prove semantic accuracy.
"""
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import hashlib,re,json
from zoneinfo import ZoneInfo
from gtm.calendar.confirmation import CODE,MailEvidence,ResolvedAcceptance,authored_text,plan_acribic_invitation

BERLIN=ZoneInfo('Europe/Berlin')
SLOT=re.compile(r'\b(\d{2}\.\d{2}\.\d{4}),\s*(\d{2}:\d{2})\b')
REPLY_SLOT=re.compile(r'\b(\d{2}\.\d{2}\.\d{4})(?:,\s*|\s+um\s+|\s+)(\d{2}:\d{2})\b')


def source_hash(source):return hashlib.sha256(source.source_text().encode()).hexdigest()


WEEKDAYS=('Montag','Dienstag','Mittwoch','Donnerstag','Freitag','Samstag','Sonntag')
MONTHS={'januar':1,'februar':2,'märz':3,'maerz':3,'april':4,'mai':5,'juni':6,'juli':7,
        'august':8,'september':9,'oktober':10,'november':11,'dezember':12}
DAY_WORD='|'.join(WEEKDAYS)
MONTH_WORD='|'.join(MONTHS)
DATE_TIME=re.compile(r'\b(?:(?P<weekday>'+DAY_WORD+r'),?\s+(?:(?:der|den)\s+)?)?(?P<day>\d{1,2})\.\s*'
    r'(?:(?P<number>\d{1,2})(?:\.|(?=\s|,|$))|(?P<month>'+MONTH_WORD+r'))\s*(?P<year>20\d{2})?'
    r'\s*,?\s*(?:um\s+)?(?P<hour>\d{1,2}):(?P<minute>\d{2})(?:\s*Uhr)?',re.I)


@dataclass(frozen=True)
class ThreadOfferContext:
    source_time:datetime
    duration_sources:tuple
    fingerprint:str


def thread_offer_context(proposal,sources):
    """Actual complete same-thread originals plus Gmail receipt times, no search.

    Duration inheritance follows only earlier exact-party RFC ancestors. Every
    source and receipt time in the current thread is bound before interpretation.
    """
    from gtm.mail_observer.sources import checked_original
    indexed={};rows=[];by_rfc={}
    parties={proposal.sender.casefold(),*(r.casefold() for r in proposal.recipients)}
    if len(parties)!=2 or CODE not in parties:raise ValueError('Exact proposal parties required')
    for source,stamp in sources:
        checked_original(source)
        if (source.thread_id!=proposal.thread_id or source.account_email!=CODE or
            not isinstance(stamp,str) or not stamp.isdigit() or source.message_id in indexed):
            raise ValueError('Exact complete thread timestamps required')
        indexed[source.message_id]=(source,int(stamp))
        if source.rfc_message_id in by_rfc:raise ValueError('Ambiguous RFC ancestor')
        by_rfc[source.rfc_message_id]=(source,int(stamp))
        rows.append((source.message_id,source_hash(source),stamp))
    if proposal.message_id not in indexed or indexed[proposal.message_id][0]!=proposal:
        raise ValueError('Proposal absent from actual complete thread')
    reference=indexed[proposal.message_id][1];parent=proposal.in_reply_to;seen=set();ancestors=[];previous=reference
    while parent and parent in by_rfc:
        if parent in seen:raise ValueError('Cyclic proposal ancestry')
        seen.add(parent);source,stamp=by_rfc[parent]
        if stamp>=previous:raise ValueError('Proposal ancestor time order changed')
        previous=stamp
        if ({source.sender.casefold(),*(r.casefold() for r in source.recipients)}!=parties or
            (source.sender==CODE and ('SENT' not in source.labels or 'DRAFT' in source.labels)) or
            (source.sender!=CODE and set(source.labels)&{'SENT','DRAFT'})):
            raise ValueError('Proposal ancestor party scope changed')
        ancestors.append(source);parent=source.in_reply_to
    digest=hashlib.sha256(json.dumps(sorted(rows),separators=(',',':')).encode()).hexdigest()
    return ThreadOfferContext(datetime.fromtimestamp(reference/1000,timezone.utc),tuple(ancestors),digest)


def _duration(source):
    body=authored_text(source.body)
    if re.search(r'\b\d+\s*(?:bis|oder|[-–—/])\s*\d+\s*(?:-minütig\w*|Minuten|Min\.?)',body,re.I):
        raise ValueError('A duration range is not an exact duration')
    matches=list(re.finditer(r'\b(\d{1,3})(?:\s*(?:Minuten|Min\.)(?!\w)|-minütige\w*\b)',body,re.I))
    values={int(m.group(1)) for m in matches}
    if len(values)>1 or any(not 1<=v<=120 for v in values):raise ValueError('Conflicting exact durations')
    if not matches:return None
    if any(re.search(r'\b(?:ca\.?|circa|ungefähr|etwa|mindestens|maximal|bis zu)\s*$',body[:m.start()],re.I) for m in matches):
        raise ValueError('Approximate duration requires review')
    return next(iter(values)),{'source_id':source.message_id,'source_hash':source_hash(source),'quote':matches[0].group(0)}


def proposal_starts(proposal,*,source_time=None):
    if not isinstance(proposal,MailEvidence) or not proposal.complete or proposal.account_email!=CODE:
        raise ValueError('Complete CODE proposal source required')
    body=authored_text(proposal.body)
    found=list(DATE_TIME.finditer(body))
    if not found or len(found)>10 or len(re.findall(r'\b\d{1,2}:\d{2}\b',body))!=len(found):
        raise ValueError('All proposed times must have exact authored dates')
    for paragraph in re.split(r'\n\s*\n',body):
        if DATE_TIME.search(paragraph) and re.search(r'\b(?:nicht|leider|belegt|besetzt|stattdessen|falls|wenn)\b',paragraph,re.I):
            raise ValueError('Conditional/negative dated proposal requires review')
    if source_time is not None and (source_time.tzinfo is None or source_time.utcoffset() is None):
        raise ValueError('Actual aware original timestamp required')
    result=[]
    for match in found:
        month=int(match['number']) if match['number'] else MONTHS[match['month'].casefold()]
        year=int(match['year']) if match['year'] else None
        if year is None:
            if source_time is None or not match['weekday']:raise ValueError('Yearless date requires original timestamp and weekday')
            candidates=[]
            for candidate_year in (source_time.astimezone(BERLIN).year,source_time.astimezone(BERLIN).year+1):
                try:stamp=datetime(candidate_year,month,int(match['day']),int(match['hour']),int(match['minute']),tzinfo=BERLIN)
                except ValueError:continue
                if source_time<stamp<=source_time+timedelta(days=90) and WEEKDAYS[stamp.weekday()].casefold()==match['weekday'].casefold():candidates.append(stamp)
            if len(candidates)!=1:raise ValueError('Yearless date is not unique within original future window')
            stamp=candidates[0]
        else:stamp=datetime(year,month,int(match['day']),int(match['hour']),int(match['minute']),tzinfo=BERLIN)
        if match['weekday'] and WEEKDAYS[stamp.weekday()].casefold()!=match['weekday'].casefold():raise ValueError('Proposal weekday mismatch')
        if source_time is not None and not source_time<stamp<=source_time+timedelta(days=90):raise ValueError('Offer outside original future window')
        if (stamp.utcoffset()!=stamp.replace(fold=1).utcoffset() or
            stamp.astimezone(ZoneInfo('UTC')).astimezone(BERLIN).replace(tzinfo=None)!=stamp.replace(tzinfo=None)):
            raise ValueError('Ambiguous/nonexistent local appointment time')
        if any(value==stamp for value,_ in result):raise ValueError('Repeated/ambiguous offer')
        result.append((stamp,match.group(0)))
    return tuple(result)


def proposal_has_slot(proposal,source_time,slot):
    try:return any(stamp==slot for stamp,_ in proposal_starts(proposal,source_time=source_time))
    except ValueError:return False


def offered_slots(proposal,*,source_time=None,duration_sources=()):
    if not isinstance(proposal,MailEvidence) or not proposal.complete or proposal.account_email!=CODE:
        raise ValueError('Complete CODE proposal source required')
    body=authored_text(proposal.body)
    duration=_duration(proposal)
    if duration is None:
        inherited=[]
        for source in duration_sources:
            # An unrelated 'I will reply in 15 minutes' is not meeting duration.
            paragraphs=[text for text in re.split(r'\n\s*\n',authored_text(source.body))
                if re.search(r'\b(?:gespräch\w*|interview\w*|austausch|call|termin\w*)\b',text,re.I)]
            value=_duration(source)
            if value is not None and any(value[1]['quote'] in paragraph for paragraph in paragraphs):inherited.append(value)
        inherited=[item for item in inherited if item is not None]
        if not inherited or len({item[0] for item in inherited})!=1:
            raise ValueError('One explicitly evidenced thread duration required')
        minutes=inherited[0][0];duration_evidence=[item[1] for item in inherited]
    else:minutes,evidence=duration;duration_evidence=[evidence]
    result=[]
    for stamp,quote in proposal_starts(proposal,source_time=source_time):
        identifier=hashlib.sha256((proposal.message_id+'|'+source_hash(proposal)+'|'+stamp.isoformat()+'|'+str(minutes)).encode()).hexdigest()
        result.append({'offer_id':identifier,'start':stamp.isoformat(),'duration_minutes':minutes,
                       'source_quote':quote,'duration_evidence':duration_evidence})
    return tuple(result)


def offers_hash(offers):return hashlib.sha256(json.dumps(offers,sort_keys=True,separators=(',',':')).encode()).hexdigest()


@dataclass(frozen=True)
class AcceptanceProof:
    account:str
    incoming_id:str
    incoming_hash:str
    incoming_version:int
    proposal_id:str
    proposal_hash:str
    proposal_version:int
    thread_id:str
    sender:str
    quote:str
    selected_offer_id:str
    offer_context_hash:str|None=None
    offers_hash:str|None=None


@dataclass(frozen=True)
class SemanticAcceptanceResolver:
    proof:AcceptanceProof
    offer_context:ThreadOfferContext|None=None
    def __call__(self,binding,proposal,incoming):
        from gtm.mail_observer.sources import checked_original,DECLINE
        checked_original(proposal);checked_original(incoming)
        p=self.proof
        if not isinstance(p,AcceptanceProof) or type(p.incoming_version)!=int or type(p.proposal_version)!=int or p.incoming_version!=1 or p.proposal_version!=1:
            raise ValueError('One typed immutable source version required')
        if not isinstance(proposal,MailEvidence) or not isinstance(incoming,MailEvidence) or not proposal.complete or not incoming.complete:
            raise ValueError('Complete actual Gmail sources required')
        if (p.account!=CODE or proposal.account_email!=CODE or incoming.account_email!=CODE or binding.account!='code' or
            p.incoming_id!=incoming.message_id or p.proposal_id!=proposal.message_id or binding.proposal_message_id!=proposal.message_id or
            p.incoming_hash!=source_hash(incoming) or p.proposal_hash!=source_hash(proposal) or
            p.thread_id!=binding.thread_id or incoming.thread_id!=binding.thread_id or proposal.thread_id!=binding.thread_id or
            p.sender.casefold()!=binding.recipient.casefold() or incoming.sender.casefold()!=binding.recipient.casefold() or
            proposal.sender.casefold()!=CODE or set(r.casefold() for r in proposal.recipients)!={binding.recipient.casefold()} or
            set(r.casefold() for r in incoming.recipients)!={CODE} or 'SENT' not in proposal.labels or 'DRAFT' in proposal.labels or
            set(incoming.labels)&{'SENT','DRAFT'} or not proposal.rfc_message_id or incoming.in_reply_to!=proposal.rfc_message_id or
            incoming.message_id==proposal.message_id):raise ValueError('Exact source/hash/version/RFC/actor association required')
        body=authored_text(incoming.body)
        if DECLINE.search(body):raise ValueError('Explicit decline/opt-out requires review')
        if not p.quote.strip() or p.quote not in body:raise ValueError('Exact authored counterparty evidence required')
        if re.search(r'\b(?:nicht|leider|falls|wenn|vielleicht|alternativ|stattdessen|verschieb\w*|ander\w*)\b',body,re.I):
            raise ValueError('Conditional/unclear/counteroffer requires review')
        if self.offer_context is not None:
            if p.offer_context_hash!=self.offer_context.fingerprint:raise ValueError('Live offer context changed')
            offers=offered_slots(proposal,source_time=self.offer_context.source_time,duration_sources=self.offer_context.duration_sources)
            if p.offers_hash!=offers_hash(offers):raise ValueError('Actual source offers changed')
        else:
            if p.offer_context_hash is not None or p.offers_hash is not None:raise ValueError('Offer context missing')
            offers=offered_slots(proposal)
        chosen=[o for o in offers if o['offer_id']==p.selected_offer_id]
        if len(chosen)!=1:raise ValueError('One actually offered appointment must be selected')
        chosen=chosen[0]
        if datetime.fromisoformat(chosen['start'])!=binding.start or chosen['duration_minutes']!=binding.duration_minutes:
            raise ValueError('Binding differs from actual selected offer')
        duration=_duration(incoming)
        if duration is not None and duration[0]!=chosen['duration_minutes']:raise ValueError('Incoming changes the offered duration')
        if re.search(r'(?<![\d:])\b\d{1,2}\s*Uhr\b',body,re.I):raise ValueError('Unsupported incoming hour selector')
        start=datetime.fromisoformat(chosen['start'])
        weekdays=('Montag','Dienstag','Mittwoch','Donnerstag','Freitag','Samstag','Sonntag')
        days=re.findall(r'\b(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)\b',body,re.I)
        if any(day.casefold()!=weekdays[start.weekday()].casefold() for day in days):raise ValueError('Incoming weekday differs from chosen offer')
        if re.search(r'\b(?:morgen|heute|übermorgen|nächste\w*|kommende\w*)\b',body,re.I):raise ValueError('Relative incoming date requires review')
        # Every authored selector must match exactly one actual source offer.
        # Short replies may select an already offered day without repeating its
        # time/year; they cannot manufacture either value.
        date_parts=list(re.finditer(r'\b(?P<day>\d{1,2})\.\s*(?:(?P<number>\d{1,2})(?:\.|(?=\s|,|$))|(?P<month>'+MONTH_WORD+r'))?\s*(?P<year>20\d{2})?',body,re.I))
        bare_days=re.findall(r'\b(?:am|der|den)\s+(\d{1,2})(?![\d.])\b',body,re.I)
        times=re.findall(r'\b\d{1,2}:\d{2}\b',body)
        if len(date_parts)+len(bare_days)>1 or len(times)>1:raise ValueError('Several incoming date/time selectors')
        candidates=list(offers)
        for selector in date_parts:
            def matches(offer):
                value=datetime.fromisoformat(offer['start'])
                month=int(selector['number']) if selector['number'] else MONTHS[selector['month'].casefold()] if selector['month'] else None
                return (value.day==int(selector['day']) and (month is None or value.month==month) and
                        (selector['year'] is None or value.year==int(selector['year'])))
            candidates=[o for o in candidates if matches(o)]
        if bare_days:candidates=[o for o in candidates if datetime.fromisoformat(o['start']).day==int(bare_days[0])]
        if days:candidates=[o for o in candidates if WEEKDAYS[datetime.fromisoformat(o['start']).weekday()].casefold()==days[0].casefold()]
        if times:candidates=[o for o in candidates if datetime.fromisoformat(o['start']).strftime('%H:%M')==times[0].zfill(5)]
        if len(offers)>1 and not (date_parts or bare_days or days or times):raise ValueError('Several offers require an explicit exact selection')
        if len(candidates)!=1 or candidates[0]['offer_id']!=p.selected_offer_id:raise ValueError('Incoming selection differs from actual chosen offer')
        affirmative=re.search(r'\b(?:passt|gerne|bestätig\w*|einverstanden|nehme|nehmen|freue|funktioniert)\b',p.quote,re.I)
        if not affirmative:raise ValueError('Explicit affirmative evidence required')
        accepted=ResolvedAcceptance(binding,incoming.message_id,incoming.sender,incoming.labels,True,incoming,proposal)
        plan_acribic_invitation(binding,accepted,availability='free',own_hold_readback_verified=True)
        return accepted
