"""Existing legacy document mutations on CanonicalSources' raw/CAS chain.

No new journal directory or database. A document is either an already imported
named original, or a narrowly named completion marker. The full new bytes are
retained by CanonicalSources.apply; its explicit target carries the one changed
recipient's state. Multi-recipient mutations and identity edits are refused.
"""
import base64
import json
import re

from background_jobs.contracts import ContractError,approved_path,canonical,digest
from outreach_night.central_sources import scope_key

PREFIX='Legacydokument-v1:'
MARKER=re.compile(r'completion:[0-9a-f]{64}')
IDENTITY={'email','draft_id','page_id','subject','body','firm'}


def read(authority,project,account,name):
    with authority.hold(project,account):return _read_locked(authority,project,account,name)


def _read_locked(authority,project,account,name):
    if not isinstance(name,str) or not name or len(name)>2048:raise ContractError('legacy_named_document_required')
    snap=authority.snapshot(project,account,verify_originals=True)
    with authority.lifecycle.connect()as c:
        rows=c.execute('SELECT payload,original_sha256 FROM outreach_source_changes WHERE scope=? ORDER BY revision DESC',
                       (scope_key(project,account),)).fetchall()
    for row in rows:
        payload=json.loads(row['payload'])
        reason=payload.get('change',{}).get('reason','')
        if not reason.startswith(PREFIX):continue
        meta=json.loads(reason[len(PREFIX):])
        if meta.get('document')!=name:continue
        raw=approved_path(authority.root,'central-source-originals/'+row['original_sha256']+'.raw').read_bytes()
        if digest(raw)!=meta.get('sha256'):raise ContractError('legacy_source_document_original_changed')
        return {'exists':True,'sha256':digest(raw),'raw_base64':base64.b64encode(raw).decode(),
                'version':{k:snap[k]for k in ('project','account','revision','entry_sha256')}}
    for change in snap['changes'][:1]:
        packet=json.loads(approved_path(authority.root,change['ref']).read_bytes())
        if not isinstance(packet,dict) or not isinstance(packet.get('files'),list):continue
        matches=[row for row in packet['files']if row.get('path')==name]
        if len(matches)>1:raise ContractError('legacy_named_original_ambiguous')
        if matches:
            row=matches[0];raw=base64.b64decode(row['raw_base64'],validate=True)
            if digest(raw)!=row['sha256']:raise ContractError('legacy_source_document_original_changed')
            return {'exists':True,'sha256':digest(raw),'raw_base64':base64.b64encode(raw).decode(),
                    'version':{k:snap[k]for k in ('project','account','revision','entry_sha256')}}
    if MARKER.fullmatch(name):
        return {'exists':False,'sha256':None,'raw_base64':None,
                'version':{k:snap[k]for k in ('project','account','revision','entry_sha256')}}
    raise ContractError('legacy_named_document_not_imported')


def _rows(value):
    rows=value.get('rows')if isinstance(value,dict)else value
    if not isinstance(rows,list):raise ContractError('legacy_document_rows_required')
    result={}
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('email'),str):raise ContractError('legacy_document_recipient_required')
        email=row['email'].strip().lower()
        if row['email']!=email or email in result:raise ContractError('legacy_document_recipient_ambiguous')
        result[email]=row
    return result


def _change(name,old,new,account):
    if MARKER.fullmatch(name):
        if not isinstance(new,dict) or new.get('status')not in {'started','complete'}:
            raise ContractError('legacy_completion_marker_invalid')
        if old is not None:
            for field in ('batch_id','batch_sha256','mails_sha256'):
                if old.get(field)!=new.get(field):raise ContractError('legacy_completion_identity_changed')
            if old.get('status')=='complete'and new.get('status')=='started':
                # Existing complete subset may expand only if the original
                # subset is still retained; close.complete checks journal/CRM.
                if not set(old.get('confirmed',[]))<=set(new.get('confirmed',[])):
                    raise ContractError('legacy_completion_subset_lost')
        return {'action':'review_only','target':{'company':'Abschluss '+name},'reason':'unused'}
    if name.endswith('.jsonl'):
        if not isinstance(old,list) or not isinstance(new,list) or len(new)!=len(old)+1 or new[:-1]!=old:
            raise ContractError('legacy_journal_exact_append_required')
        event=new[-1]
        if not isinstance(event,dict):
            raise ContractError('legacy_journal_exact_event_required')
        if event.get('event')in {'history_clear','draft_crm_attempt','draft_crm_readback','source_note_attempt','source_note_readback'}:
            target={'email':event['email']}if event.get('email')else{'company_id':event['id']}
            return {'action':'review_only','target':target,'reason':'unused'}
        if event.get('schema_version')!=1 or not event.get('attempt_id'):raise ContractError('legacy_journal_exact_event_required')
        states={'attempt':'attempt_unknown','draft_attempt':'attempt_unknown','send_ack':'attempt_unknown',
                'send_ack_recovered':'attempt_unknown','draft_ack':'attempt_unknown','sent_readback':'sent_verified','draft_readback':'draft_verified'}
        if event.get('event')not in states:raise ContractError('legacy_journal_event_unknown')
        if event['event']in {'sent_readback','draft_readback'} and not event.get('message_id'):
            raise ContractError('legacy_journal_readback_identity_required')
        if event['event']in {'sent_readback','draft_readback'} and (event.get('sender')!=account or event.get('to')!=event['email']):
            raise ContractError('legacy_journal_readback_account_or_recipient_changed')
        return {'action':states[event['event']],'target':{'email':event['email']},'reason':'unused'}
    before,after=_rows(old),_rows(new)
    if before.keys()!=after.keys():raise ContractError('legacy_document_recipients_changed')
    changed=[key for key in before if before[key]!=after[key]]
    if len(changed)>1:raise ContractError('legacy_document_one_recipient_transition_required')
    if not changed:return {'action':'review_only','target':{'company':'Historisches Journal '+name},'reason':'unused'}
    email=changed[0];a,b=before[email],after[email]
    if any(a.get(k)!=b.get(k)for k in IDENTITY):raise ContractError('legacy_document_recipient_identity_changed')
    pending=any(b.get(k)is True for k in ('send_pending','sent_crm_pending','draft_pending','create_pending','schedule_pending'))
    state=('attempt_unknown'if pending else'sent_verified'if b.get('sent_verified')is True
           else'draft_verified'if b.get('draft_verified')is True else'attempt_unknown'if b.get('scheduled')is True else'review_only')
    if a.get('sent_verified')is True and b.get('sent_verified')is not True:raise ContractError('legacy_sent_history_cannot_be_cleared')
    if state in {'sent_verified','draft_verified'}and (b.get('smtp')or {}).get('sender')!=account:
        raise ContractError('legacy_document_verified_sender_changed')
    if state=='sent_verified'and not b.get('sent_message_id'):raise ContractError('legacy_document_sent_message_id_missing')
    target={'email':email}
    if b.get('firm'):target['company']=b['firm']
    return {'action':state,'target':target,'reason':'unused'}


def operation(authority,project,account,request):
    receipt=authority.read_operation(project,account,request['operation_id'])
    with authority.lifecycle.connect()as c:
        row=c.execute('SELECT payload FROM outreach_source_changes WHERE scope=? AND operation_id=?',
            (scope_key(project,account),request['operation_id'])).fetchone()
    reason=json.loads(row[0]).get('change',{}).get('reason','')
    expected={'document':request['document'],'previous_sha256':request['previous_sha256'],'sha256':request['sha256']}
    if (not reason.startswith(PREFIX) or json.loads(reason[len(PREFIX):])!=expected
            or receipt['original_sha256']!=request['sha256']):
        raise ContractError('legacy_document_retry_metadata_or_payload_changed')
    return receipt


def parse(name,raw):
    if name.endswith('.jsonl'):
        return [json.loads(line)for line in raw.decode().splitlines()]
    return json.loads(raw)


def write(authority,project,account,request):
    raw=base64.b64decode(request['raw_base64'],validate=True)
    if not raw:raise ContractError('legacy_full_document_original_required')
    name=request['document']
    with authority.hold(project,account,exclusive=True):
        previous=read(authority,project,account,name)
        # Exact intentional retry: inspect this same operation only; never
        # derive a fresh revision/action to push a stale document through.
        try:receipt=operation(authority,project,account,{**request,'sha256':digest(raw)})
        except ContractError as error:
            if str(error)!='central_source_operation_unknown_readback_only':raise
        else:
            return receipt
        if previous['sha256']!=request['previous_sha256']:raise ContractError('legacy_document_compare_and_swap_changed')
        old=parse(name,base64.b64decode(previous['raw_base64']))if previous['exists']else None
        new=parse(name,raw)
        change=_change(name,old,new,account)
        change['reason']=PREFIX+canonical({'document':name,'previous_sha256':previous['sha256'],'sha256':digest(raw)})
        return authority.apply(project,account,change,writer_id='source-console',operation_id=request['operation_id'],
            expected_revision=request['expected_revision'],original=raw)
