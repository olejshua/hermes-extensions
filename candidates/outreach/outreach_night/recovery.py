"""Owned draft recovery from the original fenced attempt, never a new grant.

The immutable intent is sealed after live preflight, before the first create.
It permits exact account/MIME/RFC readback after cancellation or revocation. It
does not admit analysis, fresh source capture, sending, another create or K/N.
"""
import base64
from email import policy as email_policy
from email.parser import BytesParser
import json
from pathlib import Path

from background_jobs.contracts import ContractError, PermanentFailure, approved_path, canonical, digest, private_file
from background_jobs.outreach_candidate import ACCOUNTS, GmailDraftAdapter, addresses, content


def phase(store,identity):
    row=store.row(identity['job_id']);batch=json.loads(row['contract'])
    if (identity.get('phase')!='draft' or digest(batch)!=identity.get('scope_hash') or
            identity.get('item')not in batch['items'] or batch['profile']not in
            ('outreach-drafts-staging-v1','fixture-outreach') or
            ACCOUNTS.get(identity['item'].get('category'),(None,None))[1]!=identity['item'].get('account') or
            row['owner']!=identity['item'].get('owner')):
        raise ContractError('owned_draft_scope_changed')
    with store.lifecycle.connect()as c:
        found=c.execute('SELECT native_job_id FROM background_phases WHERE job_id=? AND item_id=? AND phase=?',
            (identity['job_id'],identity['item']['item_id'],'draft')).fetchone()
    return found[0]if found else None


def intent_ref(identity):return 'outreach-intents/'+digest(identity)+'.json'


def _raw_checked(raw,identity,candidate):
    parsed=BytesParser(policy=email_policy.default).parsebytes(raw)
    complete=content(raw)
    if (candidate.get('item_id')!=identity['item']['item_id'] or
        addresses(parsed,'From')!=[identity['item']['account']]or addresses(parsed,'To')!=[candidate.get('to')]or
        addresses(parsed,'Cc')or addresses(parsed,'Bcc')or
        complete['headers']['message-id']!='<ole-outreach-'+digest(identity)+'@draft.local>'):
        raise ContractError('owned_draft_original_mime_scope_changed')
    return complete


def _source_checked(store,identity,candidate):
    original=store.snapshot_path(identity['job_id'],identity['item']['item_id']).read_bytes()
    if digest(original)!=identity['item']['source_hash']:
        raise ContractError('owned_draft_source_changed')
    source=json.loads(original)
    if source.get('contract')!='outreach-originals-v1'or source.get('candidate')!=candidate:
        raise ContractError('owned_draft_source_candidate_changed')
    binding=source.get('source_binding')
    if binding and any(binding.get(k)!=identity['item'].get(k)for k in ('owner','account','category')):
        raise ContractError('owned_draft_source_binding_changed')
    return original


def seal_intent(store,identity,candidate,raw,slots,original_sha256,*,original=None):
    native=phase(store,identity)
    if not native or not store.attempted(native):raise ContractError('owned_draft_native_attempt_required')
    if original is not None:
        if digest(original)!=original_sha256 or original_sha256!=identity['item']['source_hash']:
            raise ContractError('owned_draft_source_changed')
        # The adapter already validated these exact immutable source bytes in
        # native_plan. Preserve them before create even for a direct driver that
        # did not call the separate source adapter. Recovery never fills gaps.
        target=store.snapshot_path(identity['job_id'],identity['item']['item_id'])
        try:private_file(target,original)
        except FileExistsError:
            if target.read_bytes()!=original:raise ContractError('owned_draft_source_changed')
    original=_source_checked(store,identity,candidate)
    if digest(original)!=original_sha256:raise ContractError('owned_draft_source_changed')
    complete=_raw_checked(raw,identity,candidate)
    packet={'contract':'outreach-owned-intent-v1','identity':identity,'native_phase_id':native,
        'candidate':candidate,'raw_base64':base64.b64encode(raw).decode(),'raw_sha256':digest(raw),
        'content_sha256':digest(complete),'source_snapshot_sha256':original_sha256,'proposed_slots':slots}
    data=canonical(packet).encode();target=store.root/intent_ref(identity)
    try:private_file(target,data)
    except FileExistsError:
        if target.read_bytes()!=data:raise ContractError('owned_draft_intent_changed')
    # Tie the immutable file to the existing native attempt ledger. Rewriting
    # its JSON and self-rehashing must not manufacture another owned target.
    with store.lifecycle.connect()as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('CREATE TABLE IF NOT EXISTS outreach_owned_intents(native_phase_id TEXT PRIMARY KEY,sha256 TEXT NOT NULL)')
        old=c.execute('SELECT sha256 FROM outreach_owned_intents WHERE native_phase_id=?',(native,)).fetchone()
        if old and old[0]!=digest(data):raise ContractError('owned_draft_intent_changed')
        c.execute('INSERT OR IGNORE INTO outreach_owned_intents VALUES(?,?)',(native,digest(data)))
    return packet


def load_intent(store,identity):
    native=phase(store,identity)
    if not native or not store.attempted(native):raise ContractError('owned_draft_native_attempt_required')
    data=approved_path(store.root,intent_ref(identity)).read_bytes()
    if len(data)>8*1024*1024:raise ContractError('owned_draft_intent_too_large')
    packet=json.loads(data)
    with store.lifecycle.connect()as c:
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table'AND name='outreach_owned_intents'").fetchone()
        anchor=c.execute('SELECT sha256 FROM outreach_owned_intents WHERE native_phase_id=?',(native,)).fetchone()if exists else None
    if not anchor or anchor[0]!=digest(data):raise ContractError('owned_draft_intent_unanchored_or_changed')
    if (packet.get('contract')!='outreach-owned-intent-v1'or packet.get('identity')!=identity or
        packet.get('native_phase_id')!=native or canonical(packet).encode()!=data):
        raise ContractError('owned_draft_intent_changed')
    original=_source_checked(store,identity,packet['candidate'])
    raw=base64.b64decode(packet['raw_base64'],validate=True)
    if (digest(original)!=packet['source_snapshot_sha256']or digest(raw)!=packet['raw_sha256']or
        digest(_raw_checked(raw,identity,packet['candidate']))!=packet['content_sha256']):
        raise ContractError('owned_draft_original_changed')
    return packet,raw,native


class OwnedDraftReadback(GmailDraftAdapter):
    """Actual existing Gmail readback methods, bound to the prior immutable MIME."""
    def __init__(self,store,identity,gmail):
        packet,raw,native=load_intent(store,identity)
        self.store,self.identity,self.gmail=store,identity,gmail
        self.plan={'candidate':packet['candidate'],'proposed_slots':packet['proposed_slots']}
        self.raw,self.expected,self.attempt=raw,content(raw),native
        self.ack=store.root/'outreach-acks'/(digest(identity)+'.json')
        self.live_check=None
    def bound(self,identity):
        packet,raw,_=load_intent(self.store,identity)
        if raw!=self.raw:raise ContractError('owned_draft_intent_changed')
        super().bound(identity)
    def readback(self,identity):return self.readback_owned(identity)
    def preflight(self,identity):raise PermanentFailure('owned_draft_readback_only')
    def execute_once(self,identity,active):raise PermanentFailure('owned_draft_readback_only')
