"""Capture complete owned failure cases once; healthy polls spend no model calls."""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path
from self_repair.policy import EDITABLE,digest
from self_repair.store import hash_value

LIVE=Path('/home/hermes/.hermes')
PAYLOAD=Path('/home/hermes/hermes-extensions-staging/runtime/payload')
TARGETS={rel:(LIVE/'scripts'/Path(rel).name if '/calendar/runtime/' in rel else PAYLOAD/rel) for rel in EDITABLE}

def read_json(path,maximum=1_000_000):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>maximum:
        raise ValueError('source_requires_review')
    return json.loads(path.read_bytes())

def prewrite_failure(case):
    """Recognize the native before-create exception, never an uncertain write."""
    outcome = case.get('outcome') or {}
    if case.get('phase') != 'review_required' or outcome.get('status') != 'workflow_blocked':
        return None
    proof = case.get('draft_failure') or {}
    if proof:
        model_before_write=(proof.get('code')in{'mail_model_quota','mail_model_auth','mail_model_unavailable','mail_model_output_invalid','mail_model_runtime_failed'}
            and proof.get('stage')=='semantic_review'and proof.get('cause_type')=='MailModelFailure')
        if (proof.get('contract') == 'mail-prewrite-failure-v1'
                and proof.get('gmail_create_started') is False
                and (proof.get('code') in {'mail_text_validation', 'slot_reservation_unavailable'}or model_before_write)
                and case.get('error_code') == proof['code']):
            return proof['code']
        return None
    # Older runtime only returned this outcome from the catch before Gmail create.
    # Preserve the original case; only normalize its durable incident code.
    if (case.get('contract') == 'mail-workflow-20261022-business-sources-1'
            and outcome.get('review_reason') == 'reservation_unavailable'
            and case.get('prepared_body') and case.get('prepared_slots')):
        return 'scheduling_prewrite_failed'
    return None

def capture(store,*,home=LIVE,targets=TARGETS):
    versions={rel:digest(path) for rel,path in targets.items()}
    found=0
    for path in sorted((home/'email_watcher/cases').glob('*.json')):
        case=read_json(path)
        prewrite = prewrite_failure(case)
        code = case.get('error_code') or prewrite
        if case.get('phase')!='review_required' or not code:continue
        if ((case.get('prepared_body') and not prewrite)
                or (case.get('outcome') or {}).get('status')=='needs_manual_review_draft_state'):continue
        alias,mid,tid=(case.get(k) for k in ('account','message_id','thread_id'))
        if alias not in {'code','stealth'} or not all(isinstance(x,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',x) for x in (mid,tid)):
            continue
        key=hash_value(['mail',alias,mid,case.get('source_hash'),code])
        if store.seen(key):continue
        original=case.get('original_thread')
        if original is None:
            # Migration of an already open case: fetch its complete thread once.
            sys.path.insert(0,str(home/'scripts'))
            import email_realtime
            original,omitted=email_realtime.get_thread(alias,tid)
            if omitted:raise ValueError('original_thread_incomplete')
        snapshot={'kind':'mail','item':{'account':alias,'id':mid,'threadId':tid},
                  'case':case,'original_thread':original}
        model_failures=[]
        for phase in ('understand','compose','review'):
            failure=path.with_name(alias+'-'+mid+'.'+phase+'-failure.json')
            if failure.exists():model_failures.append(read_json(failure))
        snapshot['model_failures']=model_failures
        if len(json.dumps(snapshot,ensure_ascii=False).encode())>1_000_000:
            raise ValueError('complete_case_requires_larger_scope')
        store.record(source_key=key,component='mail',code=code,versions=versions,snapshot=snapshot)
        found+=1
    for path in sorted((home/'gtm/briefings').glob('*.failure.json')):
        failure=read_json(path)
        identity=failure.get('job_id','')
        if failure.get('contract')!='briefing-failure-v1' or not re.fullmatch('[a-f0-9]{64}',identity):continue
        key=hash_value(['briefing',identity,failure.get('code')])
        if store.seen(key):continue
        # Transport uncertainty is a readback case, never a second send.
        if failure.get('stage')=='delivery':continue
        context_path=path.with_name(identity+'.context.json')
        output_path=path.with_name(identity+'.model.json')
        snapshot={'kind':'briefing','failure':failure,'identity':identity,
                  'context':read_json(context_path) if context_path.exists() else None,
                  'model_output':read_json(output_path) if output_path.exists() else None}
        store.record(source_key=key,component='briefing',code=failure['code'],versions=versions,snapshot=snapshot)
        found+=1
    return found
