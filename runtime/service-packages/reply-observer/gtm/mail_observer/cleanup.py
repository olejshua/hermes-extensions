"""Bind one verified invitation to the existing immutable cleanup workflow.

No competing hold-deletion algorithm. The active reviewed CleanupObserver,
Observation, ObservedInvitation and FrozenHoldRelease remain the write owners.
This reader fetches complete raw CODE mail and includes fresh draft followups.
"""
from datetime import datetime,timedelta,timezone
from gtm.calendar.confirmation import CODE,MailEvidence
from gtm.calendar.invitation import _get,_pages
from gtm.calendar.routing import ACRIBIC, MANAGED, event_calendar
from gtm.mail_observer.sources import SourceBlocked
import hashlib,json,re
from gtm.calendar.reply_acceptance import proposal_has_slot


# An old private draft-blocker format has no native properties and random IDs.
# Its exact description is evidence, never a synthetic native ownership tag.
DESCRIPTION_HOLD=re.compile(r'Privater30-Minuten-Blocker für Terminangebot im Antwortentwurf an '
    r'([^\s<>@]+@[^\s<>@]+)\. Noch keine bestätigte Zusage\. Prüfkennung:'
    r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')


def description_hold(module,event,recipient):
    match=DESCRIPTION_HOLD.fullmatch(event.get('description') or '')
    if not match or match[1]!=recipient:return None
    a,b=module._interval(event)
    if (not re.fullmatch(r'[0-9a-v]{5,1024}',event.get('id','')) or not event.get('etag') or
        event.get('organizer',{}).get('email')!=CODE or event.get('creator',{}).get('email')!=CODE or
        event.get('status')!='confirmed' or event.get('visibility')!='private' or
        event.get('transparency') not in (None,'opaque') or event.get('attendees') or event.get('attendeesOmitted') or
        event.get('recurrence') or event.get('recurringEventId') or event.get('extendedProperties') or
        b-a!=timedelta(minutes=30)):
        raise SourceBlocked('cleanup_description_hold_ownership')
    return a,match[2]


def description_runtime(module):
    """Add one proof schema, retaining the frozen delete/once/lease engine."""
    class Observed(module.ObservedInvitation):
        def readback(self,identity):
            result=super().readback(identity)
            if result.state!='verified':return result
            try:
                for hold in self.observation.holds:
                    if hold['claim'].get('kind')!='description_slot_hold':continue
                    # Re-read the whole exact thread, including drafts, before
                    # every inherited deletion fence. A newer offer is preserved.
                    source,receipt=self.observer.reader.find('code',hold['thread'],self.observation.recipient,
                        module._dt(hold['claim']['start']),module._dt(self.observation.cutoff))
                    if (source.message_id!=hold['source_id'] or receipt.isoformat()!=hold['source_receipt'] or
                        hashlib.sha256(source.source_text().encode()).hexdigest()!=hold['source_hash']):
                        raise SourceBlocked('cleanup_description_source_changed')
                return result
            except Exception:return module.Readback('conflict',identity)
    class Release(module.FrozenHoldRelease):
        def _proof(self,key):
            proof=super()._proof(key)
            hold=next(h for h in self.observation.holds if h['id']==key)
            claim=hold['claim']
            if claim.get('kind')=='description_slot_hold':
                proof.update(description_event_hash=hold['event_hash'],
                    description_check_id=claim['description_check_id'],
                    description_source_hash=hold['source_hash'])
            return proof
        def _verify_hold(self,event,claim):
            if claim.get('kind')!='description_slot_hold':return super()._verify_hold(event,claim)
            hold=next(h for h in self.observation.holds if h['id']==claim['event_id'])
            if module.digest(module.event_proof(event))!=hold['event_hash']:
                raise SourceBlocked('cleanup_description_revision_changed')
            verified=description_hold(module,event,self.observation.recipient)
            if not verified or verified!=(module._dt(claim['start']),claim['description_check_id']):
                raise SourceBlocked('cleanup_description_claim_changed')
    return Observed,Release


def raw_proposal_reader(module,originals,gmail):
    class Reader(module.ProposalReader):
        def __init__(self):self.begin_cycle()
        def verify_profile(self,alias):
            if alias!='code':raise SourceBlocked('cleanup_account_unmapped')
            originals.profile(gmail)
        @staticmethod
        def legacy_shape(source):
            # Reuse the active cleanup hash contract, including the entire
            # decoded original body. Exact raw bytes remain the reader's source.
            return MailEvidence(source.account_email,source.message_id,source.thread_id,source.sender,
                source.recipients,source.body,source.labels,source.rfc_message_id,source.in_reply_to,True)
        def read(self,alias,thread,mid):
            self.verify_profile(alias);source,stamp=originals.get(gmail,mid,thread_id=thread)
            if source.sender!=CODE or not set(source.labels)&{'SENT','DRAFT'}:raise SourceBlocked('cleanup_proposal_scope')
            return self.legacy_shape(source),datetime.fromtimestamp(int(stamp)/1000,timezone.utc)
        def find(self,alias,thread,recipient,slot,cutoff,mid=None):
            self.verify_profile(alias)
            rows=[self.read(alias,thread,mid)] if mid else [(self.legacy_shape(s),datetime.fromtimestamp(int(stamp)/1000,timezone.utc)) for s,stamp in originals.thread(gmail,thread,include_drafts=True) if s.sender==CODE and set(s.labels)&{'SENT','DRAFT'}]
            matches=[(s,t) for s,t in rows if set(s.recipients)-module.INTERNAL=={recipient} and (module.proposed_slot(s.body,slot) or proposal_has_slot(s,t,slot))]
            if not matches:raise SourceBlocked('cleanup_proposal_missing')
            matches.sort(key=lambda r:r[1],reverse=True)
            if matches[0][1]>cutoff:raise module.AfterCutoff('Fresh followup source preserved')
            if len(matches)>1 and matches[0][1]==matches[1][1]:raise SourceBlocked('cleanup_proposal_ambiguous')
            return matches[0]
    return Reader()


class ExactInvitationCleanup:
    def __init__(self,module,coordinator,calendar,policy,originals,gmail,*,window_start,window_end):
        self.module,self.co,self.calendar,self.policy=module,coordinator,calendar,policy
        self.observer=module.CleanupObserver(coordinator,calendar,policy,raw_proposal_reader(module,originals,gmail),
            window_start=window_start,window_end=window_end,writes_authorized=True)
        self.observed_type,self.release_type=description_runtime(module)
    def _description_snapshot(self,event,events,invitation):
        m=self.module;recipient=m.invitation_party(event);binding=invitation.binding
        if event['organizer']['email'].casefold() not in (CODE,ACRIBIC) or binding.recipient.casefold()!=recipient:
            raise SourceBlocked('cleanup_description_invitation_binding')
        cutoff=m._dt(event['created']);meeting_start,_=m._interval(event);snapshot=[]
        if binding.start!=meeting_start:raise SourceBlocked('cleanup_description_meeting_changed')
        claims=self.policy._read()
        for hold in events:
            verified=description_hold(m,hold,recipient)
            if verified is None:continue
            start,check_id=verified
            if m._dt(hold['created'])>cutoff:continue
            try:source,receipt=self.observer.reader.find('code',binding.thread_id,recipient,start,cutoff)
            except m.AfterCutoff:continue
            if (source.message_id!=binding.proposal_message_id or 'SENT' not in source.labels or
                'DRAFT' in source.labels or not source.rfc_message_id or
                not (m.proposed_slot(source.body,meeting_start) or proposal_has_slot(source,receipt,meeting_start))):
                raise SourceBlocked('cleanup_description_original_proposal_required')
            source_hash=hashlib.sha256(source.source_text().encode()).hexdigest()
            claim={'owner':json.dumps(['code',binding.thread_id,[recipient]],separators=(',',':')),
                'start':start.astimezone(timezone.utc).isoformat(),'event_id':hold['id'],'state':'held_draft_verified',
                'kind':'description_slot_hold','legacy_source_message':source.message_id,
                'legacy_source_hash':source_hash,'description_check_id':check_id,
                'description_event_hash':m.digest(m.event_proof(hold))}
            old=claims.get(hold['id'])
            if old and self.policy.verified_release(old):continue
            if old and any(old.get(k)!=v for k,v in claim.items() if k!='state'):
                raise SourceBlocked('cleanup_description_journal_changed')
            snapshot.append({'id':hold['id'],'event_hash':m.digest(m.event_proof(hold)),
                'alias':'code','thread':binding.thread_id,'source_id':source.message_id,'source_hash':source_hash,
                'source_receipt':receipt.isoformat(),'legacy':True,'claim':claim})
        return snapshot
    def run(self,invitation,identity):
        if invitation.readback(identity).state!='verified':raise SourceBlocked('invitation_readback_required')
        cid=getattr(invitation,'calendar_id',CODE)
        event=_get(self.calendar,invitation.event_id,cid)
        if event and cid!=CODE:event['_hermes_calendar_id']=cid
        with self.co.store.connect() as c:
            old=c.execute('SELECT observation FROM gtm_hold_observations WHERE event_id=?',(invitation.event_id,)).fetchone()
        if old:
            data=json.loads(old['observation']);data['holds']=tuple(data['holds']);observation=self.module.Observation(**data)
        else:
            events=[]
            for calendar_id in MANAGED:
                for row in _pages(self.calendar,calendarId=calendar_id,timeMin=self.observer.window[0].isoformat(),
                        timeMax=self.observer.window[1].isoformat(),singleEvents=True,maxResults=250):
                    if calendar_id!=CODE:row['_hermes_calendar_id']=calendar_id
                    events.append(row)
            if len({r.get('id') for r in events})!=len(events):raise SourceBlocked('cleanup_calendar_identity_ambiguous')
            if len(events)>1000:raise SourceBlocked('cleanup_event_scope_review')
            holds=[e for e in events if e.get('extendedProperties',{}).get('private',{}).get('hermesSlotHold')=='1']
            if len(holds)>100:raise SourceBlocked('cleanup_hold_scope_review')
            extra=self._description_snapshot(event,events,invitation)
            if len(holds)+len(extra)>100:raise SourceBlocked('cleanup_hold_scope_review')
            observation=self.observer.candidate(event,holds) if holds else None
            if extra:
                data=observation.serial() if observation else dict(event_id=event['id'],
                    event_hash=self.module.digest(self.module.event_proof(event)),recipient=self.module.invitation_party(event),
                    cutoff=event['created'],anchor_alias='code',meeting_start=self.module._interval(event)[0].astimezone(timezone.utc).isoformat(),
                    holds=(),review_count=0)
                if event_calendar(event)!=CODE:data['calendar_id']=event_calendar(event)
                data={**data,'holds':tuple(sorted((*data['holds'],*extra),key=lambda h:h['id']))}
                observation=self.module.Observation(**data)
            if observation is None:
                claim=self.policy._read().get(invitation.hold_id)
                if claim and self.policy.verified_release(claim) and claim['release'].get('invitation_event_id')==invitation.event_id:
                    return {'state':'completed','reused_existing_cleanup':True}
                raise SourceBlocked('cleanup_original_hold_review')
            with self.policy._locked():
                with self.co.store.connect() as c:
                    c.execute('INSERT OR IGNORE INTO gtm_hold_observations VALUES(?,?)',(observation.event_id,json.dumps(observation.serial(),sort_keys=True)))
                    frozen=c.execute('SELECT observation FROM gtm_hold_observations WHERE event_id=?',(observation.event_id,)).fetchone()
                data=json.loads(frozen['observation']);data['holds']=tuple(data['holds']);observation=self.module.Observation(**data)
                claims=self.policy._read()
                for hold in observation.holds:
                    if hold['id'] not in claims:claims[hold['id']]=hold['claim']
                self.policy._save(claims)
        observed=self.observed_type(self.observer,observation);oid=observed.identity;self.co.plan(oid)
        with self.co.store.connect() as c:
            c.execute('INSERT OR IGNORE INTO gtm_hold_observation_jobs VALUES(?,?)',(observation.event_id,self.co._job_id(oid,'hold_release')))
        for phase,adapter in [('source',observed),('invitation',observed),('hold_release',self.release_type(observed))]:
            result=self.co.run_phase(oid,phase,adapter)
            if result['state']=='verified_result_recovery_required':result=self.co.recover_verified(oid,phase,adapter)
            if result['state']!='completed':return {'state':'readback_required','phase':phase}
        return {'state':'completed','source_hash':oid.source_hash}


class ExternalInvitationCleanup:
    """Existing Gmail event -> exact external invitation -> frozen native release.

    A separate, reviewed cleanup grant is required; invitation creation stays
    disabled. Only managed, journaled, exact-party Acribic proposals are in scope.
    This does not scan on a timer or infer an invitation from an unsent draft.
    """
    def __init__(self,module,coordinator,calendar,policy,originals,gmail,*,writes_authorized=False):
        self.module,self.co,self.calendar,self.policy=module,coordinator,calendar,policy
        self.originals,self.gmail,self.writes_authorized=originals,gmail,writes_authorized
        now=datetime.now(timezone.utc)
        # Reuse the native observation tables and lease/attempt journal owner.
        self.observer=module.CleanupObserver(coordinator,calendar,policy,None,
            window_start=now-timedelta(days=30),window_end=now+timedelta(days=90))

    def _source(self,mid,thread=None):
        self.originals.profile(self.gmail)
        try:return self.originals.get(self.gmail,mid,thread_id=thread)
        except SourceBlocked as error:
            if error.code!='original_authority_review':raise
            return self._own_google_reply_source(mid,thread)

    def _own_google_reply_source(self,mid,thread):
        """Dedicated own SENT Google transport; never relax incoming authority."""
        import base64
        from email import policy as email_policy
        from email.parser import BytesParser
        from email.utils import getaddresses,parseaddr
        from gtm.mail_observer.sources import OriginalMail,mime_text
        from gtm.calendar.reconciliation import own_calendar_reply_headers
        row=self.gmail.users().messages().get(userId='me',id=mid,format='raw').execute()
        labels=tuple(row.get('labelIds',[]));encoded=row.get('raw')
        if (row.get('id')!=mid or(thread and row.get('threadId')!=thread)or 'SENT'not in labels or 'DRAFT'in labels
            or not isinstance(encoded,str)or not encoded or len(encoded)>4*((self.originals.max_bytes+2)//3)):
            raise SourceBlocked('own_calendar_reply_source_scope')
        raw=base64.b64decode(encoded+'='*(-len(encoded)%4),altchars=b'-_',validate=True)
        if len(raw)>self.originals.max_bytes:raise SourceBlocked('original_size_review')
        message=BytesParser(policy=email_policy.default).parsebytes(raw)
        if message.defects:raise SourceBlocked('original_mime_review')
        headers=own_calendar_reply_headers([(name,str(value))for name,value in message.items()])
        body,attachments=mime_text(message);stamp=row.get('internalDate')
        recipients=tuple(sorted({e.casefold()for _,e in getaddresses([headers[k]for k in ('to','cc','bcc')if headers.get(k)])if e}))
        if not body.strip()or not isinstance(stamp,str)or not stamp.isdigit()or not recipients:
            raise SourceBlocked('own_calendar_reply_content_or_timestamp')
        source=OriginalMail(CODE,mid,row['threadId'],parseaddr(headers['from'])[1].casefold(),recipients,body,labels,
            headers['message-id'].strip(),headers.get('in-reply-to','').strip(),True,base64.b64encode(raw).decode(),
            headers.get('auto-submitted','no'),headers.get('list-unsubscribe',''),headers.get('precedence',''),
            headers.get('subject',''),tuple(attachments))
        if not self._request_uid(source,'REPLY'):raise SourceBlocked('own_calendar_reply_mime_required')
        return source,stamp

    @staticmethod
    def _request_uid(source,method='REQUEST'):
        import base64
        from email import policy as email_policy
        from email.parser import BytesParser
        message=BytesParser(policy=email_policy.default).parsebytes(base64.b64decode(source.raw_b64,validate=True))
        uids=set()
        for part in message.walk():
            if part.get_content_type()!='text/calendar' and not (part.get_filename()or'').lower().endswith('.ics'):continue
            text=part.get_payload(decode=True).decode(part.get_content_charset()or'utf-8')
            lines=re.sub(r'\r?\n[ \t]','',text).splitlines()
            if [line for line in lines if line.startswith('METHOD:')]!=['METHOD:'+method]:return None
            uids.update(line[4:]for line in lines if line.startswith('UID:'))
        if not uids:return None
        if len(uids)!=1:raise SourceBlocked('external_calendar_request_uid_review')
        return uids.pop()

    def _canonical_request(self,event):
        """Resume a waiting case first; bounded original search handles backfill."""
        from gtm.calendar.reconciliation import calendar_request
        uid=event['iCalUID'];matches=[]
        with self.co.store.connect()as c:
            saved=c.execute("SELECT message_id,source_hash FROM gtm_reply_cases WHERE state='waiting_calendar_acceptance' AND json_extract(payload,'$.calendar_uid')=?",(uid,)).fetchall()if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gtm_reply_cases'").fetchone()else[]
        saved_hashes={r['message_id']:r['source_hash']for r in saved}
        if saved:
            ids=[r['message_id']for r in saved]
        else:
            sender=event['organizer']['email'].casefold()
            if not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+',sender):raise SourceBlocked('external_calendar_sender_review')
            # Receipt time is not identity: Google/imported calendar creation
            # can precede legitimate email delivery by days. Exact raw UID,
            # parties/sequence/interval plus the original SENT offer bind it.
            query='from:'+sender
            ids=[];token=None;seen=set()
            for _ in range(20):
                result=self.gmail.users().messages().list(userId='me',q=query,maxResults=100,**({'pageToken':token}if token else{})).execute()
                ids.extend(r['id']for r in result.get('messages',[]));token=result.get('nextPageToken')
                if not token:break
                if token in seen:raise SourceBlocked('external_calendar_original_pagination_review')
                seen.add(token)
            else:raise SourceBlocked('external_calendar_original_scope_review')
            if len(ids)>100:raise SourceBlocked('external_calendar_original_scope_review')
        for mid in ids:
            source,stamp=self._source(mid)
            if mid in saved_hashes and hashlib.sha256(source.source_text().encode()).hexdigest()!=saved_hashes[mid]:
                raise SourceBlocked('external_calendar_waiting_request_changed')
            if source.sender.casefold()!=event['organizer']['email'].casefold()or set(source.labels)&{'SENT','DRAFT'}:continue
            if self._request_uid(source)!=uid:continue
            try:calendar_request(source,event)
            except ValueError:continue
            matches.append((source,stamp))
        if len(matches)!=1:raise SourceBlocked('external_calendar_original_request_missing_or_ambiguous')
        return matches[0]

    @staticmethod
    def _project(sources,proposal_id):
        from gtm.calendar.routing import project_from_thread
        from gtm.mail_observer.sources import checked_original
        ordered=sorted(sources,key=lambda pair:int(pair[1]));packet=[]
        for source,stamp in ordered:
            if source.message_id==proposal_id:break
            if 'DRAFT'in source.labels:continue
            h=checked_original(source)
            packet.append(dict(thread_id=source.thread_id,labels=list(source.labels),text=source.body,
                **{'from':h.get('from',''),'to':h.get('to',''),'cc':h.get('cc',''),
                   'references':h.get('references',''),'in_reply_to':source.in_reply_to,
                   'rfc_message_id':source.rfc_message_id,'internalDate':stamp}))
        return project_from_thread('code',packet)

    def _release_compatible(self,hold,recipient,event_id):
        from gtm.calendar.routing import claim_calendar
        candidate=dict(hold['claim']);candidate['state']='released_after_verified_invitation'
        candidate['release']=dict(attempted=True,verified=True,account=CODE,recipient=recipient,
            hold_id=hold['id'],invitation_event_id=event_id,source_hash='0'*64)
        cid=claim_calendar(candidate)
        if cid!=CODE:candidate['release']['calendar_id']=cid
        return self.policy.verified_release(candidate)

    def preview(self,message_id,thread_id=None):
        from gtm.calendar.reconciliation import calendar_request,calendar_reply,calendar_status,proposal_relation,owner_binding
        from gtm.calendar.routing import claim_calendar
        source,stamp=self._source(message_id,thread_id)
        own_reply=source.sender==CODE and 'SENT'in source.labels and 'DRAFT'not in source.labels
        uid=self._request_uid(source,'REPLY'if own_reply else'REQUEST')
        if not uid:return None
        self.policy._calendars(self.calendar)
        found=[]
        for cid in MANAGED:
            for event in _pages(self.calendar,calendarId=cid,iCalUID=uid,maxResults=100):
                if event.get('iCalUID')!=uid:raise SourceBlocked('external_calendar_uid_query_mismatch')
                if cid!=CODE:event['_hermes_calendar_id']=cid
                found.append(event)
        if not found:raise SourceBlocked('external_calendar_event_not_imported')
        if len(found)!=1:raise SourceBlocked('external_calendar_event_ambiguous')
        event=found[0]
        trigger=None
        if own_reply:
            if calendar_status(event)=='invited_awaiting_response':raise SourceBlocked('external_calendar_acceptance_not_imported')
            trigger=calendar_reply(source,event)
            source,stamp=self._canonical_request(event)
        status=calendar_status(event)
        request=calendar_request(source,event,require_accepted=False)
        if status!='confirmed':
            return {'state':'waiting_calendar_acceptance'if status=='invited_awaiting_response'else'declined_preserved',
                'source_hash':request['source_hash'],'source_message_id':source.message_id,
                'calendar_uid':event['iCalUID'],'event_id':event['id'],'eligible_holds':0}
        with self.co.store.connect()as c:
            old=c.execute('SELECT observation FROM gtm_hold_observations WHERE event_id=?',(event['id'],)).fetchone()
        if old:
            data=json.loads(old['observation']);data['holds']=tuple(data['holds'])
            observation=self.module.Observation(**data)
            if not observation.holds or any(h.get('external_request')!=request for h in observation.holds):
                raise SourceBlocked('external_calendar_existing_observation_changed')
            return {'observation':observation,'event':event,'execution_ready':all(
                self._release_compatible(h,observation.recipient,event['id'])for h in observation.holds),'reused':True}
        snapshot=[];preserved=[];threads={};claims=self.policy._read()
        for key,claim in claims.items():
            if self.policy.verified_release(claim)or claim.get('state')!='held_draft_verified':continue
            try:alias,tid,recipients=json.loads(claim['owner'])
            except (KeyError,TypeError,ValueError):continue
            if alias!='code'or recipients!=[source.sender.casefold()]or claim.get('project')!='acribic':continue
            if len(snapshot)+len(preserved)>=100:raise SourceBlocked('external_calendar_hold_scope_review')
            try:
                cid=claim_calendar(claim);hold=_get(self.calendar,key,cid)
                if not hold:raise ValueError('owned_hold_missing_without_attempt')
                if cid!=CODE:hold['_hermes_calendar_id']=cid
                self.policy._verify(hold,claim);owner_binding(hold)
                start,_=self.module._interval(hold)
                expected=hashlib.sha256((claim['owner']+'|'+start.astimezone(timezone.utc).isoformat()).encode()).hexdigest()[:40]
                if (key!=claim.get('event_id')or key!=expected or not hold.get('etag')or
                    hold.get('organizer',{}).get('email')!=cid or hold.get('creator',{}).get('email')!=CODE or
                    hold.get('attendeesOmitted')or hold.get('recurrence')or hold.get('recurringEventId')or
                    self.module._dt(hold['created'])>self.module._dt(event['created'])or claim.get('release')):
                    raise ValueError('managed_hold_identity_or_cutoff_review')
                if tid not in threads:threads[tid]=self.originals.thread(self.gmail,tid,include_drafts=True)
                relation=proposal_relation(hold,event,threads[tid])
                if relation['state']!='obsolete_proposal':
                    preserved.append({'id':key,'reason':relation['reason'],'state':relation['state']});continue
                if self._project(threads[tid],relation['proposal_message_id'])!='acribic':
                    raise ValueError('original_business_project_required')
                snapshot.append(dict(id=key,event_hash=self.module.digest(self.module.event_proof(hold)),
                    alias='code',thread=tid,source_id=relation['proposal_message_id'],
                    source_hash=relation['proposal_source_hash'],source_receipt=relation['proposal_receipt'],
                    legacy=False,claim=claim,proof_hold=hold,external_request=request,external_trigger=trigger,relation=relation))
            except Exception:
                preserved.append({'id':key,'reason':'source_or_hold_binding_requires_review','state':'preserve'})
        if not snapshot:return {'observation':None,'event':event,'request':request,'preserved':preserved,'execution_ready':True}
        observation=self.module.Observation(event['id'],self.module.digest(self.module.event_proof(event)),
            source.sender.casefold(),event['created'],'code',self.module._interval(event)[0].astimezone(timezone.utc).isoformat(),
            tuple(sorted(snapshot,key=lambda h:h['id'])),0,event_calendar(event))
        return {'observation':observation,'event':event,'preserved':preserved,'execution_ready':all(
            self._release_compatible(h,observation.recipient,event['id'])for h in snapshot)}

    def _adapter(self,observation):
        parent=self;m=self.module
        class Observed(m.ObservedInvitation):
            def readback(self,identity):
                self._identity(identity)
                from gtm.calendar.reconciliation import calendar_request,proposal_relation
                try:
                    event=_get(parent.calendar,self.event_id,self.calendar_id)
                    if event and self.calendar_id!=CODE:event['_hermes_calendar_id']=self.calendar_id
                    if not event or m.digest(m.event_proof(event))!=observation.event_hash:return m.Readback('conflict',identity)
                    threads={}
                    for hold in observation.holds:
                        req=hold['external_request'];source,_=parent._source(req['message_id'],req['thread_id'])
                        if calendar_request(source,event)!=req:return m.Readback('conflict',identity)
                        if hold.get('external_trigger'):
                            from gtm.calendar.reconciliation import calendar_reply
                            trigger,_=parent._source(hold['external_trigger']['message_id'])
                            if calendar_reply(trigger,event)!=hold['external_trigger']:return m.Readback('conflict',identity)
                        if hold['thread']not in threads:
                            threads[hold['thread']]=parent.originals.thread(parent.gmail,hold['thread'],include_drafts=True)
                        if proposal_relation(hold['proof_hold'],event,threads[hold['thread']])!=hold['relation']:
                            return m.Readback('conflict',identity)
                    return m.Readback('verified',identity,self.event_id)
                except (KeyError,TypeError,ValueError):return m.Readback('conflict',identity)
                except Exception:return m.Readback('unknown',identity)
        return Observed(self.observer,observation)

    def run(self,message_id,thread_id=None):
        if not self.writes_authorized:return {'state':'disabled','saas_writes':0}
        plan=self.preview(message_id,thread_id)
        if plan is None:return None
        if plan.get('state')in ('waiting_calendar_acceptance','declined_preserved'):return plan
        observation=plan['observation']
        if observation is None:return {'state':'completed','eligible_holds':0,'preserved':plan.get('preserved',[]),
            'source_hash':plan['request']['source_hash'],'source_message_id':plan['request']['message_id'],
            'request_source_hash':plan['request']['source_hash']}
        if not plan['execution_ready']:raise SourceBlocked('external_invitation_release_policy_review')
        with self.policy._locked():
            with self.co.store.connect()as c:
                c.execute('INSERT OR IGNORE INTO gtm_hold_observations VALUES(?,?)',
                    (observation.event_id,json.dumps(observation.serial(),sort_keys=True)))
                frozen=c.execute('SELECT observation FROM gtm_hold_observations WHERE event_id=?',(observation.event_id,)).fetchone()
            data=json.loads(frozen['observation']);data['holds']=tuple(data['holds'])
            observation=self.module.Observation(**data)
        invitation=self._adapter(observation);identity=observation.identity;self.co.plan(identity)
        with self.co.store.connect()as c:
            c.execute('INSERT OR IGNORE INTO gtm_hold_observation_jobs VALUES(?,?)',
                (observation.event_id,self.co._job_id(identity,'hold_release')))
        for phase,adapter in [('source',invitation),('invitation',invitation),('hold_release',self.module.FrozenHoldRelease(invitation))]:
            result=self.co.run_phase(identity,phase,adapter)
            if result['state']=='verified_result_recovery_required':result=self.co.recover_verified(identity,phase,adapter)
            if result['state']!='completed':return {'state':'readback_required','phase':phase,'writes_replayed':False}
        return {'state':'completed','eligible_holds':len(observation.holds),'source_hash':identity.source_hash,
            'source_message_id':observation.holds[0]['external_request']['message_id'],
            'request_source_hash':observation.holds[0]['external_request']['source_hash'],'preserved':plan.get('preserved',[])}

    def on_message(self,row):
        if not self.writes_authorized:return None
        if row.get('account')!='code':raise SourceBlocked('external_calendar_account_review')
        # The existing Gmail-history intake invokes this once per actual new
        # incoming source. No independent calendar sweeper or model is added.
        return self.run(row['id'],row['threadId'])
