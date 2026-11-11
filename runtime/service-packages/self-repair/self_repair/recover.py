"""Resume the original owned business case only after verified publication."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import sys
import uuid
import datetime as dt
from dataclasses import replace
from pathlib import Path
from self_repair.sources import LIVE,PAYLOAD,prewrite_failure

def login_failure_code(error):
    """Keep allowlisted auth causes across calendar/workflow wrappers."""
    seen=set()
    for _ in range(12):
        if error is None or id(error) in seen:break
        seen.add(id(error))
        code=getattr(error,'code',None)
        if isinstance(code,str) and code in {'invalid_grant','access_denied','admin_policy_enforced'}:return code
        error=error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return None

def mail_epoch():
    # One business retry per actual installed implementation, not one forever
    # and not one per arbitrary controller restart. No credentials enter it.
    files=[LIVE/'scripts'/name for name in ('email_realtime.py','email_watcher.py',
           'mail_workflow.py','mail_policy.py','calendar_routing.py','mail_business.py',
           'mail_outcomes.py','mail_result_io.py','mail_model_worker.py','mail_model_status.py')]
    values={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
    return hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()

def mail_retry_epoch(case,snapshot):
    """An implementation change or actual provider recovery, never a timer."""
    epoch=mail_epoch();wait=case.get('auto_repair_provider_wait')or case.get('model_failure')
    if not wait or wait.get('code')not in{'mail_model_quota','mail_model_auth','mail_model_unavailable'}:return epoch
    from mail_model_status import advances,evidence_key
    current=snapshot.get('provider_progress')
    if not advances(wait.get('availability'),current,code=wait['code']):return None
    return hashlib.sha256(json.dumps([epoch,evidence_key(current)]).encode()).hexdigest()

def mail_provider_failure(error,item,r,*,persist=True):
    code=getattr(error,'code',None)
    if code not in{'mail_model_quota','mail_model_auth','mail_model_unavailable'}:return None
    failure={'code':code,'availability':getattr(error,'availability',None)}
    generation=getattr(error,'write_generation',None)
    if generation is not None:
        current=r.read_case(item)
        if current.get('write_generation',0)!=generation:
            pending=(current.get('write_commit')or{}).get('state')=='pending'
            return {'state':'needs_write_readback'if pending else'superseded',
                'reason':'newer_write_generation','replayed':False}
    if persist:
        case=r.read_case(item)
        if case.get('phase')in{'draft_write_pending','draft_update_pending'}:return None
        if((case.get('outcome')or{}).get('status')=='created'
                and(case.get('write_commit')or{}).get('state')in{'verified','completed'}):
            r.remember_case(item,last_recovery_provider_failure=failure)
        else:
            r.remember_case(item,phase='review_required',error_code=code,
                outcome={'status':'workflow_blocked','review_reason':code},auto_repair_provider_wait=failure)
    return {'state':'provider_wait',**failure,'replayed':False}

def outcome_failure_mail(snapshot,signature,r):
    """Resume a failed read-only semantic check before considering any patch."""
    from mail_outcomes import assess,CONTRACT
    from mail_result_io import current
    item=snapshot['item']
    try:facts=current(item,runtime=r)
    except Exception as error:
        return {'state':'sources_unavailable','code':login_failure_code(error)or type(error).__name__,'replayed':False}
    if facts['state']=='superseded':return {'state':'superseded','replayed':False}
    if facts['state']!='current':return {'state':'needs_existing_draft_review','replayed':False}
    case=facts.get('case')or{};thread=facts['thread'];hard=facts.get('hard_facts')or{}
    incoming=thread[-1]
    received=dt.datetime.fromtimestamp(int(incoming['internalDate'])/1000,r.BERLIN).isoformat()if incoming.get('internalDate')else None
    context={'account':item['account'],'current_message':item['id'],'thread':thread,'review_phase':'saved',
        'received_at':received,'processing_at':dt.datetime.now(r.BERLIN).isoformat(),'draft':facts.get('draft'),
        'user_question':(case.get('outcome')or{}).get('question'),
        'business_context':case.get('business_context'),'hard_facts':hard,'require_time_claims':True}
    try:receipt=assess(context,r.model_json)
    except Exception as error:
        return mail_provider_failure(error,item,r,persist=False)or{'state':'workflow_blocked',
            'code':getattr(error,'code',None)or type(error).__name__,'replayed':False}
    verdict=receipt['review']
    if verdict['result']=='not_required':return {'state':'no_reply_required','replayed':False}
    if verdict['result']=='needs_user'and(case.get('outcome')or{}).get('question'):
        return {'state':'awaiting_user_decision','replayed':False}
    uncertain=case.get('phase')in{'draft_write_pending','draft_update_pending'}or(
        (case.get('outcome')or{}).get('status')=='needs_manual_review_draft_state')
    if (verdict['result']=='ready'and not uncertain and not hard.get('hard_errors')
            and all(hard.get(key)is True for key in('saved_in_gmail','source_bound','single_draft'))
            and(not verdict['scheduling_required']or hard.get('calendar_verified')is True)):
        # A useful existing result, including a user's draft, is retained.
        return {'state':'verified_existing_result','replayed':False,'saved_result_verified':True}
    expectation={'contract':CONTRACT,'receipt':receipt,'checked_draft':facts.get('draft'),
        'hard_facts':hard,'owned_draft':facts.get('owned_draft')}
    return expected_mail({**snapshot,'case':case,'original_thread':thread,'expectation':expectation},signature,r)

def mail_notice(runtime,item,source,outcome):
    # Reuse the normal source-bound substance, importance and approval boundary.
    state={};notice_outcome=outcome
    if outcome.get('invitation_approval_required')and outcome.get('appointment_start'):
        notice_outcome={**outcome,'crm_review_required':False}
    runtime.queue_draft_notification(state,{**source,**item},notice_outcome)
    messages=state.get('notifications')or[]
    return(messages[0]if isinstance(messages[0],str)else messages[0]['text'])if len(messages)==1 else None

def notify_recovered_result(runtime,item,source,outcome):
    if runtime.read_case(item).get('repair_notification_attempted'):return
    notice=mail_notice(runtime,item,source,outcome)
    if not notice:return
    runtime.remember_case(item,repair_notification_attempted=True)
    try:runtime.remember_case(item,repair_notification_receipt=runtime.notify(notice))
    except Exception:pass

def non_draft_result(runtime,item,source,outcome):
    status=outcome.get('status')
    if status in{'no_draft_needed','not_worthy_or_already_handled','out_of_scope_platform_mail','cc_information','deliberately_closed'}:
        return {'state':'no_reply_required','replayed':False}
    if status not in{'needs_user_input','important_information'}:return None
    state={};runtime.queue_draft_notification(state,{**source,**item},outcome)
    pending=state.get('notifications')or[]
    fresh=runtime.read_case(item)
    if pending and not fresh.get('repair_notification_attempted'):
        runtime.remember_case(item,repair_notification_attempted=True)
        try:
            text=pending[0]if isinstance(pending[0],str)else pending[0]['text']
            runtime.remember_case(item,repair_notification_receipt=runtime.notify(text))
        except Exception:pass
    if status=='needs_user_input'and outcome.get('question'):
        return {'state':'awaiting_user_decision','replayed':False}
    if status=='important_information':return {'state':'restored_information','replayed':False}
    return None

def saved_mail_result(snapshot,r,*,facts=None):
    """A created status is not a completed reply, including on a resumed repair.

    Use the native exact-body content receipt and fresh service readbacks. The
    snapshot supplies the independently established scheduling requirement;
    a missing calendar claim must never turn that requirement off.
    """
    from mail_result_io import current,normalized_thread,verified_before_save
    item=snapshot['item'];case=r.read_case(item)
    if facts is None:facts=current(item,runtime=r)
    if facts.get('state')=='superseded':return {'state':'superseded','replayed':False}
    if case.get('phase')in{'draft_write_pending','draft_update_pending'}or(
            (case.get('outcome')or{}).get('status')=='needs_manual_review_draft_state'):
        return {'state':'needs_write_readback','replayed':False}
    hard=facts.get('hard_facts')or{};owned=facts.get('owned_draft')or{}
    errors=list(hard.get('hard_errors')or[])
    original=snapshot.get('original_thread')or(snapshot.get('case')or{}).get('original_thread')or[]
    if facts.get('state')!='current' or normalized_thread(facts.get('thread')or[])!=normalized_thread(original):
        errors.append('mail_reply_source_changed')
    if not facts.get('draft')or hard.get('saved_in_gmail')is not True:
        errors.append('mail_saved_draft_unverified')
    if hard.get('source_bound')is not True or hard.get('single_draft')is not True:
        errors.append('mail_reply_identity_unverified')
    if not owned.get('draft_id')or owned['draft_id']!=(case.get('outcome')or{}).get('draft_id'):
        errors.append('mail_draft_ownership_unverified')
    receipt=verified_before_save(case,facts)
    if receipt is None:errors.append('mail_content_receipt_unverified')
    expected=((snapshot.get('expectation')or{}).get('receipt')or{}).get('review')or{}
    reviewed=(receipt or{}).get('review')or{}
    if (expected.get('scheduling_required')is True or reviewed.get('scheduling_required')is True
            or hard.get('calendar_required')is True)and hard.get('calendar_verified')is not True:
        errors.append('mail_calendar_evidence_missing')
    if errors:
        return {'state':'workflow_blocked','code':'reply_standard_failed',
                'result_errors':sorted(set(errors)),'replayed':False}
    return {'state':'restored','draft_id':owned['draft_id'],'saved_result_verified':True}

def mail(snapshot,signature):
    sys.path.insert(0,str(LIVE/'scripts'))
    import email_realtime as r
    item=snapshot['item'];original=snapshot['case']
    if item.get('threadId')in getattr(r,'CLOSED_THREADS',{}).get(item.get('account'),set()):
        return {'state':'superseded','reason':'deliberately_closed','replayed':False}
    path=r.case_path(item).with_suffix('.lock')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with path.open('a') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        case=r.read_case(item)
        if(case.get('outcome')or{}).get('status')=='deliberately_closed':
            return {'state':'superseded','reason':'deliberately_closed','replayed':False}
        if snapshot.get('expectation'):
            return expected_mail(snapshot,signature,r)
        if snapshot.get('outcome_check_failure'):
            return outcome_failure_mail(snapshot,signature,r)
        if (case.get('repair_signature')==signature and
                (case.get('outcome') or {}).get('status')=='created'):
            return {**saved_mail_result(snapshot,r),'reused':True}
        prewrite = prewrite_failure(case)
        if (case.get('prepared_body') and not prewrite) or case.get('phase')=='draft_write_pending':
            return {'state':'needs_write_readback','replayed':False}
        own_retry=case.get('repair_signature')==signature
        if (case.get('phase')!='review_required' or (case.get('error_code')!=original.get('error_code') and not own_retry) or
                (case.get('source_hash')!=original.get('source_hash') and not own_retry)):
            return {'state':'case_changed_or_retry_used','replayed':False}
        epoch=mail_retry_epoch(case,snapshot)
        if epoch is None:return {'state':'provider_wait','code':(case.get('auto_repair_provider_wait')or case.get('model_failure')or{}).get('code'),'replayed':False}
        if case.get('auto_repair_epoch')==epoch or (case.get('auto_repair_attempts',0)>=1 and not case.get('auto_repair_epoch')):
            return {'state':'case_changed_or_retry_used','replayed':False}
        try:thread,omitted=r.get_thread(item['account'],item['threadId'])
        except Exception as error:
            return {'state':'sources_unavailable','code':login_failure_code(error) or type(error).__name__, 'replayed':False}
        if omitted or not thread:return {'state':'sources_unavailable','replayed':False}
        if getattr(r,'closed_source_lineage',lambda *args:False)(item['account'],item['threadId'],thread):
            return {'state':'superseded','reason':'deliberately_closed_source_lineage','replayed':False}
        if thread[-1]['id']!=item['id'] and not any('DRAFT' in x.get('labels',[]) for x in thread):
            r.remember_case(item,phase='completed',outcome={'status':'not_worthy_or_already_handled'})
            return {'state':'superseded','replayed':False}
        if any('DRAFT' in x.get('labels',[]) for x in thread):
            # Existence alone proves neither correctness nor authorship of a
            # saved draft. Preserve it, but do not report a recovered outcome.
            return {'state':'needs_existing_draft_review','replayed':False}
        if own_retry and original.get('original_thread'):
            keys=('id','thread_id','from','to','cc','internalDate','rfc_message_id','in_reply_to','text')
            normalize=lambda messages:[{k:m.get(k)for k in keys}for m in messages]
            if normalize(thread)!=normalize(original['original_thread']):
                return {'state':'source_changed','replayed':False}
        r.remember_case(item,phase='retry_pending',understanding=None,draft_plan=None,
                        outcome=None,error_code=None,auto_repair_attempts=case.get('auto_repair_attempts',0)+1,
                        auto_repair_epoch=epoch,repair_signature=signature,
                        auto_repair_provider_wait=None,model_failure=None,
                        **({'prepared_body': None, 'prepared_slots': [], 'draft_failure': None} if prewrite else {}))
        try:outcome=r._draft_one(item)
        except Exception as error:
            provider=mail_provider_failure(error,item,r)
            if provider:return provider
            case=r.read_case(item)
            status='needs_manual_review_draft_state' if case.get('prepared_body') else 'workflow_blocked'
            r.remember_case(item,phase='review_required',error_code=getattr(error,'code',None) or type(error).__name__,
                            outcome={'status':status,'case_key':item['account']+':'+item['id']})
            return {'state':status,'code':login_failure_code(error) or getattr(error,'code',None) or type(error).__name__,'replayed':False}
        if outcome.get('status')=='created':
            verified=saved_mail_result(snapshot,r)
            if verified['state']!='restored':return verified
            # Persist the notification boundary as well; an unknown Telegram send
            # must never duplicate the restored draft or this result message.
            notify_recovered_result(r,item,thread[-1],outcome)
            return verified
        settled=non_draft_result(r,item,thread[-1],outcome)
        if settled:return settled
        return {'state':outcome.get('status','unresolved'),
                'code':outcome.get('review_reason')or r.read_case(item).get('error_code'),'replayed':False}

def expected_mail(snapshot,signature,r):
    """Recover an independently detected gap using the same native mail path."""
    from mail_outcomes import CONTRACT
    from mail_result_io import current,normalized_thread,body_key
    expectation=snapshot['expectation'];verdict=(expectation.get('receipt')or{}).get('review')or{}
    if expectation.get('contract')!=CONTRACT or verdict.get('reply_required')is not True:
        return {'state':'expectation_requires_review','replayed':False}
    item=snapshot['item'];case=r.read_case(item)
    facts=current(item,runtime=r)
    if facts['state']=='superseded':return {'state':'superseded','replayed':False}
    if facts['state']!='current':return {'state':'needs_existing_draft_review','replayed':False}
    if normalized_thread(facts['thread'])!=normalized_thread(snapshot['original_thread']):
        return {'state':'source_changed','replayed':False}
    # A lost acknowledgement is resolved by exact content readback, never by a
    # second create. Even a missing remote draft does not waive this boundary.
    uncertain=case.get('phase')in{'draft_write_pending','draft_update_pending'}or(
        (case.get('outcome')or{}).get('status')=='needs_manual_review_draft_state')
    if uncertain:
        if facts.get('recoverable_write'):
            from mail_result_io import confirm_pending
            existing=confirm_pending(facts)
            r.remember_case(item,phase='completed',error_code=None,repair_signature=signature,
                outcome={**(case.get('outcome')or{}),'status':'created','draft_id':existing['draft_id'],
                         'body_sha256':existing['body_sha256'],'creation_origin':'uncertain_write_verified_by_readback'})
            if verdict.get('result')!='ready':
                return {'state':'workflow_blocked','code':'reply_standard_failed','readback_only':True}
            return {**saved_mail_result(snapshot,r),'readback_only':True}
        return {'state':'needs_write_readback','replayed':False}
    if(case.get('repair_signature')==signature and(case.get('outcome')or{}).get('status')=='created'):
        return {**saved_mail_result(snapshot,r,facts=facts),'reused':True}
    if case.get('prepared_body')and not prewrite_failure(case)and not facts.get('owned_draft'):
        return {'state':'needs_write_readback','replayed':False}
    epoch=mail_retry_epoch(case,snapshot)
    if epoch is None:return {'state':'provider_wait','code':(case.get('auto_repair_provider_wait')or case.get('model_failure')or{}).get('code'),'replayed':False}
    if case.get('auto_repair_epoch')==epoch:return {'state':'case_changed_or_retry_used','replayed':False}
    existing=facts.get('owned_draft')
    settled=(existing and case.get('repair_signature')==signature and
             (case.get('outcome')or{}).get('creation_origin')=='uncertain_write_verified_by_readback'and
             existing['body_sha256']==body_key(expectation.get('checked_draft')or''))
    if facts.get('draft')and(not existing or(existing!=expectation.get('owned_draft')and not settled)):
        return {'state':'needs_existing_draft_review','replayed':False}
    if not facts.get('draft')and(case.get('outcome')or{}).get('status')=='created':
        return {'state':'previously_saved_draft_removed','replayed':False}
    r.remember_case(item,phase='retry_pending',understanding=None,draft_plan=None,outcome=None,error_code=None,
        auto_repair_epoch=epoch,auto_repair_attempts=case.get('auto_repair_attempts',0)+1,
        repair_signature=signature,prepared_body=None,prepared_slots=[],draft_failure=None,
        auto_repair_provider_wait=None,model_failure=None,
        **({'revision_of':existing}if existing else{}))
    try:
        if existing:
            recipients=existing['recipients'];owner=(item['account'],item['threadId'],recipients)
            def options(now=None,window=None,availability=None):
                choices=[]
                bounds=availability or {}
                begin=r.datetime.fromisoformat(bounds['not_before'])if bounds.get('not_before')else None
                end=r.datetime.fromisoformat(bounds['not_after'])if bounds.get('not_after')else None
                for value in existing['slots']:
                    start=r.datetime.fromisoformat(value)
                    if start<now+r.timedelta(minutes=45):continue
                    if window and not(window[0]<=start and start+r.timedelta(minutes=30)<=window[1]):continue
                    if (begin and start<begin)or(end and start+r.timedelta(minutes=30)>end):continue
                    if any(r.datetime.fromisoformat(x['start'])<start+r.timedelta(minutes=30)
                           and r.datetime.fromisoformat(x['end'])>start for x in bounds.get('excluded',[])):continue
                    if r.check_incoming_availability(start,owner=owner):
                        from mail_policy import label
                        choices.append({'start':value,'label':label(value)})
                return choices or r.calendar_options(now=now,window=window,**({'availability':availability}if availability else{}))
            answer=r.generate(item['account'],item['id'],facts['thread'],False,
                scheduling_owner=owner,reply_row=item,calendar_options_reader=options,
                journal=lambda **values:r.remember_case(item,**values))
            if not answer.get('body'):return {'state':'needs_existing_draft_review','replayed':False}
            body=r.reply_signature(facts['thread'],answer['body'])
            write_owner=uuid.uuid4().hex
            r.remember_case(item,phase='draft_update_pending',write_owner=write_owner,prepared_body=body,prepared_slots=answer['proposed_slots'],
                draft_update={'draft_id':existing['draft_id'],'before_message_id':existing['message_id'],
                              'before_body_sha256':existing['body_sha256']})
            result=r.w.revise_draft(item['account'],item['id'],existing['draft_id'],existing['message_id'],
                existing['body_sha256'],body,slots=answer['proposed_slots'],project=answer.get('project'),company=answer.get('company'),write_owner=write_owner)
            outcome={**answer,'status':'created','draft_id':result['draftId'],'body':None,
                'body_sha256':result['body_sha256'],'draft_message_id':result['draft_message_id'],
                'creation_origin':'revised_verified_native_draft'}
            r.remember_case(item,phase='completed',outcome=outcome)
            if answer.get('invitation_approval_required'):
                r.remember_case(item,phase='awaiting_approval',pending_decision={
                    'action':'create_invitation','authorized':False,
                    'start':(answer.get('proposed_slots')or[None])[0],
                    'recipients':answer.get('invitation_recipients')or recipients,
                    'source_evidence':answer.get('invitation_recipient_evidence',[]),
                    'request_source':{'account':item['account'],'id':item['id'],'threadId':item['threadId'],
                        'rfc_message_id':facts['thread'][-1].get('rfc_message_id'),'text':facts['thread'][-1]['text']},
                    'requires_fresh_original_read':True})
        else:outcome=r._draft_one(item)
    except Exception as error:
        provider=mail_provider_failure(error,item,r)
        if provider:return provider
        changed=getattr(r.w,'SourceChangedBeforeDraft',())
        if changed and isinstance(error,changed):return {'state':'source_or_user_draft_changed','replayed':False}
        fresh=r.read_case(item);unknown=fresh.get('phase')in{'draft_write_pending','draft_update_pending'}
        code=getattr(error,'code',None)or type(error).__name__
        if isinstance(error,r.w.SchedulingDraftReviewRequired):
            unknown=False
            r.remember_case(item,draft_failure={'contract':'mail-prewrite-failure-v1',
                'gmail_create_started':False,'code':code,'stage':getattr(error,'stage','revision_preflight'),
                'cause_type':getattr(error,'cause_type',None)})
        r.remember_case(item,phase='review_required',error_code=code,
            outcome={'status':'needs_manual_review_draft_state'if unknown else'workflow_blocked','review_reason':code})
        return {'state':'needs_write_readback'if unknown else'workflow_blocked',
                'code':login_failure_code(error)or code,'replayed':False}
    if outcome.get('status')!='created':
        if outcome.get('status')=='needs_user_input'and outcome.get('question'):
            fresh=r.read_case(item)
            if not fresh.get('repair_notification_attempted'):
                r.remember_case(item,repair_notification_attempted=True)
                try:r.remember_case(item,repair_notification_receipt=r.notify(outcome['question']))
                except Exception:pass
            return {'state':'awaiting_user_decision','replayed':False}
        return {'state':'workflow_blocked','code':r.read_case(item).get('error_code')or'reply_standard_failed','replayed':False}
    verified=saved_mail_result(snapshot,r)
    if verified['state']!='restored':return verified
    notify_recovered_result(r,item,facts['thread'][-1],outcome)
    return verified

def briefing(snapshot,signature):
    sys.path.insert(0,str(PAYLOAD))
    from gtm import briefing_daemon as b
    from gtm.briefing import due_briefing
    context=snapshot.get('context')
    if not context:return {'state':'sources_unavailable'}
    now=dt.datetime.now(dt.timezone.utc)
    reads=b.ExistingReads();event=reads.current_event(context['calendar']['id'])
    if event.get('start')!=context['calendar'].get('start'):return {'state':'rescheduled','replayed':False}
    category,_=b.classify(event)
    if category is None:
        mapped=reads.business_mapping(event)
        category=mapped.get('category') if mapped else None
    due=due_briefing(event,calendar_id=b.event_calendar(event),category=category,now=now,max_delay_seconds=900)
    if not due or isinstance(due,dict):return {'state':'call_not_upcoming','replayed':False}
    identity=hashlib.sha256((snapshot['identity']+':repair:'+signature).encode()).hexdigest()
    due=replace(due,identity=identity)
    store=b.Store(LIVE/'gtm/briefings');row=store.get(identity)
    if row and row['send_attempted']:
        row=store.recover_send(identity)
        return {'state':'restored' if row['state']=='delivered' else 'needs_delivery_readback','replayed':False}
    runtime=b.Runtime(store,target=b.DESTINATION)
    if row is None:
        if not store.claim(due,clock=now.timestamp(),max_tasks=2):return {'state':'briefing_budget_busy'}
        fresh=reads.context(event,category);fresh['briefing_job_id']=identity
        store.step(identity,'model_attempted',model=True)
        raw=runtime.model(fresh)
    else:
        cp=store.root/(identity+'.context.json');op=store.root/(identity+'.model.json')
        if not cp.exists() or not op.exists():return {'state':'model_result_unknown','replayed':False}
        fresh=json.loads(cp.read_text());raw=json.loads(op.read_text())
    text=b.validate_output(raw,fresh,starts_at=due.starts_at)
    final=reads.current_event(due.event_id)
    if (final.get('etag')!=fresh['calendar'].get('etag') or final.get('status')=='cancelled' or
            dt.datetime.now(dt.timezone.utc)>=due.starts_at):return {'state':'call_changed','replayed':False}
    store.step(identity,'send_attempted',sending=True)
    result=runtime.send(identity,text)
    return {'state':'restored' if result and result['state']=='delivered' else 'needs_delivery_readback'}
