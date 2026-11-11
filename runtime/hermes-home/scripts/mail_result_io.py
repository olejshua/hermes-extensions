"""Read native service evidence; settle only positively verified local receipts."""
from __future__ import annotations
import hashlib
import json
import uuid
import fcntl
from contextlib import contextmanager
from datetime import datetime,timedelta
from email.utils import getaddresses,parseaddr

def normalized_thread(thread):
    keys=('id','thread_id','from','to','cc','internalDate','rfc_message_id','in_reply_to','references','text','labels','reference_source')
    # Reading an original does not change its meaning or reply lineage.
    # Keep every other label, including SENT/SPAM/TRASH, in the source binding.
    return [{**{k:m.get(k)for k in keys if k!='labels'},
             'labels':sorted(set(m.get('labels',[]))-{'UNREAD'})}
            for m in thread if'DRAFT'not in m.get('labels',[])]

def body_key(text):return hashlib.sha256(text.replace('\r\n','\n').rstrip('\n').encode()).hexdigest()

def owned_recipient_set(case,recipients):
    stored=[*(case.get('recipients')or[]),*(case.get('copied')or[])]
    if not stored and(case.get('outcome')or{}).get('to'):
        stored=[a for _,a in getaddresses([(case['outcome']['to'])])if a]
    return not stored or sorted({a.casefold()for a in stored})==sorted({a.casefold()for a in recipients})

def attempt_generation(item,*,runtime=None):
    if runtime is None:import email_realtime as runtime
    if not all(item.get(key)for key in('account','id','threadId')):raise ValueError('write_source_binding_missing')
    return runtime.read_case(item).get('write_generation',0)

def require_write_admission(case,owner):
    from email_watcher import DraftStateUnverified
    if ((case.get('write_commit')or{}).get('state')=='pending'
            or(case.get('outcome')or{}).get('status')=='needs_manual_review_draft_state'
            or(case.get('phase')in{'draft_write_pending','draft_update_pending'}and(
                not owner or owner!=case.get('write_owner')))):
        raise DraftStateUnverified('Prior write owner is unresolved; read back instead of replaying')

@contextmanager
def write_commit_lock(item,runtime):
    # Separate from the long-lived outer workflow lock, so every direct native
    # writer participates without recursively locking its parent's case lock.
    path=runtime.case_path(item).with_suffix('.write.lock')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.is_symlink():raise ValueError('write_admission_lock_unverified')
    with path.open('a')as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        yield

def remember_review(item,runtime,generation,owner,**changes):
    from email_watcher import DraftStateUnverified
    with write_commit_lock(item,runtime):
        current=runtime.read_case(item);require_write_admission(current,owner)
        if current.get('write_generation',0)!=generation:
            raise DraftStateUnverified('A write committed after this review began; inspect its result')
        runtime.remember_case(item,**changes)

def review_before_write(item,thread,text,slots,recipients,*,project=None,write_owner=None,followup=False,runtime=None,primary_recipients=None):
    """Full current source + actual final body + fresh calendar, before any hold.

    The model judges meaning, including all offered starts. A source/body-bound
    receipt is reusable only for identical input and fresh calendar evidence.
    A caller cannot supply a success flag or a receipt to bypass this gate.
    """
    if runtime is None:import email_realtime as runtime
    import mail_policy as policy
    from mail_workflow import calendar_facts,validate_understanding,timestamp
    from mail_outcomes import assess,fingerprint,OutcomeReviewError
    case=runtime.read_case(item)
    require_write_admission(case,write_owner)
    with write_commit_lock(item,runtime):
        case=runtime.read_case(item);require_write_admission(case,write_owner)
        generation=case.get('write_generation',0)
    if (not thread or thread[-1]['id']!=item['id']or any(message.get('truncated')for message in thread)):
        raise policy.MailPolicyReviewRequired('Complete current source required for semantic write review')
    if getattr(runtime,'closed_source_lineage',lambda *args:False)(item['account'],item['threadId'],thread):
        from email_watcher import SourceChangedBeforeDraft
        raise SourceChangedBeforeDraft('deliberately_closed_source_lineage')
    now=datetime.now(policy.BERLIN);same_source=normalized_thread(case.get('original_thread',[]))==normalized_thread(thread)
    owner=(item['account'],item['threadId'],recipients)
    calendar_source=policy.CalendarSource(item['account'],item['threadId'],thread,primary_recipients=primary_recipients)
    available=calendar_source.wrap(policy.own_hold_available)
    exact_check=calendar_source.wrap(policy._check)
    understanding=case.get('understanding')if same_source else None
    if understanding and understanding.get('invitation_requested') and 'invitation_recipients' not in understanding: understanding=None
    incoming=thread[-1]
    received=datetime.fromtimestamp(int(incoming['internalDate'])/1000,policy.BERLIN)if incoming.get('internalDate')else now
    if not understanding:
        packet={'account':item['account'],'project':project,'current_message':item['id'],
            'thread':thread,'received_at':received.isoformat(),'processing_at':now.isoformat()}
        understanding=validate_understanding(runtime.model_json('understand',packet),incoming.get('text')or'',received)
    def options(now=None,window=None,availability=None):
        values=list(runtime.calendar_options(now=now,window=window,availability=availability))
        for value in slots:
            dt=policy.start(value)
            if (window and not(window[0]<=dt and dt+timedelta(minutes=30)<=window[1]))or dt<now+timedelta(minutes=45):continue
            if available(runtime.calendar_service(),*owner,dt)is True:
                if value not in{choice['start']for choice in values}:values.append({'start':value,'label':policy.label(value)})
        return values
    if understanding:
        calendar=calendar_facts(understanding,now=now,owner=owner,
            exact=calendar_source.wrap(runtime.check_incoming_availability),options=calendar_source.wrap(options),evidence=calendar_source.evidence)
    else:calendar={'choices':[],'relation':'none','checked_offers':0}
    for value in slots:
        dt=policy.start(value)
        if dt<now+timedelta(minutes=45):raise policy.MailPolicyReviewRequired('Proposed start expired before write')
        offers=understanding.get('offers')or[]
        valid_offer=not offers
        for offer in offers:
            a=timestamp(offer['start']);b=timestamp(offer['end'])if offer.get('end')else None
            if offer.get('kind')=='window'and b is None:b=(a+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
            if (b and a<=dt and dt+timedelta(minutes=30)<=b)or(not b and dt==a):valid_offer=True
        bounds=understanding.get('availability')or{}
        if (not valid_offer or(bounds.get('not_before')and dt<timestamp(bounds['not_before']))
                or(bounds.get('not_after')and dt+timedelta(minutes=30)>timestamp(bounds['not_after']))
                or any(timestamp(interval['start'])<dt+timedelta(minutes=30)and timestamp(interval['end'])>dt
                       for interval in bounds.get('excluded',[]))):
            raise policy.MailPolicyReviewRequired('Declared proposal is outside the source-bound availability')
        service=runtime.calendar_service()
        if available(service,*owner,dt)is not True:
            exact_check(service,dt,policy._calendars(service))
        if value not in{choice['start']for choice in calendar['choices']}:calendar['choices'].append({'start':value,'label':policy.label(value)})
    if slots:calendar['relation']='within_offer'if understanding.get('offers')else'request'
    old=case.get('standard_context')or{}
    age=None
    try:
        processed=datetime.fromisoformat(old['processing_at'])
        age=(now-processed).total_seconds()
    except (KeyError,ValueError,TypeError):pass
    reusable=(same_source and age is not None and 0<=age<=300 and processed.date()==now.date()
        and body_key(old.get('draft')or'')==body_key(text)
        and normalized_thread(old.get('thread',[]))==normalized_thread(thread)
        and (old.get('hard_facts')or{}).get('proposed_slots',[])==list(slots))
    context=(dict(old)if reusable else{'account':item['account'],'project':project,'current_message':item['id'],
        'thread':thread,'received_at':received.isoformat(),'processing_at':now.isoformat(),'review_phase':'candidate',
        'user_question':None,'business_context':case.get('business_context')if same_source else None,
        'candidate_understanding':understanding})
    context.update(draft=text,require_time_claims=True)
    if project=='acribic'and not context.get('business_context'):
        from mail_business import research_context,THREAD_ID,SOURCE_ID
        sources,omitted=runtime.get_thread('code',THREAD_ID)
        matches=[message for message in sources if message['id']==SOURCE_ID]
        if omitted or len(matches)!=1:raise policy.MailPolicyReviewRequired('Complete business source unavailable')
        context['business_context']=research_context(lambda:matches[0],thread)
    if followup:context['user_authorized_followup']=True
    context['hard_facts']={'calendar':calendar,'saved_in_gmail':False,'proposed_slots':list(slots)}
    try:receipt=assess(context,runtime.model_json,cached=case.get('result_standard')if reusable else None)
    except OutcomeReviewError as error:
        remember_review(item,runtime,generation,write_owner,standard_review_error=error.code,
            standard_review_failure={'context':context,'rejected':getattr(error,'rejected',[])},
            **({'standard_verified':False}if not case.get('write_review')else{}))
        raise policy.MailPolicyReviewRequired('Semantic write claims could not be verified')from error
    proof={'contract':'mail-semantic-write-v1','body_sha256':body_key(text),
           'source_sha256':fingerprint(normalized_thread(thread)),'slots':list(slots),
           'context_key':receipt['input_key'],'receipt':receipt}
    if receipt['review']['result']!='ready':
        attempts=[*case.get('standard_attempts',[]),{'candidate':{'body':text,'proposed_slots':list(slots)},'receipt':receipt}]
        remember_review(item,runtime,generation,write_owner,last_write_review={'proof':proof,'context':context},standard_attempts=attempts,
            **({'standard_verified':False,'result_standard':receipt,'standard_context':context}if not case.get('write_review')else{}))
        raise policy.MailPolicyReviewRequired('Actual final reply does not meet the semantic result standard')
    remember_review(item,runtime,generation,write_owner,write_review=proof,standard_context=context,result_standard=receipt,standard_verified=True)
    return {'owner':write_owner or uuid.uuid4().hex,'proof':proof,'understanding':understanding,'write_generation':generation}

def begin_owned_write(item,thread,text,slots,recipients,review,*,revision=None,runtime=None):
    if runtime is None:import email_realtime as runtime
    from email_watcher import DraftStateUnverified
    from mail_outcomes import fingerprint
    with write_commit_lock(item,runtime):
        current=runtime.read_case(item);require_write_admission(current,review['owner'])
        if current.get('write_generation',0)!=review.get('write_generation'):
            raise DraftStateUnverified('Stale review cannot replace a newer write generation')
        proof=review['proof']
        if (proof.get('body_sha256')!=body_key(text)or proof.get('source_sha256')!=fingerprint(normalized_thread(thread))
                or proof.get('slots')!=list(slots)or(proof.get('receipt')or{}).get('review',{}).get('result')!='ready'):
            raise DraftStateUnverified('Write review does not bind this exact commit')
        values={'phase':'draft_update_pending'if revision else'draft_write_pending','write_owner':review['owner'],
            'write_generation':current.get('write_generation',0)+1,
            'write_commit':{'owner':review['owner'],'state':'pending','body_sha256':body_key(text)},
            'write_review':proof,'original_thread':thread,'prepared_body':text,'prepared_slots':list(slots),
            'recipients':recipients,'copied':[]}
        if revision:values['revision_of']=revision
        runtime.remember_case(item,**values)

def finish_owned_write(result,*,runtime=None):
    if result.get('status')not in{'created','updated'}:return
    if runtime is None:import email_realtime as runtime
    item={'account':result['account'],'id':result['source_message_id'],'threadId':result['threadId']}
    from email_watcher import DraftStateUnverified
    with write_commit_lock(item,runtime):
        case=runtime.read_case(item);commit=case.get('write_commit')or{}
        if (commit.get('state')!='pending'or commit.get('owner')!=result.get('write_owner')
                or commit.get('body_sha256')!=result['body_sha256']):
            raise DraftStateUnverified('Saved result is not owned by the current committed writer')
        runtime.remember_case(item,phase='completed',write_commit={**commit,'state':'verified'},
            outcome={**(case.get('outcome')or{}),'status':'created',
                'draft_id':result['draftId'],'body_sha256':result['body_sha256'],'draft_message_id':result.get('draft_message_id')})

def model_failed_before_write(item,error,*,write_owner=None,write_generation=None,runtime=None):
    """Called only by the native writer with write_started=False."""
    if runtime is None:import email_realtime as runtime
    code=error.code
    with write_commit_lock(item,runtime):
        case=runtime.read_case(item)
        if write_generation is not None and case.get('write_generation',0)!=write_generation:return False
        require_write_admission(case,write_owner)
        if(case.get('outcome')or{}).get('status')=='created':
            # A genuine failed revision may keep its diagnosis while retaining
            # the last saved result. A late caller without its generation has
            # no authority even to attach that diagnosis to a newer result.
            if write_generation is not None:
                approval=case.get('pending_decision')or{}
                runtime.remember_case(item,phase='awaiting_approval'if approval and not approval.get('authorized')else'completed',
                    write_owner=(case.get('write_commit')or{}).get('owner',case.get('write_owner')),
                    last_prewrite_failure={'code':code,'stage':'semantic_review',
                    'owner':write_owner,'write_generation':write_generation,'availability':getattr(error,'availability',None)})
            return False
        runtime.remember_case(item,phase='review_required',error_code=code,
            outcome={**(case.get('outcome')or{}),'status':'workflow_blocked','review_reason':code},
            model_failure={'code':code,'availability':getattr(error,'availability',None)},
            draft_failure={'contract':'mail-prewrite-failure-v1','gmail_create_started':False,
                'code':code,'stage':'semantic_review','cause_type':type(error).__name__})
        return True

def current(item, *, runtime=None, watcher=None, policy=None):
    if runtime is None:import email_realtime as runtime
    if watcher is None:import email_watcher as watcher
    if policy is None:import mail_policy as policy
    alias=item['account'];tid=item['threadId'];mid=item['id']
    thread,omitted=runtime.get_thread(alias,tid)
    if omitted or not thread or any(m.get('truncated')for m in thread):raise ValueError('outcome_source_incomplete')
    original=[m for m in thread if'DRAFT'not in m.get('labels',[])]
    if not original:raise ValueError('outcome_source_missing')
    calendar_source=policy.CalendarSource(alias,tid,original)
    if getattr(runtime,'closed_source_lineage',lambda *args:False)(alias,tid,original):
        return {'state':'superseded','reason':'deliberately_closed_source_lineage','thread':original,'draft':None,'hard_facts':{}}
    if original[-1]['id']!=mid or{'SENT','SPAM','TRASH'}&set(original[-1].get('labels',[])):
        return {'state':'superseded','thread':original,'draft':None,'hard_facts':{}}
    case=runtime.read_case(item)
    candidates=[m for m in thread if'DRAFT'in m.get('labels',[])]
    result={'state':'current','thread':original,'case':case,'draft':None,'hard_facts':{},'owned_draft':None}
    if not candidates:return result
    if len(candidates)!=1:
        result.update(state='multiple_drafts');return result
    service=watcher.service(alias)
    expected={'code':'operator@primary.example.invalid','stealth':'operator@secondary.example.invalid'}[alias]
    if watcher.verified_profile(service).casefold()!=expected:raise ValueError('mailbox_identity_mismatch')
    message=service.users().messages().get(userId='me',id=candidates[0]['id'],format='full').execute()
    headers=watcher.headers(message)
    if message.get('threadId')!=tid or'DRAFT'not in message.get('labelIds',[])or'SENT'in message.get('labelIds',[]):
        raise ValueError('outcome_draft_identity_changed')
    text=watcher.body(message.get('payload',{})).replace('\r\n','\n').rstrip('\n')
    source=original[-1]
    source_match=bool(source.get('rfc_message_id')and headers.get('in-reply-to')==source['rfc_message_id'])
    recipients=sorted({a.casefold()for _,a in getaddresses([headers.get('to',''),headers.get('cc','')])if a})
    primary=[a.casefold()for _,a in getaddresses([headers.get('to','')])if a]
    calendar_source=policy.CalendarSource(alias,tid,original,primary_recipients=primary)
    facts={'saved_in_gmail':True,'source_bound':source_match,'single_draft':True,
           'calendar_required':False,'calendar_verified':False,'signature_verified':True}
    slots=[];holds=[];hard_errors=[]
    # Only native durable claims whose exact complete label occurs in this
    # actual body can be calendar evidence. No language is guessed here.
    claims=policy._read();owner=json.dumps([alias,tid,recipients],separators=(',',':'))
    with calendar_source.activate():
        for key,claim in claims.items():
            if policy.verified_release(claim)or text.count(policy.label(claim['start']))!=1:continue
            if policy._owned_claim_key(claims,owner,policy.start(claim['start']))!=key:continue
            slots.append(policy.start(claim['start']).astimezone(policy.BERLIN).isoformat());holds.append(claim)
    slots.sort();facts['proposed_slots']=slots
    try:
        formatted=policy.format_body(text,slots)
        if formatted!=text:hard_errors.append('mail_format_not_canonical')
        if not policy.mime_body_matches(message.get('payload',{}),text,policy.body_html(text)):
            hard_errors.append('mail_mime_not_verified')
    except policy.MailPolicyReviewRequired:hard_errors.append('mail_calendar_evidence_missing')
    if not source_match:hard_errors.append('mail_reply_source_mismatch')
    if holds:
        try:
            calendar=runtime.calendar_service();ids=policy._calendars(calendar)
            for claim in holds:
                if policy.start(claim['start'])<datetime.now(policy.BERLIN)-timedelta(minutes=5):
                    hard_errors.append('mail_proposed_time_expired');continue
                cid=policy.claim_calendar(claim)
                event=calendar.events().get(calendarId=cid,eventId=claim['event_id']).execute()
                policy._verify(event,claim)
                calendar_source.wrap(policy._check)(calendar,policy.start(claim['start']),ids,claim['event_id'],cid)
                if claim.get('state')!='held_draft_verified':hard_errors.append('mail_hold_not_verified')
        except ValueError:hard_errors.append('mail_calendar_readback_unverified')
        facts.update(calendar_required=True,calendar_verified=not hard_errors)
    elif ((case.get('calendar')or{}).get('relation')=='offer_conflict'
          and normalized_thread(case.get('original_thread',[]))==normalized_thread(original)):
        # A valid conflict reply has no proposed slot. Recheck all original
        # offered windows now rather than accepting an old failure sentence.
        from mail_workflow import calendar_facts
        fresh=calendar_facts(case['understanding'],now=datetime.now(policy.BERLIN),
            owner=(alias,tid,recipients),exact=calendar_source.wrap(runtime.check_incoming_availability),
            options=calendar_source.wrap(runtime.calendar_options),evidence=calendar_source.evidence)
        facts.update(calendar=fresh,calendar_required=True,
            calendar_verified=fresh['relation']=='offer_conflict'and fresh['checked_offers']>0 and not fresh['choices'])
        if not facts['calendar_verified']:hard_errors.append('mail_calendar_conflict_outdated')
    role_evidence=calendar_source.evidence()
    if role_evidence:facts['room_reservation']=role_evidence
    facts.update(hard_errors=sorted(set(hard_errors)),signature_verified=not any('format'in e or'mime'in e for e in hard_errors))
    stored=case.get('outcome')or{}
    revision=case.get('revision_of')or{}
    draft_id=stored.get('draft_id')or revision.get('draft_id')
    if (draft_id and (stored.get('body_sha256')==body_key(text)or revision.get('body_sha256')==body_key(text)or
                     case.get('prepared_body')and body_key(case['prepared_body'])==body_key(text))
            and source_match and owned_recipient_set(case,recipients)):
        actual=service.users().drafts().get(userId='me',id=draft_id,format='full').execute().get('message',{})
        if actual.get('id')==message['id']:
            result['owned_draft']={'draft_id':draft_id,'message_id':message['id'],
                'body_sha256':body_key(text),'recipients':recipients,'slots':slots}
    uncertain=case.get('phase')in{'draft_write_pending','draft_update_pending'}or(
        (case.get('outcome')or{}).get('status')=='needs_manual_review_draft_state')
    if uncertain and case.get('prepared_body')and source_match:
        try:expected_text=policy.format_body(case['prepared_body'],case.get('prepared_slots',[]))
        except ValueError:expected_text=None
        expected_recipients=sorted({x.casefold()for x in [*case.get('recipients',[]),*case.get('copied',[])]})
        if not expected_recipients:expected_recipients=revision.get('recipients',[])
        if (expected_text and body_key(expected_text)==body_key(text)and expected_recipients==recipients
                and set(hard_errors)<={'mail_hold_not_verified'}):
            if draft_id:
                mapped=service.users().drafts().get(userId='me',id=draft_id,format='full').execute().get('message',{})
                if mapped.get('id')!=message['id']:draft_id=None
            if not draft_id:
                token=None
                for _ in range(20):
                    page=service.users().drafts().list(userId='me',maxResults=500,**({'pageToken':token}if token else{})).execute()
                    matches=[d['id']for d in page.get('drafts',[])if d.get('message',{}).get('id')==message['id']]
                    if len(matches)>1:raise ValueError('outcome_draft_mapping_ambiguous')
                    if matches:draft_id=matches[0];break
                    token=page.get('nextPageToken')
                    if not token:break
            if draft_id:
                result['recoverable_write']={'draft_id':draft_id,'message_id':message['id'],
                    'body_sha256':body_key(text),'recipients':recipients,'slots':slots,
                    'claims':holds}
    result.update(draft=text,draft_message_id=message['id'],hard_facts=facts,slots=slots)
    return result

def confirm_pending(facts):
    """Only a verified Gmail readback can settle its exact pending claims."""
    import mail_policy as policy
    proof=facts['recoverable_write']
    with policy._locked():
        claims=policy._read()
        keys=[]
        for expected in proof['claims']:
            matches=[key for key,claim in claims.items()if claim==expected and claim.get('event_id')==expected['event_id']]
            if len(matches)!=1:raise ValueError('outcome_hold_changed_during_readback')
            keys.append(matches[0]);claim=claims[matches[0]]
            if claim['state']not in{'held_pending_draft','held_draft_unverified','held_draft_verified'}:
                raise ValueError('outcome_hold_state_changed')
        for key in keys:claims[key]['state']='held_draft_verified'
        if proof['claims']:policy._save(claims)
    return {k:v for k,v in proof.items()if k!='claims'}

def verified_before_save(case, facts):
    """Reuse the independent content review only for the same source and body."""
    if not facts.get('draft')or facts['hard_facts'].get('hard_errors'):
        return None
    if normalized_thread(case.get('original_thread',[]))!=normalized_thread(facts['thread']):return None
    proof=case.get('write_review')or{}
    from mail_outcomes import CONTRACT,REVIEW,fingerprint,validate_review,validate_write_claims,evidence_text,OutcomeReviewError
    if proof.get('contract')=='mail-semantic-write-v1':
        receipt=proof.get('receipt')or{};context=proof.get('context')or case.get('standard_context')or{}
        if (proof.get('body_sha256')==body_key(facts['draft'])
                and proof.get('source_sha256')==fingerprint(normalized_thread(facts['thread']))
                and sorted(proof.get('slots',[]))==sorted((facts.get('hard_facts')or{}).get('proposed_slots',[]))
                and context.get('require_time_claims')is True
                and normalized_thread(context.get('thread',[]))==normalized_thread(facts['thread'])
                and proof.get('context_key')==receipt.get('input_key')and receipt.get('contract')==CONTRACT
                and receipt.get('review_policy')==fingerprint(REVIEW)
                and receipt.get('input_key')==fingerprint({'contract':receipt.get('contract'),'prompt':REVIEW,'context':context})
                and body_key(context.get('draft')or'')==body_key(facts['draft'])):
            try:review=validate_write_claims(validate_review(receipt['review'],
                evidence_text(facts['thread'][-1]),facts['draft']),context)
            except (OutcomeReviewError,KeyError):return None
            if review['result']=='ready':return receipt
        return None
    if not case.get('standard_verified'):return None
    attempts=case.get('standard_attempts')or[]
    if not attempts:return None
    import mail_policy as policy
    candidate=attempts[-1].get('candidate')or{};receipt=attempts[-1].get('receipt')or{}
    from mail_outcomes import REVIEW,fingerprint
    if receipt.get('review_policy')!=fingerprint(REVIEW):return None
    if receipt.get('review',{}).get('result')!='ready' or not candidate.get('body'):return None
    import email_realtime as runtime
    try:body=policy.format_body(runtime.reply_signature(facts['thread'],candidate['body']),candidate.get('proposed_slots',[]))
    except ValueError:return None
    return receipt if body_key(body)==body_key(facts['draft'])else None
