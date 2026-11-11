"""Trusted native semantic proof. Models have no tools; no business writes."""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

def original_failure(snapshot,standard):
    expectation=snapshot.get('expectation')or{}
    if expectation:
        receipt=expectation.get('receipt')or{}
        candidate=(snapshot.get('case')or{}).get('prepared_body')
        # The monitor records the exact saved text separately from case intent.
        candidate=expectation.get('checked_draft',candidate)
    else:
        attempts=(snapshot.get('case')or{}).get('standard_attempts')or[]
        if not attempts:raise ValueError('semantic_original_receipt_missing')
        receipt=attempts[-1]['receipt'];candidate=(attempts[-1].get('candidate')or{}).get('body')
    if receipt.get('contract')!=standard.CONTRACT:raise ValueError('semantic_original_contract_changed')
    review=standard.validate_review(receipt['review'],standard.evidence_text(snapshot['original_thread'][-1]),candidate)
    if review['result']not in{'missing','revise'}:raise ValueError('semantic_original_failure_unproven')
    return review

def run(source,snapshot):
    scripts=Path(source)/'gtm/calendar/runtime';sys.path.insert(0,str(scripts))
    import email_realtime as r
    import mail_outcomes as standard
    failed=original_failure(snapshot,standard)
    item=snapshot['item'];thread=snapshot['original_thread'];journal={}
    answer=r.generate(item['account'],item['id'],thread,False,
        scheduling_owner=(item['account'],item['threadId'],[r.parseaddr(thread[-1]['from'])[1]]),
        journal=lambda **values:journal.update(values))
    if not answer.get('body')or not answer.get('standard_verified'):
        raise ValueError('semantic_original_candidate_missing')
    from mail_policy import format_body
    from mail_result_io import body_key
    final=format_body(r.reply_signature(thread,answer['body']),answer.get('proposed_slots',[]))
    context=journal['standard_context'];receipt=journal['result_standard']
    if body_key(context.get('draft')or'')!=body_key(final):raise ValueError('semantic_final_body_unbound')
    standard.validate_write_claims(receipt['review'],context)
    bad={**context,'draft':final+'\n\nZusätzlich könnte ich am Dienstag, 01.01.2030 um 03:17 Uhr.'}
    rejected=False
    try:extra=standard.assess(bad,r.model_json,cached=receipt)
    except standard.OutcomeReviewError as error:
        if error.code!='outcome_actual_proposals_unverified':raise
        rejected=True
    else:rejected=extra['review']['result']in{'revise','needs_user'}
    if not rejected:raise ValueError('semantic_unverified_extra_start_accepted')
    # A routine acknowledgement is the relevant countercase to overcorrecting
    # every incoming message into an unnecessary answer/calendar operation.
    incoming={**thread[-1],'id':'private-countercase','text':'Vielen Dank für Ihre Rückmeldung. Ich melde mich nach unserer internen Abstimmung wieder bei Ihnen.'}
    counter=[*thread[:-1],incoming];quiet={}
    result=r.generate(item['account'],incoming['id'],counter,False,
        journal=lambda **values:quiet.update(values))
    if result.get('body')or result.get('question')or result.get('proposed_slots'):
        raise ValueError('semantic_countercase_created_unnecessary_reply')
    return {'baseline_failed':True,'baseline_kind':'recorded_source_bound_semantic_failure',
            'baseline_review':failed,'candidate_passed':True,'native_empty_tools':True,
            'candidate':answer,'candidate_review':journal['result_standard'],
            'final_body_bound':True,'unverified_extra_start_blocked':True,
            'countercase_passed':True,'countercase_review':quiet['result_standard'],
            'business_writes':0}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',required=True);parser.add_argument('--input',required=True)
    args=parser.parse_args()
    try:result=run(args.source,json.loads(Path(args.input).read_bytes()))
    except Exception as error:
        code=str(getattr(error,'code',None)or error)
        if not code.replace('_','').isalnum()or len(code)>100:code=type(error).__name__
        print(json.dumps({'probe_failed':True,'validation_code':code}));return 1
    print(json.dumps(result,ensure_ascii=False));return 0

if __name__=='__main__':raise SystemExit(main())
