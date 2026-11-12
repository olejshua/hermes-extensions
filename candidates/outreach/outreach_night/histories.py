"""Durable native attempts also protect against Gmail indexing delays.

No second registry: read the existing native attempt ledger and immutable pool
contracts across jobs while the account authority is held. Own phase ownership
remains the existing native lease gate's responsibility, not a string flag here.
"""
import json
from background_jobs.contracts import ContractError,PermanentFailure,digest
from .pool_sources import family
from .sources import persist


def native_source_history(root, db_path, project, account, *, own_identity=None, allowed_databases=None):
    """Read existing attempt/intent originals across a process/SQLite boundary.

    No current lease is needed to see a historical attempt. Identity and pool
    contracts are checked instead; expired/cancelled owners cannot erase it.
    Only the exact current background item may ignore its own prior attempt.
    """
    import base64
    from pathlib import Path
    import sqlite3
    from .central_sources import ACCOUNTS, evidence
    from .protections import _read
    from background_jobs.contracts import canonical
    from background_jobs.outreach_candidate import content
    from email import policy
    from email.parser import BytesParser
    from email.utils import getaddresses
    if ACCOUNTS.get(project) != account: raise ContractError('central_source_project_account_mismatch')
    root = Path(root); db_path = Path(db_path)
    if db_path.is_symlink() or not db_path.is_file(): raise ContractError('central_native_ledger_unavailable')
    c = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True); c.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = []
        if 'background_attempts' in tables:
            count = c.execute('''SELECT COUNT(*) FROM background_attempts a JOIN background_phases p USING(native_job_id)
                JOIN background_jobs j ON j.job_id=p.job_id WHERE p.phase='draft' AND j.account=?''', (account,)).fetchone()[0]
            if count and 'outreach_pool_contracts' not in tables: raise ContractError('native_attempt_history_pool_binding_missing')
            if count:
                rows = c.execute('''SELECT p.job_id,p.item_id,p.native_job_id,a.attempted,j.contract,j.scope_hash,
                    o.contract pool_contract FROM background_attempts a JOIN background_phases p USING(native_job_id)
                    JOIN background_jobs j ON j.job_id=p.job_id LEFT JOIN outreach_pool_contracts o ON o.job_id=p.job_id
                    WHERE p.phase='draft' AND j.account=?''', (account,)).fetchall()
                for row in rows:
                    if not row['pool_contract']: raise ContractError('native_prior_attempt_source_unknown')
                    batch = json.loads(row['contract']); pool = json.loads(row['pool_contract'])
                    if digest(batch) != row['scope_hash'] or pool.get('scope_hash') != row['scope_hash'] or pool.get('job_id') != row['job_id']:
                        raise ContractError('native_prior_attempt_source_changed')
                    matches = [v for v in pool.get('candidates', []) if v.get('item_id') == row['item_id']]
                    if len(matches) != 1: raise ContractError('native_prior_attempt_source_unknown')
                    prior = matches[0]
                    own = (isinstance(own_identity, dict) and own_identity.get('job_id') == row['job_id']
                           and own_identity.get('scope_hash') == row['scope_hash']
                           and own_identity.get('item', {}).get('item_id') == row['item_id'])
                    if own: continue
                    target = {k: prior[k] for k in ('company_id', 'family_id')}; target['email'] = prior['to']
                    result.append(evidence('native-attempt:' + digest([str(db_path),row['native_job_id']]), 'attempt_unknown', target,
                        operation_id=row['native_job_id'], original={'db': str(db_path), 'row': dict(row)}))
        for path in sorted((root / 'native-write-intents').glob('*.json')):
            raw, stat = _read(path); intent = json.loads(raw); bound = intent.get('scope', {})
            if bound.get('account') != account: continue
            if bound.get('native_db')not in (allowed_databases or [str(db_path)]):
                raise ContractError('central_native_unregistered_intent_database')
            if bound.get('native_db')!=str(db_path):continue
            if (intent.get('contract') != 'native-existing-job-write-intent-v2'
                    or path.stem != digest([str(db_path), bound.get('native_id')])):
                raise ContractError('central_native_intent_binding_changed')
            identity = bound.get('identity', {})
            if bound.get('kind') == 'background':
                actual = c.execute('''SELECT p.job_id,p.item_id,j.scope_hash,i.identity FROM background_phases p
                    JOIN background_jobs j ON j.job_id=p.job_id JOIN background_items i ON i.job_id=p.job_id AND i.item_id=p.item_id
                    WHERE p.native_job_id=?''', (bound['native_id'],)).fetchone()
                if (not actual or actual['job_id'] != identity.get('job_id') or actual['scope_hash'] != identity.get('scope_hash')
                        or json.loads(actual['identity']) != identity.get('item') or identity['item']['category'] != project):
                    raise ContractError('central_native_intent_binding_changed')
            elif bound.get('kind') == 'gtm':
                key = dict(identity); key.pop('source_hash', None)
                workflow = digest(json.dumps(key, sort_keys=True).encode())
                actual = c.execute('SELECT identity FROM gtm_sources WHERE workflow_id=?', (workflow,)).fetchone()
                if not actual or json.loads(actual[0]) != identity or identity.get('category') != project:
                    raise ContractError('central_native_intent_binding_changed')
            else: raise ContractError('central_native_intent_binding_changed')
            request = intent['request']
            message = request['body']['message']
            mime = base64.b64decode(message['raw'] + '=' * (-len(message['raw']) % 4), altchars=b'-_', validate=True)
            if (digest(request) != intent['request_sha256'] or digest(mime) != intent['raw_sha256']
                    or digest(content(mime)) != intent['content_sha256']):
                raise ContractError('central_native_intent_content_changed')
            parsed = BytesParser(policy=policy.default).parsebytes(mime)
            recipients = [v.casefold() for _, v in getaddresses(parsed.get_all('To', []) + parsed.get_all('Cc', []))]
            if not recipients: raise ContractError('central_native_intent_target_missing')
            own = (bound.get('kind') == 'background' and isinstance(own_identity, dict)
                   and all(identity.get(k) == own_identity.get(k) for k in ('job_id', 'scope_hash', 'item')))
            if own: continue
            originals = {'db': str(db_path), 'intent': intent, 'intent_sha256': digest(raw), 'file_identity': stat}
            state = 'attempt_unknown'
            ack_path = root / 'native-write-acks' / path.name
            ack = None
            if ack_path.exists():
                ack_raw, _ = _read(ack_path); ack = json.loads(ack_raw)
                if ack.get('request_sha256') != intent['request_sha256'] or ack.get('intent_ref') != str(path.relative_to(root)):
                    raise ContractError('central_native_ack_binding_changed')
                originals['ack'] = ack; originals['ack_sha256'] = digest(ack_raw); state = 'draft_ack'
            readback_path = root / 'native-write-readbacks' / path.name
            if readback_path.exists():
                readback_raw, _ = _read(readback_path); rb = json.loads(readback_raw); response = rb.get('response', {})
                actual_message = response.get('message', {})
                if (rb.get('request_sha256') != intent['request_sha256'] or rb.get('intent_ref') != str(path.relative_to(root))
                        or rb.get('content_sha256') != intent['content_sha256'] or 'DRAFT' not in actual_message.get('labelIds', [])
                        or 'SENT' in actual_message.get('labelIds', [])):
                    raise ContractError('central_native_readback_binding_changed')
                actual_mime = base64.b64decode(actual_message['raw'] + '=' * (-len(actual_message['raw']) % 4), altchars=b'-_', validate=True)
                if (digest(content(actual_mime)) != intent['content_sha256'] or digest(actual_mime) != rb['raw_sha256']
                        or (message.get('threadId') is not None and actual_message.get('threadId') != message['threadId'])
                        or (request.get('id') is not None and response.get('id') != request['id'])
                        or (ack and ack['response'].get('id') != response.get('id'))):
                    raise ContractError('central_native_readback_binding_changed')
                originals['readback'] = rb; originals['readback_sha256'] = digest(readback_raw); state = 'draft_verified'
            # Present code admits Draft only. Never relabel ACK as delivered.
            if intent['operation'] not in {'users.drafts.create', 'users.drafts.update'}:
                raise ContractError('central_native_unreviewed_send_operation')
            for recipient in recipients:
                result.append(evidence('native-intent:' + path.stem + ':' + recipient, state, {'email': recipient},
                                       operation_id=bound['native_id'], original=originals))
        coverage=[]
        if {'background_attempts','background_phases','background_jobs'}<=tables:coverage.append('background')
        if {'gtm_attempts','gtm_sources'}<=tables:coverage.append('gtm')
        return {'evidence':result,'coverage':coverage,'root':str(root),'db':str(db_path)}
    finally: c.close()


def check_native_attempt_history(store,identity,candidate):
    with store.lifecycle.connect()as c:
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table'AND name='outreach_pool_contracts'").fetchone()
        if not exists:raise ContractError('native_attempt_history_pool_binding_missing')
        rows=c.execute('''SELECT p.job_id,p.item_id,p.native_job_id,a.attempted,j.contract,j.scope_hash,j.account,
            o.contract AS pool_contract FROM background_attempts a JOIN background_phases p USING(native_job_id)
            JOIN background_jobs j ON j.job_id=p.job_id LEFT JOIN outreach_pool_contracts o ON o.job_id=p.job_id
            WHERE p.phase='draft'AND j.account=?''',(identity['item']['account'],)).fetchall()
    originals=[];collision=False;bases={family(v)for v in candidate['family_domains']}
    for row in rows:
        if row['job_id']==identity['job_id']and row['item_id']==candidate['item_id']:
            continue # Actual owner/generation/lease/unknown gates remain in A.
        if not row['pool_contract']:raise ContractError('native_prior_attempt_source_unknown')
        batch=json.loads(row['contract']);pool=json.loads(row['pool_contract'])
        if digest(batch)!=row['scope_hash']or pool.get('scope_hash')!=row['scope_hash']or pool.get('job_id')!=row['job_id']:
            raise ContractError('native_prior_attempt_source_changed')
        matches=[p for p in pool.get('candidates',[])if p.get('item_id')==row['item_id']]
        if len(matches)!=1:raise ContractError('native_prior_attempt_source_unknown')
        prior=matches[0]
        conflict=(prior['to'].strip().casefold()==candidate['to'].strip().casefold()or
                  prior['company_id']==candidate['company_id']or prior['family_id']==candidate['family_id']or
                  bool(bases&{family(v)for v in prior['family_domains']}))
        originals.append({'native_job_id':row['native_job_id'],'job_id':row['job_id'],'item_id':row['item_id'],
                          'attempted':row['attempted'],'pool_contract_sha256':digest(pool),'candidate':prior,'collision':conflict})
        collision|=conflict
    proof=persist(store.root,'native_attempt_history','native-attempt-history:'+candidate['item_id'],
        {'account':identity['item']['account'],'original_attempts':originals,'collision':collision},
        {'identity':identity,'candidate':candidate},'existing-native-ledger.all-account-draft-attempts')
    if collision:raise PermanentFailure('native_prior_company_or_mailbox_attempt')
    return proof
