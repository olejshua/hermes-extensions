"""Bounded new-email intake for the existing realtime poll, no core monkeypatch.

Only configured existing business mappings are read. Never use realtime's 4k/12
message draft summary as CRM truth. Fetch the full incoming Gmail message under
an exact checked profile, then plan existing GTM phases. No writes occur here.
"""
from __future__ import annotations
import hashlib
from dataclasses import dataclass
from datetime import datetime,timezone
from email.utils import getaddresses,parseaddr
from gtm.coordinator import ACCOUNTS,Identity,Readback
from gtm.twenty import (NotePlan,Statement,TwentyRecordAdapter,TwentyStatusAdapter,
                        ExistingHelpersClient,StageReviewRequired)
from gtm.calendar.confirmation import MailEvidence,authored_text

@dataclass(frozen=True)
class ExistingLeadBinding:
    company_id: str
    source_page_id: str | None
    person_id: str | None=None

class ExistingLeadNotFound(ValueError):
    pass


class ExistingReplyReadClient(ExistingHelpersClient):
    def get(self,kind,rid):
        if kind!='person':return super().get(kind,rid)
        try:
            value=self.helper.request_json('GET',f'{self.helper.TWENTY_URL}/rest/people/{rid}',self._key)['data']['person']
            if not isinstance(value,dict):raise ValueError('Malformed person record')
            return value
        except Exception:raise RuntimeError('Existing Twenty person read unavailable; mapping blocked') from None

    def lookup_existing_sender(self,sender):
        """One bounded exact person-email lookup, not a company/domain dump."""
        sender=sender.strip()
        if '@' not in sender or any(c.isspace() for c in sender):raise ValueError('Exact sender email required')
        escaped=sender.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        query='''query($filter:PersonFilterInput!){people(first:3,filter:$filter){
            pageInfo{hasNextPage} edges{node{id companyId emails{primaryEmail additionalEmails}}}}}'''
        variables={'filter':{'or':[{'emails':{'primaryEmail':{'ilike':escaped}}},
                                   {'emails':{'additionalEmails':{'like':'%'+escaped+'%'}}}]}}
        try:result=self.helper.graphql(self._key,query,variables)['people']
        except Exception:raise RuntimeError('Bounded Twenty person lookup unavailable; no writes') from None
        if result.get('pageInfo',{}).get('hasNextPage'):raise ValueError('Email lookup exceeds bound; mapping review required')
        candidates=[]
        for edge in result.get('edges',[]):
            person=edge['node'];emails=person.get('emails') or {}
            addresses=[emails.get('primaryEmail')]+list(emails.get('additionalEmails') or [])
            if sender.casefold() in {e.casefold() for e in addresses if isinstance(e,str)}:candidates.append(person)
        if not candidates:return self._lookup_source_mailbox(sender,escaped)
        if len(candidates)!=1 or not candidates[0].get('companyId'):
            raise ValueError('Unique exact existing person-to-company match absent')
        person=candidates[0];company=self.get('company',person['companyId'])
        if not company or company.get('id')!=person['companyId']:raise ValueError('Existing person company missing')
        sources=company.get('notionQuellen') or []
        if not isinstance(sources,list):raise ValueError('Source mapping malformed')
        matching=[]
        for source in sources:
            props={**source.get('properties',{}),**source.get('outreach',{})}
            addresses={a.casefold() for _,a in getaddresses([str(props.get('E-Mail') or '')])}
            if sender.casefold() in addresses:matching.append(source)
        selected=matching[0] if len(matching)==1 else sources[0] if len(sources)==1 else None
        return ExistingLeadBinding(company['id'],selected.get('page_id') if selected else None,person['id'])

    def _lookup_source_mailbox(self,sender,escaped):
        # Legacy outreach role mailboxes often have no Person entity. Only an
        # exact email in existing effective source properties proves association.
        query='''query($filter:CompanyFilterInput!){companies(first:3,filter:$filter){
            pageInfo{hasNextPage} edges{node{id name notionQuellen}}}}'''
        try:result=self.helper.graphql(self._key,query,{'filter':{'notionEmails':{'ilike':'%'+escaped+'%'}}})['companies']
        except Exception:raise RuntimeError('Bounded Twenty source mailbox lookup unavailable; no writes') from None
        if result.get('pageInfo',{}).get('hasNextPage'):raise ValueError('Source mailbox lookup exceeds bound')
        companies=[]
        for edge in result.get('edges',[]):
            company=edge['node'];matching=[]
            for source in company.get('notionQuellen') or []:
                props={**source.get('properties',{}),**source.get('outreach',{})}
                addresses={a.casefold() for _,a in getaddresses([str(props.get('E-Mail') or '')])}
                if sender.casefold() in addresses:matching.append(source)
            if matching:companies.append((company,matching))
        if not companies:raise ExistingLeadNotFound('Unique exact existing source mailbox-to-company match absent')
        if len(companies)!=1:raise ValueError('Unique exact existing source mailbox-to-company match absent')
        company,matching=companies[0]
        return ExistingLeadBinding(company['id'],matching[0].get('page_id') if len(matching)==1 else None,None)


def verify_existing_mapping(client,binding:ExistingLeadBinding,sender):
    company=client.get('company',binding.company_id)
    if not company or company.get('id')!=binding.company_id:raise ValueError('Exact existing company required')
    original_sources=company.get('notionQuellen') or []
    if not isinstance(original_sources,list):raise ValueError('Source mapping malformed')
    sources=[s for s in original_sources if s.get('page_id')==binding.source_page_id] if binding.source_page_id else []
    if binding.source_page_id and len(sources)!=1:raise ValueError('Unique exact migrated source required; no company guesses')
    if not binding.source_page_id and not binding.person_id:
        sources=original_sources # ambiguous source can still prove exact company mailbox
    sender=sender.casefold()
    if binding.person_id:
        person=client.get('person',binding.person_id)
        emails=person.get('emails') or {} if person else {}
        addresses=[emails.get('primaryEmail')]+list(emails.get('additionalEmails') or [])
        if (not person or person.get('id')!=binding.person_id or person.get('companyId')!=binding.company_id or
            sender not in {e.casefold() for e in addresses if isinstance(e,str)}):
            raise ValueError('Exact existing person email-to-company relationship required')
    else:
        addresses=set()
        for source in sources:
            props={**source.get('properties',{}),**source.get('outreach',{})}
            addresses.update(a.casefold() for _,a in getaddresses([str(props.get('E-Mail') or '')]))
        if sender not in addresses:raise ValueError('Exact source mailbox required; domain/name matching is insufficient')
    return company


def capture_full_message(w,gmail,row,category,*,max_body_chars=200000):
    expected=ACCOUNTS[category]
    if gmail.users().getProfile(userId='me').execute().get('emailAddress')!=expected:
        raise ValueError('Authenticated Gmail profile mismatch')
    message=gmail.users().messages().get(userId='me',id=row['id'],format='full').execute()
    headers=w.headers(message);body=w.body(message.get('payload',{}))
    sender=parseaddr(headers.get('from',''))[1]
    recipients=tuple(a.casefold() for _,a in getaddresses([headers.get('to',''),headers.get('cc','')]) if a)
    labels=tuple(message.get('labelIds',[]))
    if (message.get('id')!=row['id'] or message.get('threadId')!=row['threadId'] or
        'SENT' in labels or 'DRAFT' in labels or sender.casefold()==expected or expected not in recipients or
        not isinstance(body,str) or not body.strip() or len(body)>max_body_chars):
        raise ValueError('Complete external incoming message under exact poll identity required')
    if not headers.get('message-id'):raise ValueError('Original RFC message identity absent')
    evidence=MailEvidence(expected,message['id'],message['threadId'],sender,recipients,body,labels,
                          headers['message-id'],headers.get('in-reply-to',''),True)
    milliseconds=message.get('internalDate')
    if not isinstance(milliseconds,str) or not milliseconds.isdigit() or milliseconds!=row.get('internalDate'):
        raise ValueError('Exact original Gmail receipt timestamp under poll identity required')
    return evidence,datetime.fromtimestamp(int(milliseconds)/1000,timezone.utc).isoformat()


def classify_exact_body(evidence):
    text=' '.join(authored_text(evidence.body).split()).casefold()
    known={
        'kein interesse.':('Kein Interesse','Der Absender erklärt kein Interesse.'),
        'bitte kontaktieren sie uns nicht mehr.':('Kein Interesse','Der Absender bittet darum, nicht erneut kontaktiert zu werden.'),
        'wir haben interesse an einem gespräch.':('Antwort: Interesse','Der Absender erklärt Interesse an einem Gespräch.'),
        'was genau bieten sie an?':('Antwort: Rueckfrage','Der Absender fragt nach dem konkreten Angebot.'),
    }
    # Other wording is preserved as evidence, not guessed by a second model.
    return known.get(text,(None,'Eingegangene Antwort; inhaltliche Einordnung benötigt Prüfung.'))

@dataclass(frozen=True)
class PreparedReply:
    identity:Identity
    note_plan:NotePlan
    company_snapshot:dict
    binding:ExistingLeadBinding
    stage:str | None
    source:MailEvidence
    def adapters(self,client,*,server_version='v2.42.6',declared_twenty_projects=frozenset({'acribic'})):
        kwargs={'declared_twenty_projects':declared_twenty_projects}
        return {'twenty_note':TwentyRecordAdapter(client,self.note_plan,component='note',**kwargs),
                'twenty_link':TwentyRecordAdapter(client,self.note_plan,component='target',**kwargs),
                'twenty_status':DeferredReplyStatus(self,client,server_version,declared_twenty_projects)}


def prepare_new_reply(coordinator,w,gmail,twenty,row,binding,*,category,
                      declared_twenty_projects=frozenset({'acribic'})):
    if category not in declared_twenty_projects:raise ValueError('No explicit project-to-Twenty mapping')
    alias={'acribic':'code','stealth':'stealth'}[category]
    if row.get('account')!=alias:raise ValueError('Poll account/project mismatch')
    source,occurred=capture_full_message(w,gmail,row,category)
    company=verify_existing_mapping(twenty,binding,source.sender)
    text=source.source_text();identity=Identity(category,source.account_email,'reply',source.message_id,1,hashlib.sha256(text.encode()).hexdigest())
    stage,summary=classify_exact_body(source)
    quote=authored_text(source.body)
    statement=Statement(source.sender,quote,'Gmail Originalnachricht '+source.message_id,'counterparty' if stage else 'unknown')
    note=NotePlan(identity,binding.company_id,text,summary,(statement,),occurred_at=occurred).checked()
    # Durable native GTM source plan before any downstream external write.
    coordinator.plan(identity)
    return PreparedReply(identity,note,company,binding,stage,source)

class ReplySourceAdapter:
    """Read-only phase proof; fetch the full immutable message again if recovering."""
    def __init__(self,prepared,read_again):self.prepared,self.read_again=prepared,read_again
    def readback(self,identity):
        if identity!=self.prepared.identity:raise ValueError('Wrong reply source identity')
        try:source=self.read_again()
        except Exception:return Readback('unknown',identity)
        if (source.message_id!=identity.source_id or source.account_email!=identity.account or
            hashlib.sha256(source.source_text().encode()).hexdigest()!=identity.source_hash):
            return Readback('conflict',identity)
        return Readback('verified',identity,source.message_id)
    def preflight(self,identity):raise ValueError('Source phase never writes')
    def execute_once(self,identity):raise ValueError('Source phase never writes')

class DeferredReplyStatus:
    """Status review cannot prevent the independent Note/link phases from running."""
    def __init__(self,prepared,client,version,declared):
        self.prepared,self.client,self.version,self.declared=prepared,client,version,declared
        self.stage_guard=lambda:True
    def _get(self):
        p=self.prepared
        if not p.binding.source_page_id:raise StageReviewRequired('Source status mapping unresolved; preserve verified Note/link')
        return TwentyStatusAdapter(self.client,p.note_plan,p.company_snapshot,p.binding.source_page_id,
            {'Stage':p.stage} if p.stage and self.stage_guard() else {},server_version=self.version,declared_twenty_projects=self.declared)
    def readback(self,identity):
        if identity!=self.prepared.identity:raise ValueError('Wrong source identity')
        try:return self._get().readback(identity)
        except StageReviewRequired:return Readback('conflict',identity)
    def preflight(self,identity):return self._get().preflight(identity)
    def execute_once(self,identity):return self._get().execute_once(identity)

class ReplyClassificationAdapter:
    """Recompute the strict local classification; no model or external write."""
    def __init__(self,prepared,read_again):self.prepared,self.read_again=prepared,read_again
    def readback(self,identity):
        source=ReplySourceAdapter(self.prepared,self.read_again).readback(identity)
        if source.state!='verified':return source
        stage,summary=classify_exact_body(self.prepared.source)
        if stage!=self.prepared.stage or summary!=self.prepared.note_plan.summary:
            return Readback('conflict',identity)
        return Readback('verified',identity,identity.source_id+':strict-classification')
    def preflight(self,identity):raise ValueError('Classification phase never writes')
    def execute_once(self,identity):raise ValueError('Classification phase never writes')

class RealtimeReplyHook:
    """Reviewable callback for new `tick()` messages, before its seq checkpoint.

    Source/business mappings are explicit, old mailbox backlog is excluded by
    the activation cutoff. It only plans durable jobs; workers execute phases.
    Unmapped messages are ignored without reading their complete/private bodies.
    """
    def __init__(self,coordinator,w,gmail_for_account,twenty,bindings_by_sender,*,cutoff,
                 declared_twenty_projects=frozenset({'acribic'}),lookup_by_sender=None):
        if cutoff.tzinfo is None:raise ValueError('Explicit activation cutoff timezone required')
        self.coordinator,self.w,self.gmail,self.twenty=coordinator,w,gmail_for_account,twenty
        self.bindings={k.casefold():v for k,v in bindings_by_sender.items()}
        self.cutoff=cutoff;self.declared=declared_twenty_projects;self.lookup=lookup_by_sender
    def on_message(self,row):
        category={'code':'acribic','stealth':'stealth'}.get(row.get('account'))
        if category not in self.declared:return {'state':'project_not_mapped','writes_performed':False}
        stamp=row.get('internalDate')
        if not isinstance(stamp,str) or not stamp.isdigit():raise ValueError('Poll receipt timestamp absent')
        if datetime.fromtimestamp(int(stamp)/1000,timezone.utc)<self.cutoff:
            return {'state':'historical_ignored','writes_performed':False}
        sender=parseaddr(row.get('from',''))[1].casefold();binding=self.bindings.get(sender)
        if binding is None and self.lookup is not None:
            binding=self.lookup(sender)
        if binding is None:return {'state':'business_mapping_missing','writes_performed':False}
        prepared=prepare_new_reply(self.coordinator,self.w,self.gmail(row['account']),self.twenty,
            row,binding,category=category,declared_twenty_projects=self.declared)
        return {'state':'planned','identity':prepared.identity,'prepared':prepared,'writes_performed':False}


def make_crm_binding_resolver(client,*,declared_twenty_projects=frozenset({'acribic'})):
    """Shared call callback: exact external-email/person/company, never a model match.

    Unique company with ambiguous migrated source remains Note/link-capable;
    source_page_id=None blocks only status. Caller catches API outages and keeps
    source/archive phases independent. Stealth must be separately declared.
    """
    def crm_binding(emails,business_binding):
        category=business_binding.get('category');account=business_binding.get('account')
        if category not in declared_twenty_projects or ACCOUNTS.get(category)!=account:return {}
        internal={a.casefold() for a in ACCOUNTS.values()}|{'contact-3@primary.example.invalid'}
        external=sorted({str(e).strip().casefold() for e in emails if str(e).strip().casefold() not in internal})
        if not external or len(external)>10:return {}
        bindings=[]
        for sender in external:
            try:bindings.append(client.lookup_existing_sender(sender))
            except ValueError:return {} # none/ambiguous; no guessing another company
        companies={b.company_id for b in bindings}
        if len(companies)!=1:return {}
        company_id=next(iter(companies));company=client.get('company',company_id)
        if not company or company.get('id')!=company_id or not company.get('name'):return {}
        pages={b.source_page_id for b in bindings}
        page=next(iter(pages)) if len(pages)==1 and None not in pages else None
        return {'company_id':company_id,'company':company['name'],'source_page_id':page,'crm_binding_verified':True}
    return crm_binding

class ReplyEnvelopeHook:
    """Fast poll callback: private metadata recipe + existing native intake job.

    No API/model/CRM calls in the mail poller. Call for every event message before
    committing its seq, outside Gmail's `alerted` dedupe; native keys dedupe GTM.
    A separate native worker resolves this recipe through RealtimeReplyHook.
    """
    def __init__(self,coordinator,*,cutoff,declared_twenty_projects=frozenset({'acribic'})):
        import json
        if cutoff.tzinfo is None:raise ValueError('Explicit activation cutoff timezone required')
        self.coordinator,self.cutoff,self.declared=coordinator,cutoff,declared_twenty_projects
        with coordinator.store.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS gtm_reply_envelopes(idempotency_key TEXT PRIMARY KEY, metadata TEXT NOT NULL)')
    def on_message(self,row):
        import json
        category={'code':'acribic','stealth':'stealth'}.get(row.get('account'))
        if category not in self.declared:return {'state':'project_not_mapped','writes_performed':False}
        stamp=row.get('internalDate')
        if not isinstance(stamp,str) or not stamp.isdigit():raise ValueError('Poll receipt timestamp missing')
        if datetime.fromtimestamp(int(stamp)/1000,timezone.utc)<self.cutoff:return {'state':'historical_ignored','writes_performed':False}
        for field in ('id','threadId'):
            value=row.get(field)
            if not isinstance(value,str) or not value or len(value)>256:raise ValueError('Bounded immutable poll identity required')
        sender=parseaddr(row.get('from',''))[1].casefold()
        if not sender or '@' not in sender:raise ValueError('Exact incoming sender metadata required')
        metadata={'account':row['account'],'id':row['id'],'threadId':row['threadId'],
                  'from':sender,'internalDate':stamp} # never subject/snippet/body/auth
        key='gtm_reply_intake:'+hashlib.sha256((row['account']+'|'+row['id']).encode()).hexdigest()
        canonical=json.dumps(metadata,sort_keys=True,separators=(',',':'))
        with self.coordinator.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old=conn.execute('SELECT metadata FROM gtm_reply_envelopes WHERE idempotency_key=?',(key,)).fetchone()
            if old and old['metadata']!=canonical:raise ValueError('Poll source metadata changed under same message ID')
            conn.execute('INSERT OR IGNORE INTO gtm_reply_envelopes VALUES(?,?)',(key,canonical))
        job=self.coordinator.store.enqueue_job(idempotency_key=key,job_kind='gtm_reply_intake')
        return {'state':'intake_queued','job_id':job['job_id'],'writes_performed':False}
    def metadata_for_job(self,job_id):
        import json
        job=self.coordinator.store.get_job(job_id)
        if job['job_kind']!='gtm_reply_intake':raise ValueError('Wrong native job kind')
        with self.coordinator.store.connect() as conn:
            # Native public status deliberately omits private idempotency keys.
            # Join private metadata by the native job ID; no raw content in status.
            row=conn.execute('''SELECT e.metadata FROM gtm_reply_envelopes e
                JOIN lifecycle_jobs j ON j.idempotency_key=e.idempotency_key
                WHERE j.job_id=? AND j.job_kind='gtm_reply_intake' ''',(job_id,)).fetchone()
        if not row:raise ValueError('Intake recipe missing; do not guess source')
        return json.loads(row['metadata'])


def verify_outreach_thread(w,gmail,row,category):
    """Full source direction proof; a company email alone is not outreach proof."""
    expected=ACCOUNTS[category]
    if gmail.users().getProfile(userId='me').execute().get('emailAddress')!=expected:
        raise ValueError('Wrong authenticated outreach account')
    raw=gmail.users().threads().get(userId='me',id=row['threadId'],format='full').execute()
    messages=raw.get('messages')
    if raw.get('id')!=row['threadId'] or not isinstance(messages,list) or not 1<=len(messages)<=200:
        raise ValueError('Complete bounded outreach thread required')
    incoming=[m for m in messages if m.get('id')==row['id']]
    if len(incoming)!=1:raise ValueError('Exact incoming message absent from full thread')
    stamp=row['internalDate'];sender=parseaddr(row['from'])[1].casefold();proofs=[];latest=[]
    for message in messages:
        if message.get('threadId')!=row['threadId']:raise ValueError('Thread membership mismatch')
        date=message.get('internalDate');labels=message.get('labelIds',[])
        if not isinstance(date,str) or not date.isdigit():raise ValueError('Original thread chronology absent')
        if 'DRAFT' in labels:continue
        latest.append((int(date),message['id']))
        headers=w.headers(message);body=w.body(message.get('payload',{}))
        if not isinstance(body,str) or len(body)>200000:raise ValueError('Complete thread body required')
        recipients={a.casefold() for _,a in getaddresses([headers.get('to',''),headers.get('cc','')])}
        if ('SENT' not in labels or parseaddr(headers.get('from',''))[1].casefold()!=expected or
            sender not in recipients or int(date)>=int(stamp)):continue
        own=authored_text(body).casefold()
        # Recognise the existing Acribic interview source, not incidental quoted
        # text, domains, a calendar invite, or a model's guess of business intent.
        if category!='acribic' or not all(t in own for t in ('code university','ted','ausschreib')):continue
        if not headers.get('message-id'):raise ValueError('Sent outreach RFC identity missing')
        source=MailEvidence(expected,message['id'],row['threadId'],expected,tuple(sorted(recipients)),body,
                            tuple(labels),headers['message-id'],headers.get('in-reply-to',''),True)
        proofs.append((message['id'],hashlib.sha256(source.source_text().encode()).hexdigest()))
    if not proofs:raise ValueError('No verified earlier SENT Acribic outreach to exact sender')
    newest=max(date for date,_ in latest)
    current=[mid for date,mid in latest if date==newest]==[row['id']]
    return {'sent_sources':sorted(proofs),'status_current':current}


class ReplyQueueConsumer:
    """Concrete native consumer, isolated from the mail draft/send flow.

    Review-gated execution uses existing Coordinator leases and attempt markers.
    This worker never calls a model, Gmail mutations, calendar mutations or a
    notification route. Failed/uncertain destinations stay available for review.
    """
    def __init__(self,envelopes,w,gmail_for_account,twenty,*,writes_authorized=False,
                 server_version='v2.42.6'):
        self.envelopes=envelopes;self.co=envelopes.coordinator
        self.w,self.gmail,self.twenty=w,gmail_for_account,twenty
        self.writes_authorized=writes_authorized;self.version=server_version
        with self.co.store.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS gtm_reply_business_proofs(job_id TEXT PRIMARY KEY, proof TEXT NOT NULL)')
    def run_one(self):
        import json,uuid
        if not self.writes_authorized:return {'state':'disabled','writes_performed':False}
        self.co.store.detect_stale_jobs()
        with self.co.store.connect() as conn:
            job=conn.execute("SELECT * FROM lifecycle_jobs WHERE job_kind='gtm_reply_intake' AND state IN ('queued','failed','stalled') AND attempts<max_attempts ORDER BY created_at,job_id LIMIT 1").fetchone()
        if not job:return {'state':'idle'}
        job=dict(job);jid=job['job_id'];owner='reply_'+uuid.uuid4().hex
        # Do not acquire/retry an intake while its already-planned downstream
        # phase still has a genuine native owner. Minute ticks must not exhaust
        # the three-attempt intake budget merely waiting for its lease to expire.
        row=self.envelopes.metadata_for_job(jid)
        with self.co.store.connect() as conn:
            sources=conn.execute("SELECT identity FROM gtm_sources WHERE json_extract(identity,'$.source_id')=? AND json_extract(identity,'$.account')=? AND json_extract(identity,'$.kind')='reply' LIMIT 2",
                (row['id'],ACCOUNTS[{'code':'acribic','stealth':'stealth'}[row['account']]])).fetchall()
            for source in sources:
                identity=Identity(**json.loads(source['identity']))
                for phase in ('source','classification','twenty_note','twenty_link','twenty_status'):
                    active=conn.execute("SELECT 1 FROM lifecycle_jobs WHERE job_id=? AND state IN ('running','checkpointed','verifying') AND lease_until>?",
                        (self.co._job_id(identity,phase),datetime.now(timezone.utc).timestamp())).fetchone()
                    if active:return {'state':'deferred_live_phase','writes_performed':False}
        try:
            if job['state']!='queued':self.co.store.retry_job(jid,owner=job['owner'],lease_generation=job['lease_generation'])
            claimed=self.co.store.start_job(jid,owner=owner,lease_seconds=300)
        except ValueError:return {'state':'waiting_owner'}
        gen=claimed['lease_generation']
        try:
            row=self.envelopes.metadata_for_job(jid)
            category={'code':'acribic','stealth':'stealth'}.get(row['account'])
            if category not in self.envelopes.declared:raise ValueError('Project mapping undeclared')
            if datetime.fromtimestamp(int(row['internalDate'])/1000,timezone.utc)<self.envelopes.cutoff:
                raise ValueError('Historical intake forbidden')
            binding=self.twenty.lookup_existing_sender(row['from'])
            gmail=self.gmail(row['account'])
            proof=verify_outreach_thread(self.w,gmail,row,category)
            canonical=json.dumps(proof['sent_sources'],sort_keys=True)
            with self.co.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                old=conn.execute('SELECT proof FROM gtm_reply_business_proofs WHERE job_id=?',(jid,)).fetchone()
                if old and old['proof']!=canonical:raise ValueError('Outreach direction source changed')
                conn.execute('INSERT OR IGNORE INTO gtm_reply_business_proofs VALUES(?,?)',(jid,canonical))
            prepared=prepare_new_reply(self.co,self.w,gmail,self.twenty,row,binding,category=category,
                                      declared_twenty_projects=self.envelopes.declared)
            def source_again():return capture_full_message(self.w,gmail,row,category)[0]
            adapters=prepared.adapters(self.twenty,server_version=self.version,declared_twenty_projects=self.envelopes.declared)
            adapters['source']=ReplySourceAdapter(prepared,source_again)
            adapters['classification']=ReplyClassificationAdapter(prepared,source_again)
            def verify_fresh():
                if ReplySourceAdapter(prepared,source_again).readback(prepared.identity).state!='verified':
                    raise ValueError('Incoming source changed before external write')
                fresh=verify_outreach_thread(self.w,gmail,row,category)
                if json.dumps(fresh['sent_sources'],sort_keys=True)!=canonical:
                    raise ValueError('Original outreach direction changed before external write')
                return fresh
            # An earlier queued response still gets its own linked source Note;
            # newer incoming messages suppress only its obsolete Stage overlay.
            adapters['twenty_status'].stage_guard=lambda:verify_fresh()['status_current']
            class FreshBoundAdapter:
                def __init__(self,adapter):self.adapter=adapter
                def readback(self,identity):
                    try:verify_fresh()
                    except ValueError:return Readback('conflict',identity)
                    except Exception:return Readback('unknown',identity)
                    return self.adapter.readback(identity)
                def preflight(self,identity):verify_fresh();return self.adapter.preflight(identity)
                def execute_once(self,identity):verify_fresh();return self.adapter.execute_once(identity)
            for phase in ('twenty_note','twenty_link','twenty_status'):
                adapters[phase]=FreshBoundAdapter(adapters[phase])
            phases=('source','classification','twenty_note','twenty_link','twenty_status')
            for phase in phases:
                self.co.store.heartbeat(jid,owner=owner,lease_generation=gen,lease_seconds=300)
                result=self.co.run_phase(prepared.identity,phase,adapters[phase])
                if result['state']=='verified_result_recovery_required':
                    result=self.co.recover_verified(prepared.identity,phase,adapters[phase])
                if result['state']!='completed':raise RuntimeError('Phase verification incomplete')
            self.co.store.begin_verification(jid,owner=owner,lease_generation=gen)
            self.co.store.complete_job(jid,owner=owner,lease_generation=gen,verification_evidence='reply_source_note_link_status_verified')
            return {'state':'completed','workflow_id':prepared.identity.key,'review_required':prepared.stage is None}
        except Exception:
            # Exception strings can contain private provider content. Only this
            # fixed error type enters native status; original message stays Gmail.
            self.co.store.fail_job(jid,owner=owner,lease_generation=gen,error_type='reply_review_required')
            return {'state':'review_required','job_id':jid,'write_replayed':False}
