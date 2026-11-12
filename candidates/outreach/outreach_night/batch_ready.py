"""N fully checked members before any first-contact Draft Attempt.

Domain evidence for the existing native scheduler, not a second lifecycle.
Only recorded native quality plus current factual gates can seal a batch.
"""
from datetime import datetime, timezone
import json

from background_jobs.contracts import ContractError, approved_path, canonical, digest, private_file
from background_jobs.outreach_candidate import checked_live_gate, content, draft_raw, native_plan, required_live_checks
from .pool_sources import family

CONTRACT='hermes-outreach-ready-batch-v1'


def _phase(store,c,identity,phase):
    """Read the real native completion anchor and its exact local destination."""
    job=identity['job_id'];item=identity['item']['item_id']
    row=c.execute('''SELECT p.native_job_id,l.state,l.idempotency_key,l.job_kind,
        l.owner,l.lease_generation,l.terminal_at,l.verification_evidence,b.proof
        FROM background_phases p JOIN lifecycle_jobs l ON l.job_id=p.native_job_id
        LEFT JOIN background_proofs b ON b.native_job_id=p.native_job_id
        WHERE p.job_id=? AND p.item_id=? AND p.phase=?''',(job,item,phase)).fetchone()
    if not row or row['state']!='completed'or not row['proof']:
        raise ContractError('batch_native_preparation_incomplete')
    expected={**identity,'phase':phase}
    source_ref=str(store.snapshot_path(job,item).relative_to(store.root))
    ref=source_ref if phase=='source'else 'results/'+job+'/'+digest(item)+'/analysis.json'
    proof=json.loads(row['proof'])
    proof_sha=digest(row['proof'].encode())
    if (not isinstance(proof,dict)or canonical(proof)!=row['proof']or
        proof.get('identity')!=expected or proof.get('state')!='verified'or proof.get('destination_id')!=ref or
        row['idempotency_key']!='background:'+digest([job,item,phase])or row['job_kind']!='background_'+phase or
        row['verification_evidence']!='readback_sha256:'+proof_sha or not row['terminal_at']):
        raise ContractError('batch_native_preparation_proof_changed')
    raw=approved_path(store.root,ref).read_bytes();file_sha=digest(raw)
    if proof.get('proof_sha256')!=file_sha or (phase=='source'and file_sha!=identity['item']['source_hash']):
        raise ContractError('batch_native_preparation_file_changed')
    binding={'native_job_id':row['native_job_id'],'proof':proof,'proof_json_sha256':proof_sha,
        'file_ref':ref,'file_sha256':file_sha,'verification_evidence':row['verification_evidence'],
        'completion':{key:row[key]for key in ('idempotency_key','job_kind','owner','lease_generation','terminal_at')}}
    return binding,raw


def _preparation(store,c,identity):
    source,original=_phase(store,c,identity,'source')
    analysis,raw=_phase(store,c,identity,'analysis')
    packet=json.loads(raw)
    if (not isinstance(packet,dict)or packet.get('identity')!={**identity,'phase':'analysis'}or
        packet.get('result_hash')!=digest(packet.get('result'))):
        raise ContractError('batch_native_analysis_packet_changed')
    return {'source':source,'analysis':analysis},original,packet['result']


def _locked_scope(store,c,batch,pool):
    """Same DB fence as Store/OutreachPool, without a display-revision pin."""
    job=pool['job_id']
    row=c.execute('SELECT * FROM background_jobs WHERE job_id=?',(job,)).fetchone()
    actual=c.execute('SELECT contract FROM outreach_pool_contracts WHERE job_id=?',(job,)).fetchone()
    if (not row or not actual or row['contract']!=canonical(batch)or actual[0]!=canonical(pool)or
        row['owner']!=batch['owner']or row['session_id']!=batch['session_id']or
        row['scope_hash']!=digest(batch)or row['account']!=batch['account']or row['profile']!=batch['profile']):
        raise ContractError('batch_ready_scope_changed')
    if row['cancel_requested']or row['state']in ('cancelled','cancelling')or c.execute(
        'SELECT 1 FROM background_revision_requests WHERE parent_job=?',(job,)).fetchone():
        raise ContractError('outreach_cancelled')
    for item in batch['items']:
        actual_item=c.execute('SELECT identity FROM background_items WHERE job_id=? AND item_id=?',(job,item['item_id'])).fetchone()
        if not actual_item or actual_item[0]!=canonical(item):raise ContractError('batch_ready_item_scope_changed')


def _attempts(c,job):
    return c.execute('''SELECT p.item_id,p.native_job_id,l.state,l.verification_evidence,b.proof
        FROM background_phases p JOIN background_attempts a ON a.native_job_id=p.native_job_id
        JOIN lifecycle_jobs l ON l.job_id=p.native_job_id
        LEFT JOIN background_proofs b ON b.native_job_id=p.native_job_id
        WHERE p.job_id=? AND p.phase='draft' ''',(job,)).fetchall()


def _main_original(store,batch,pool,value):
    from background_jobs.sales_admission import _checked
    ref=value['main_handoff_ref'];raw=approved_path(store.root,ref).read_bytes();packet=json.loads(raw)
    if (digest(raw)!=value['main_handoff_sha256']or canonical(packet).encode()!=raw or
        ref!='main-sales-handoffs/'+digest(packet)+'.json'or packet.get('contract')!='hermes-main-sales-handoff-v1'or
        packet.get('job_id')!=pool['job_id']or packet.get('scope_hash')!=digest(batch)or
        packet.get('pool_contract_sha256')!=digest(pool)):
        raise ContractError('batch_main_original_changed')
    _checked(packet['request'],batch,pool)


def _members_checked(store,c,batch,pool,value):
    from .binding import _recorded_quality
    seen={key:set()for key in ('item_id','mailbox','organizational_domain','family_id')}
    for member in value['members']:
        candidate=next((p for p in pool['candidates']if p['item_id']==member.get('item_id')),None)
        item=next((i for i in batch['items']if i['item_id']==member.get('item_id')),None)
        if (not candidate or not item or member.get('mailbox')!=candidate['to'].strip().casefold()or
            member.get('organizational_domain')!=family(candidate['to'])or member.get('family_id')!=candidate['family_id']or
            member.get('source_sha256')!=item['source_hash']or member.get('source_version')!=item['source_version']):
            raise ContractError('batch_ready_member_changed')
        for key in seen:
            if member.get(key)in seen[key]:raise ContractError('batch_duplicate_mailbox_domain_or_family')
            seen[key].add(member.get(key))
        identity={'job_id':pool['job_id'],'scope_hash':digest(batch),'item':item,'phase':'draft'}
        prepared,original,saved_result=_preparation(store,c,identity)
        if (prepared!=member.get('native_preparation')or member.get('source_ref')!=prepared['source']['file_ref']or
            member.get('analysis_ref')!=prepared['analysis']['file_ref']or member.get('analysis_sha256')!=prepared['analysis']['file_sha256']):
            raise ContractError('batch_native_preparation_changed')
        result,raw_source,actual=_recorded_quality(store,identity)
        if actual!=candidate or raw_source!=original or canonical(result)!=canonical(saved_result):
            raise ContractError('batch_ready_member_changed')
        plan=native_plan(result,identity,candidate,raw_source)
        if plan is None or digest(content(draft_raw(identity,plan)))!=member['content_sha256']:
            raise ContractError('batch_ready_content_changed')
        checked_live_gate(store,identity,candidate,member['content_sha256'],None,member['readiness'],required_live_checks(identity,plan['proposed_slots']))
    return seen


def _scope(store,job):
    from background_jobs.sales_admission import _bound
    row=store.row(job);batch=json.loads(row['contract'])
    if row['cancel_requested']:raise ContractError('outreach_cancelled')
    _,pool=_bound(store,job,batch['owner'],batch['session_id'])
    return batch,pool


def seal_ready_batch(store,policy,job_id,identities,live_check,check_current):
    from .binding import _recorded_quality,_current_admission
    batch,pool=_scope(store,job_id)
    if not isinstance(identities,list)or len(identities)!=pool['target']:raise ContractError('batch_exact_n_required')
    with store.lifecycle.connect()as c:
        c.execute('BEGIN')
        _locked_scope(store,c,batch,pool)
        if _attempts(c,job_id):raise ContractError('batch_must_precede_first_draft_attempt')
    entries=[];mailboxes=set();domains=set();families=set();items=set();handoff=None
    for identity in identities:
        if (identity.get('job_id')!=job_id or identity.get('scope_hash')!=digest(batch)or identity.get('phase')!='draft'or
            identity.get('item')not in batch['items']):raise ContractError('batch_item_scope_changed')
        admission=_current_admission(store,policy,identity,check_current)
        if handoff is not None and admission!=handoff:raise ContractError('batch_current_main_changed')
        handoff=admission
        with store.lifecycle.connect()as c:
            c.execute('BEGIN')
            prepared,saved_original,saved_result=_preparation(store,c,identity)
        result,original,candidate=_recorded_quality(store,identity)
        if candidate not in pool['candidates']or original!=saved_original or canonical(result)!=canonical(saved_result):
            raise ContractError('batch_candidate_not_permitted')
        plan=native_plan(result,identity,candidate,original)
        if plan is None:raise ContractError('batch_quality_not_ready')
        # Proposed holds need their real owner-bound transaction before a ready
        # receipt. Do not label a future calendar insertion as already verified.
        mailbox=candidate['to'].strip().casefold();domain=family(mailbox)
        if (candidate['item_id']in items or mailbox in mailboxes or domain in domains or candidate['family_id']in families):
            raise ContractError('batch_duplicate_mailbox_domain_or_family')
        expected=digest(content(draft_raw(identity,plan)))
        gate=live_check(identity,plan)
        checked_live_gate(store,identity,candidate,expected,None,gate,required_live_checks(identity,plan['proposed_slots']))
        if _current_admission(store,policy,identity,check_current)!=handoff:raise ContractError('batch_current_main_changed')
        entries.append({'item_id':candidate['item_id'],'mailbox':mailbox,'organizational_domain':domain,
            'family_id':candidate['family_id'],'source_sha256':identity['item']['source_hash'],
            'source_version':identity['item']['source_version'],'source_ref':prepared['source']['file_ref'],
            'analysis_ref':prepared['analysis']['file_ref'],'analysis_sha256':prepared['analysis']['file_sha256'],
            'native_preparation':prepared,
            'content_sha256':expected,'readiness':gate,'prepared_at':datetime.now(timezone.utc).isoformat()})
        items.add(candidate['item_id']);mailboxes.add(mailbox);domains.add(domain);families.add(candidate['family_id'])
    value={'contract':CONTRACT,'job_id':job_id,'scope_hash':digest(batch),'main_request_id':batch['request_id'],
        'target':pool['target'],'pool_sha256':digest(pool['candidates']),'source_manifest_sha256':batch['manifest_hash'],
        'project':batch['items'][0]['category'],'account':batch['account'],'members':entries,
        'preparation_binding_version':1,'main_handoff_ref':'main-sales-handoffs/'+digest(handoff)+'.json',
        'main_handoff_sha256':digest(canonical(handoff).encode())}
    raw=canonical(value).encode();ref='outreach-ready-batches/'+digest(raw)+'.json'
    try:private_file(store.root/ref,raw)
    except FileExistsError:
        if (store.root/ref).read_bytes()!=raw:raise ContractError('batch_ready_original_changed')
    with store.lifecycle.connect()as c:
        c.execute('BEGIN IMMEDIATE')
        _locked_scope(store,c,batch,pool)
        if policy.get('enabled')is not True or policy.get('outreach_profile_enabled')is not True:
            raise ContractError('outreach_staging_profile_not_activated')
        _main_original(store,batch,pool,value)
        _members_checked(store,c,batch,pool,value)
        c.execute('CREATE TABLE IF NOT EXISTS outreach_ready_batches(job_id TEXT PRIMARY KEY,ref TEXT NOT NULL,sha256 TEXT NOT NULL)')
        old=c.execute('SELECT ref,sha256 FROM outreach_ready_batches WHERE job_id=?',(job_id,)).fetchone()
        if old and (old[0],old[1])!=(ref,digest(raw)):raise ContractError('batch_ready_original_changed')
        attempts=_attempts(c,job_id)
        if attempts and not old:raise ContractError('batch_must_precede_first_draft_attempt')
        c.execute('INSERT OR IGNORE INTO outreach_ready_batches VALUES(?,?,?)',(job_id,ref,digest(raw)))
    return {'ref':ref,'sha256':digest(raw),'batch':value}


def checked_ready_batch(store,identity):
    """Historical preparation provenance, never a current Draft write grant."""
    batch,pool=_scope(store,identity['job_id'])
    with store.lifecycle.connect()as c:
        c.execute('BEGIN')
        _locked_scope(store,c,batch,pool)
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table'AND name='outreach_ready_batches'").fetchone()
        row=_active_anchor(store,c,batch,pool)if exists else None
        if not row:raise ContractError('exact_n_ready_batch_required')
        raw=approved_path(store.root,row[0]).read_bytes();value=json.loads(raw)
        if (digest(raw)!=row[1]or canonical(value).encode()!=raw or value.get('contract')!=CONTRACT or
        value.get('job_id')!=identity['job_id']or value.get('scope_hash')!=digest(batch)or identity.get('scope_hash')!=digest(batch)or
        value.get('main_request_id')!=batch['request_id']or value.get('target')!=pool['target']or
        value.get('pool_sha256')!=digest(pool['candidates'])or value.get('source_manifest_sha256')!=batch['manifest_hash']or
        value.get('account')!=batch['account']or value.get('project')!=identity['item']['category']or
        identity.get('item')not in batch['items']or identity.get('phase')!='draft'or value.get('preparation_binding_version')!=1 or
        not isinstance(value.get('members'),list)or len(value['members'])!=pool['target']):raise ContractError('batch_ready_scope_changed')
        _main_original(store,batch,pool,value)
        seen=_members_checked(store,c,batch,pool,value)
        if identity['item']['item_id']not in seen['item_id']:raise ContractError('draft_not_in_ready_batch')
    return {'ref':row[0],'sha256':row[1],'batch':value}


def _active_anchor(store,c,batch,pool):
    """Follow append-only successors without rewriting the original N anchor."""
    row=c.execute('SELECT ref,sha256 FROM outreach_ready_batches WHERE job_id=?',(pool['job_id'],)).fetchone()
    if not row:return None
    head=(row[0],row[1]);visited=set()
    exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table'AND name='outreach_ready_batch_successors'").fetchone()
    if not exists:return head
    while True:
        if head[1]in visited or len(visited)>500:raise ContractError('batch_ready_successor_cycle')
        visited.add(head[1])
        rows=c.execute('SELECT ref,sha256 FROM outreach_ready_batch_successors WHERE job_id=? AND predecessor_sha256=?',
                       (pool['job_id'],head[1])).fetchall()
        if not rows:return head
        if len(rows)!=1:raise ContractError('batch_ready_successor_fork')
        old_raw=approved_path(store.root,head[0]).read_bytes();raw=approved_path(store.root,rows[0][0]).read_bytes()
        if digest(old_raw)!=head[1]or digest(raw)!=rows[0][1]:raise ContractError('batch_ready_original_changed')
        old=json.loads(old_raw);value=json.loads(raw)
        if canonical(value).encode()!=raw:raise ContractError('batch_ready_original_changed')
        _transition_checked(store,c,batch,pool,head,old,value)
        head=(rows[0][0],rows[0][1])


def _completed_attempts(store,c,batch,pool):
    from background_jobs.outreach_pool import OutreachPool
    complete={};by_id={i['item_id']:i for i in batch['items']}
    for attempt in _attempts(c,pool['job_id']):
        item_id=attempt['item_id'];candidate=next(v for v in pool['candidates']if v['item_id']==item_id)
        identity={'job_id':pool['job_id'],'scope_hash':digest(batch),'item':by_id[item_id],'phase':'draft'}
        if (attempt['state']!='completed'or not attempt['proof']or
            attempt['verification_evidence']!='readback_sha256:'+digest(attempt['proof'].encode())):
            raise ContractError('batch_successor_unknown_write_requires_readback')
        receipt=OutreachPool(store)._phase_receipt(c,identity,candidate)
        result=c.execute('SELECT state,receipt FROM outreach_pool_results WHERE job_id=? AND item_id=?',
                         (pool['job_id'],item_id)).fetchone()
        if not result or result['state']!='verified'or result['receipt']!=canonical(receipt):
            raise ContractError('batch_successor_completed_member_not_counted')
        complete[item_id]=receipt
    return complete


def _transition_checked(store,c,batch,pool,previous,old,value,*,sealing=False):
    if value.get('predecessor')!={'ref':previous[0],'sha256':previous[1]}:
        raise ContractError('batch_ready_successor_predecessor_changed')
    stable=set(old)-{'members','predecessor','replacement_decisions','current_readiness','completed_receipts'}
    if any(value.get(k)!=old[k]for k in stable):raise ContractError('batch_ready_successor_scope_changed')
    before={m['item_id']:m for m in old['members']};after={m['item_id']:m for m in value['members']}
    removed=set(before)-set(after);added=set(after)-set(before)
    if not removed or len(removed)!=len(added)or len(after)!=pool['target']:
        raise ContractError('batch_ready_successor_exact_replacement_required')
    if any(c.execute('SELECT 1 FROM outreach_pool_results WHERE job_id=? AND item_id=?',(pool['job_id'],rid)).fetchone()for rid in added):
        raise ContractError('batch_ready_successor_replacement_already_terminal')
    if any(before[k]!=after[k]for k in set(before)&set(after)):
        raise ContractError('batch_ready_successor_retained_member_changed')
    completed=value.get('completed_receipts')
    if not isinstance(completed,dict):raise ContractError('batch_ready_successor_completed_receipts_required')
    if sealing:
        if completed!=_completed_attempts(store,c,batch,pool):raise ContractError('batch_ready_successor_completed_receipts_changed')
    else:
        # Historical preparation must remain readable after a future member's
        # own first Attempt. This never grants permission for that new write.
        from background_jobs.outreach_pool import OutreachPool
        for rid,receipt in completed.items():
            candidate=next((v for v in pool['candidates']if v['item_id']==rid),None)
            item=next((i for i in batch['items']if i['item_id']==rid),None)
            if not candidate or not item:raise ContractError('batch_ready_successor_completed_receipts_changed')
            identity={'job_id':pool['job_id'],'scope_hash':digest(batch),'item':item,'phase':'draft'}
            if receipt!=OutreachPool(store)._phase_receipt(c,identity,candidate):
                raise ContractError('batch_ready_successor_completed_receipts_changed')
    if not set(completed)<=set(after)or set(completed)&(removed|added):
        raise ContractError('batch_ready_successor_cannot_replace_attempted_member')
    decisions=value.get('replacement_decisions')
    if not isinstance(decisions,list)or {d.get('item_id')for d in decisions}!=removed or len(decisions)!=len(removed):
        raise ContractError('batch_ready_successor_rejection_required')
    for decision in decisions:
        rid=decision['item_id']
        if c.execute('SELECT 1 FROM background_phases p JOIN background_attempts a USING(native_job_id) WHERE p.job_id=? AND p.item_id=? AND p.phase=\'draft\'',
                     (pool['job_id'],rid)).fetchone():raise ContractError('batch_ready_successor_cannot_replace_attempted_member')
        actual=c.execute('SELECT item_id,state,reason FROM outreach_pool_results WHERE job_id=? AND item_id=?',
                         (pool['job_id'],rid)).fetchone()
        if not actual or dict(actual)!=decision or decision['state']not in ('blocked','rejected')or not decision['reason']:
            raise ContractError('batch_ready_successor_rejection_changed')
    gates=value.get('current_readiness')
    if not isinstance(gates,list)or len(gates)!=pool['target']or {g.get('item_id')for g in gates}!=set(after):
        raise ContractError('batch_ready_successor_current_n_required')
    for current in gates:
        rid=current['item_id'];item=next(i for i in batch['items']if i['item_id']==rid)
        candidate=next(v for v in pool['candidates']if v['item_id']==rid)
        identity={'job_id':pool['job_id'],'scope_hash':digest(batch),'item':item,'phase':'draft'}
        expected_draft=completed.get(rid,{}).get('draft_id')
        if current.get('existing_draft_id')!=expected_draft:raise ContractError('batch_ready_successor_owned_draft_changed')
        checked_live_gate(store,identity,candidate,after[rid]['content_sha256'],expected_draft,current.get('readiness'),required_live_checks(identity))


def seal_ready_successor(store,policy,job_id,identities,live_check,check_current):
    """Replace only rejected, unattempted members from the acquired same scope.

    No SourceManifest, contract, completed receipt, first anchor or old file is
    mutated. Unknown writes block the transition regardless of remaining pool.
    """
    from .binding import _recorded_quality,_current_admission
    batch,pool=_scope(store,job_id)
    if not isinstance(identities,list)or len(identities)!=pool['target']:raise ContractError('batch_exact_n_required')
    with store.lifecycle.connect()as c:
        c.execute('BEGIN');_locked_scope(store,c,batch,pool)
        previous=_active_anchor(store,c,batch,pool)
        if not previous:raise ContractError('exact_n_ready_batch_required')
        raw=approved_path(store.root,previous[0]).read_bytes()
        if digest(raw)!=previous[1]:raise ContractError('batch_ready_original_changed')
        old=json.loads(raw);_main_original(store,batch,pool,old);_members_checked(store,c,batch,pool,old)
        completed=_completed_attempts(store,c,batch,pool)
        before={m['item_id']:m for m in old['members']};requested={i['item']['item_id']for i in identities}
        decisions=[]
        for rid in set(before)-requested:
            row=c.execute('SELECT item_id,state,reason FROM outreach_pool_results WHERE job_id=? AND item_id=?',(job_id,rid)).fetchone()
            if not row:raise ContractError('batch_ready_successor_rejection_required')
            decisions.append(dict(row))
    members=[];gates=[]
    for identity in identities:
        if identity.get('job_id')!=job_id or identity.get('scope_hash')!=digest(batch)or identity.get('phase')!='draft'or identity.get('item')not in batch['items']:
            raise ContractError('batch_item_scope_changed')
        _current_admission(store,policy,identity,check_current)
        result,original,candidate=_recorded_quality(store,identity);plan=native_plan(result,identity,candidate,original)
        if plan is None:raise ContractError('batch_quality_not_ready')
        rid=candidate['item_id'];expected=digest(content(draft_raw(identity,plan)))
        draft_id=completed.get(rid,{}).get('draft_id')
        gate=live_check(identity,plan,existing_draft_id=draft_id)
        checked_live_gate(store,identity,candidate,expected,draft_id,gate,required_live_checks(identity,plan['proposed_slots']))
        gates.append({'item_id':rid,'existing_draft_id':draft_id,'readiness':gate})
        if rid in before:members.append(before[rid]);continue
        with store.lifecycle.connect()as c:
            c.execute('BEGIN');prepared,_,_=_preparation(store,c,identity)
        members.append({'item_id':rid,'mailbox':candidate['to'].strip().casefold(),'organizational_domain':family(candidate['to']),
            'family_id':candidate['family_id'],'source_sha256':identity['item']['source_hash'],'source_version':identity['item']['source_version'],
            'source_ref':prepared['source']['file_ref'],'analysis_ref':prepared['analysis']['file_ref'],
            'analysis_sha256':prepared['analysis']['file_sha256'],'native_preparation':prepared,'content_sha256':expected,
            'readiness':gate,'prepared_at':datetime.now(timezone.utc).isoformat()})
    value={**old,'members':members,'predecessor':{'ref':previous[0],'sha256':previous[1]},
           'replacement_decisions':sorted(decisions,key=lambda d:d['item_id']),'current_readiness':gates,'completed_receipts':completed}
    raw=canonical(value).encode();ref='outreach-ready-batches/'+digest(raw)+'.json'
    try:private_file(store.root/ref,raw)
    except FileExistsError:
        if approved_path(store.root,ref).read_bytes()!=raw:raise ContractError('batch_ready_original_changed')
    with store.lifecycle.connect()as c:
        c.execute('BEGIN IMMEDIATE');_locked_scope(store,c,batch,pool)
        if policy.get('enabled')is not True or policy.get('outreach_profile_enabled')is not True:raise ContractError('outreach_staging_profile_not_activated')
        if _active_anchor(store,c,batch,pool)!=previous:raise ContractError('batch_ready_successor_raced')
        _main_original(store,batch,pool,value);_members_checked(store,c,batch,pool,value)
        _transition_checked(store,c,batch,pool,previous,old,value,sealing=True)
        c.execute('CREATE TABLE IF NOT EXISTS outreach_ready_batch_successors(job_id TEXT NOT NULL,predecessor_sha256 TEXT NOT NULL,ref TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(job_id,predecessor_sha256))')
        c.execute('INSERT INTO outreach_ready_batch_successors VALUES(?,?,?,?)',(job_id,previous[1],ref,digest(raw)))
    return {'ref':ref,'sha256':digest(raw),'batch':value}
