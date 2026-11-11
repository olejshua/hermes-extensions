"""One immutable case per new CODE reply, on the existing native phase store.

Review cases are terminal for automatic intake. Bounded recovery only reads back
attempted writes; a manual resume preserves every source, plan and attempt marker.
No model, mail sending, contact creation, or account/project inference exists here.
"""
from __future__ import annotations
from dataclasses import asdict,dataclass
import copy
from datetime import datetime,timedelta,timezone
import hashlib,json,re,time,uuid
from gtm.coordinator import Identity,Readback,PLANS
from gtm.reply import ExistingLeadBinding,ExistingReplyReadClient,PreparedReply,ReplySourceAdapter,prepare_new_reply,verify_existing_mapping
from gtm.twenty import NotePlan,Statement
from gtm.calendar.confirmation import Binding,CODE
from gtm.calendar.invitation import InvitationAdapter
from gtm.calendar.reply_acceptance import AcceptanceProof,SemanticAcceptanceResolver,offered_slots,thread_offer_context,offers_hash
from gtm.mail_observer.semantics import SemanticReceipts
from gtm.mail_observer.sources import OriginalMail,SourceBlocked,sha


@dataclass(frozen=True)
class RunSnapshot:
    source:OriginalMail
    occurred:str
    binding:ExistingLeadBinding
    company:dict
    sources:tuple
    proof:dict


class PhaseReads:
    """Bounded reuse of exact kind/ID reads inside one native phase only.

    Clear at phase entry, immediately before a physical write and after every
    write/unknown response. No live source, mapping or revision is carried
    across those boundaries; the underlying adapters keep their real checks.
    """
    def __init__(self,client):self.client=client;self.values={};self.before_write=None;self.gate_error=None
    def clear(self):self.values.clear()
    def get(self,kind,rid):
        key=(kind,rid)
        if key not in self.values:self.values[key]=copy.deepcopy(self.client.get(kind,rid))
        return copy.deepcopy(self.values[key])
    def write_once(self,query,variables):
        self.clear()
        if self.before_write is None:raise SourceBlocked('physical_write_gate_unbound')
        try:self.before_write()
        except Exception as error:
            self.gate_error=error;self.clear();raise
        try:return self.client.write_once(query,variables)
        finally:self.clear() # force exact positive/unknown destination readback


class ReplyCaseConsumer:
    def __init__(self,envelopes,originals,gmail,twenty,*,crm_enabled=False,invitation_enabled=False,
                 calendar=None,hold_policy=None,semantic_wait_seconds=300,clock=time.time,preview_enabled=False,version_reader=None):
        self.envelopes,self.co,self.originals,self.gmail,self.twenty=envelopes,envelopes.coordinator,originals,gmail,twenty
        self.crm_enabled,self.invitation_enabled=crm_enabled,invitation_enabled
        self.preview_enabled=preview_enabled
        if invitation_enabled and (calendar is None or hold_policy is None):
            raise ValueError('Invitations require separately reviewed calendar/hold bindings')
        self.calendar,self.policy,self.clock=calendar,hold_policy,clock
        self.cleanup=None
        self.confirmed_cleanup=None
        self.invitation_cutoff=None
        self.version_reader=version_reader or (lambda:'v2.42.6')
        self.wait=semantic_wait_seconds;self.receipts=SemanticReceipts(self.co)
        with self.co.store.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS gtm_reply_cases(
                message_id TEXT PRIMARY KEY,job_id TEXT UNIQUE NOT NULL,state TEXT NOT NULL,
                reason TEXT,source_hash TEXT,binding TEXT,business_proof TEXT,payload TEXT,
                planned INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,next_check REAL NOT NULL,
                recovery_checks INTEGER NOT NULL DEFAULT 0)''')
    def _update(self,mid,**values):
        with self.co.store.connect() as c:
            c.execute('UPDATE gtm_reply_cases SET '+','.join(k+'=?' for k in values)+' WHERE message_id=?',(*values.values(),mid))
    def status(self,message_id):
        with self.co.store.connect() as c:
            row=c.execute('SELECT message_id,state,reason,planned,recovery_checks FROM gtm_reply_cases WHERE message_id=?',(message_id,)).fetchone()
            if row:return dict(row)
            table=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gtm_reply_observer_reviews'").fetchone()
            if table:
                scope=json.dumps({'account':CODE,'category':'acribic','cutoff':self.envelopes.cutoff.isoformat()},sort_keys=True)
                digest=hashlib.sha256(scope.encode()).hexdigest()
                review=c.execute('SELECT reason FROM gtm_reply_observer_reviews WHERE scope=? AND message_id=?',(digest,message_id)).fetchone()
                if review:return {'message_id':message_id,'state':'review_required','reason':review['reason'],'planned':0,'recovery_checks':0}
        return dict(row) if row else {'state':'not_observed'}
    def _preplan_rows(self,c,job_id,*,preview=False):
        """Private local eligibility, not a new source or destination readback."""
        case=c.execute('SELECT * FROM gtm_reply_cases WHERE job_id=?',(job_id,)).fetchone()
        job=c.execute('SELECT * FROM lifecycle_jobs WHERE job_id=?',(job_id,)).fetchone()
        if not case or not job:raise SourceBlocked('intake_reconcile_scope')
        case,job=dict(case),dict(job)
        pairs={('skipped','newsletter_or_autoreply_ignored')}
        if isinstance(case['reason'],str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',case['reason']):
            pairs.add(('review_required',case['reason']))
        if preview:pairs={('previewed','read_only_plan')}
        if ((case['state'],case['reason']) not in pairs or case['planned']!=0 or (bool(case['payload'])!=preview)
            or job['job_kind']!='gtm_reply_intake' or job['intent_id'] is not None
            or job['checkpoint'] or job['verification_evidence']):raise SourceBlocked('intake_reconcile_scope')
        envelope=c.execute('SELECT metadata FROM gtm_reply_envelopes WHERE idempotency_key=?',(job['idempotency_key'],)).fetchone()
        if not envelope:raise SourceBlocked('intake_reconcile_scope')
        metadata=json.loads(envelope['metadata'])
        if (set(metadata)!={'account','id','threadId','from','internalDate'} or metadata['account']!='code'
            or metadata['id']!=case['message_id']
            or not isinstance(metadata['threadId'],str) or not 1<=len(metadata['threadId'])<=256
            or not isinstance(metadata['from'],str) or '@' not in metadata['from']
            or not isinstance(metadata['internalDate'],str)
            or not metadata['internalDate'].isdigit() or self.envelopes.declared!=frozenset({'acribic'})
            or datetime.fromtimestamp(int(metadata['internalDate'])/1000,timezone.utc)<self.envelopes.cutoff):
            raise SourceBlocked('intake_reconcile_scope')
        key='gtm_reply_intake:'+hashlib.sha256(('code|'+metadata['id']).encode()).hexdigest()
        if job['idempotency_key']!=key or job_id!='job_'+hashlib.sha256(key.encode()).hexdigest()[:32]:
            raise SourceBlocked('intake_reconcile_scope')
        identity=Identity('acribic',CODE,'reply',metadata['id'],1,'0'*64).checked()
        if not preview and (c.execute("SELECT 1 FROM gtm_sources WHERE json_extract(identity,'$.source_id')=? AND json_extract(identity,'$.account')=? AND json_extract(identity,'$.kind')='reply'",(metadata['id'],CODE)).fetchone()
            or c.execute('SELECT 1 FROM gtm_attempts WHERE phase_key LIKE ?',('gtm:'+identity.key+':%',)).fetchone()
            or c.execute('SELECT 1 FROM lifecycle_jobs WHERE idempotency_key LIKE ?',('gtm:'+identity.key+':%',)).fetchone()):
            raise SourceBlocked('intake_reconcile_downstream')
        return case,job,metadata
    def reconcile_unclaimed(self,job_id):
        """Settle one persisted preclaim rejection via native start/fail only.

        Standalone repair is limited to the persisted terminal case pairs
        excluded by run_one's selection. The operator must also exclude manual
        case edits/resume (the current service's lifetime flock blocks its CLI).
        No global lock or atomic Case/Job transaction is claimed: native CAS and
        exact before/after reads fail closed. Any crash/race after our own start
        remains leased/gated, never stolen or auto-completed.
        """
        with self.co.store.connect() as c:case,job,metadata=self._preplan_rows(c,job_id)
        if (job['state']=='failed' and job['error_type']==case['reason'] and job['lease_until']==0
            and job['attempts']==1 and job['lease_generation']==1 and job['owner'].startswith('reply_preclaim_')):
            return {'state':'failed','reused':True,'saas_writes':0}
        if any(case[k] for k in ('source_hash','binding','business_proof')):
            raise SourceBlocked('intake_reconcile_source_bound')
        if (job['state']!='queued' or job['attempts']!=0 or job['lease_generation']!=0
            or job['owner'] or job['lease_until']!=0 or job['error_type'] or job['max_attempts']!=3):
            raise SourceBlocked('intake_reconcile_native_state')
        owner='reply_preclaim_'+uuid.uuid4().hex
        with self.co.store.connect() as c:
            if self._preplan_rows(c,job_id)!=(case,job,metadata):raise SourceBlocked('intake_reconcile_race')
        claimed=self.co.store.start_job(job_id,owner=owner,lease_seconds=300)
        with self.co.store.connect() as c:
            fresh,current,current_metadata=self._preplan_rows(c,job_id)
        if (fresh!=case or current_metadata!=metadata or current['state']!='running' or current['owner']!=owner
            or current['lease_generation']!=claimed['lease_generation'] or current['attempts']!=1):
            raise SourceBlocked('intake_reconcile_race')
        self.co.store.fail_job(job_id,owner=owner,lease_generation=claimed['lease_generation'],error_type=case['reason'])
        with self.co.store.connect() as c:
            fresh,finished,current_metadata=self._preplan_rows(c,job_id)
        if (fresh!=case or current_metadata!=metadata or finished['state']!='failed' or finished['lease_until']!=0
            or finished['error_type']!=case['reason'] or finished['owner']!=owner
            or finished['lease_generation']!=claimed['lease_generation']):raise SourceBlocked('intake_reconcile_race')
        return {'state':'failed','reused':False,'saas_writes':0}
    def _preview_rows(self,c,job_id,owned=None):
        case,intake,metadata=self._preplan_rows(c,job_id,preview=True)
        saved=json.loads(case['payload']);p=self._restore(case['payload']);p.note_plan.checked()
        if (saved.get('snapshot_contract')!='ole-reply-run-snapshot-v1' or not saved.get('thread_sources')
            or p.identity.kind!='reply' or p.identity.category!='acribic' or p.identity.account!=CODE
            or p.identity.version!=1 or p.identity.source_id!=metadata['id']
            or p.source.message_id!=metadata['id'] or p.source.thread_id!=metadata['threadId']
            or p.source.sender.casefold()!=metadata['from'].casefold()
            or p.source.account_email!=CODE or p.source.complete is not True
            or not isinstance(p.source.raw_b64,str) or not p.source.raw_b64
            or p.company_snapshot.get('id')!=p.binding.company_id
            or sha(p.source)!=p.identity.source_hash or case['source_hash']!=p.identity.source_hash
            or p.note_plan.source_text!=p.source.source_text() or p.note_plan.company_id!=p.binding.company_id
            or case['binding']!=json.dumps(asdict(p.binding),sort_keys=True)):
            raise SourceBlocked('preview_scope_changed')
        threads={mid:(value,stamp)for mid,value,stamp in saved['thread_sources']}
        proof=json.loads(case['business_proof'])
        if (len(threads)!=len(saved['thread_sources']) or threads.get(metadata['id'],(None,))[0]!=case['source_hash']
            or not isinstance(proof,list) or not proof or len({mid for mid,value in proof})!=len(proof)
            or any(threads.get(mid,(None,))[0]!=value for mid,value in proof)):
            raise SourceBlocked('preview_source_proof_missing')
        source=c.execute('SELECT identity FROM gtm_sources WHERE workflow_id=?',(p.identity.key,)).fetchone()
        sources=c.execute("SELECT identity FROM gtm_sources WHERE json_extract(identity,'$.source_id')=? AND json_extract(identity,'$.account')=? AND json_extract(identity,'$.kind')='reply'",(metadata['id'],CODE)).fetchall()
        phases={r['idempotency_key']:dict(r)for r in c.execute('SELECT * FROM lifecycle_jobs WHERE idempotency_key LIKE ?',('gtm:'+p.identity.key+':%',))}
        if c.execute('SELECT 1 FROM gtm_attempts WHERE phase_key LIKE ?',('gtm:'+p.identity.key+':%',)).fetchone():
            raise SourceBlocked('preview_attempted_phase')
        wanted={self.co._phase_key(p.identity,phase):phase for phase in PLANS['reply']}
        if source:
            if len(sources)!=1 or json.loads(source['identity'])!=p.identity.fields() or set(phases)!=set(wanted):
                raise SourceBlocked('preview_native_plan_changed')
        elif sources or phases:raise SourceBlocked('preview_native_plan_changed')
        jobs={job_id:intake}
        for key,row in phases.items():
            phase=wanted[key]
            if (row['job_id']!=self.co._job_id(p.identity,phase) or row['job_kind']!='gtm_'+phase
                or row['intent_id'] is not None):raise SourceBlocked('preview_native_plan_changed')
            jobs[row['job_id']]=row
        for jid,row in jobs.items():
            empty=(row['state']=='queued' and row['attempts']==row['lease_generation']==row['lease_until']==0
                   and not row['owner'] and not row['error_type'])
            paused=(row['state']=='failed' and row['attempts']==row['lease_generation']==1 and row['lease_until']==0
                    and row['owner'].startswith('reply_preview_') and row['error_type']=='readonly_preview_paused')
            own=(owned and jid==owned[0] and row['state']=='running' and row['owner']==owned[1]
                 and row['lease_generation']==owned[2] and row['attempts']==1)
            if row['max_attempts']!=3 or row['checkpoint'] or row['verification_evidence'] or not (empty or paused or own):
                raise SourceBlocked('preview_native_job_not_unattempted')
        return case,metadata,jobs
    def reconcile_preview(self,job_id):
        """Park one full readonly preview and its exact legacy unattempted plan.

        Source/plan data is preserved; no original/provider reads or execution.
        The active preview-only observer never selects persisted previewed cases.
        Manual activation/resume must be excluded by the reviewed operator.
        """
        if self.crm_enabled:raise SourceBlocked('preview_reconcile_requires_readonly')
        with self.co.store.connect() as c:case,metadata,initial=self._preview_rows(c,job_id)
        changed=0
        for jid in sorted(initial):
            with self.co.store.connect() as c:fresh,meta,jobs=self._preview_rows(c,job_id)
            if fresh!=case or meta!=metadata or set(jobs)!=set(initial):raise SourceBlocked('preview_reconcile_race')
            if jobs[jid]['state']=='failed':continue
            owner='reply_preview_'+uuid.uuid4().hex
            claim=self.co.store.start_job(jid,owner=owner,lease_seconds=300)
            with self.co.store.connect() as c:
                fresh,meta,_=self._preview_rows(c,job_id,owned=(jid,owner,claim['lease_generation']))
            if fresh!=case or meta!=metadata:raise SourceBlocked('preview_reconcile_race')
            self.co.store.fail_job(jid,owner=owner,lease_generation=claim['lease_generation'],error_type='readonly_preview_paused')
            changed+=1
        with self.co.store.connect() as c:fresh,meta,jobs=self._preview_rows(c,job_id)
        if fresh!=case or meta!=metadata or any(r['state']!='failed' for r in jobs.values()):
            raise SourceBlocked('preview_reconcile_race')
        return {'state':'failed','paused_native_jobs':len(jobs),'reused':changed==0,'saas_writes':0}
    def _resume_preplan(self,row):
        with self.co.store.connect() as c:case,job,metadata=self._preplan_rows(c,row['job_id'])
        if (case!=dict(row) or case['state']!='review_required' or case['reason']!='crm_mapping_review'
            or job['state']!='failed' or job['lease_until']!=0 or job['error_type']!=case['reason']
            or not job['owner'] or job['lease_generation']<1 or job['attempts']<1
            or job['attempts']>=job['max_attempts']):raise SourceBlocked('case_not_resumable')
        # Failed fresh mapping checks mutate neither case nor retry budget.
        fresh=self._snapshot(metadata)
        checks={'source_hash':sha(fresh.source),'binding':json.dumps(asdict(fresh.binding),sort_keys=True),
                'business_proof':json.dumps(fresh.proof['sent_sources'],sort_keys=True)}
        if any(case[k] and case[k]!=value for k,value in checks.items()):raise SourceBlocked('case_scope_changed')
        with self.co.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if self._preplan_rows(c,row['job_id'])!=(case,job,metadata):raise SourceBlocked('case_resume_race')
            c.execute("UPDATE gtm_reply_cases SET state='recovering',reason=NULL,next_check=?,recovery_checks=0,source_hash=?,binding=?,business_proof=? WHERE job_id=?",
                      (self.clock(),checks['source_hash'],checks['binding'],checks['business_proof'],row['job_id']))
        return {'state':'recovering','source_hash':checks['source_hash'],'writes_replayed':False}
    def resume(self,message_id):
        """Explicit local operator recovery, retaining original scope and markers."""
        with self.co.store.connect() as c:
            row=c.execute('SELECT * FROM gtm_reply_cases WHERE message_id=?',(message_id,)).fetchone()
            native=c.execute('SELECT * FROM lifecycle_jobs WHERE job_id=?',(row['job_id'],)).fetchone() if row else None
        if row and self.invitation_enabled and not self.crm_enabled and self.invitation_cutoff is not None:
            metadata=self.envelopes.metadata_for_job(row['job_id'])
            if int(metadata['internalDate'])<int(self.invitation_cutoff.timestamp()*1000):raise SourceBlocked('invitation_before_activation')
        if not row or row['state']!='review_required':raise SourceBlocked('case_not_resumable')
        state=native['state']
        if state in ('completed','cancelled'):raise SourceBlocked('case_terminal_review')
        if not row['payload']:return self._resume_preplan(row)
        if state not in ('failed','stalled'):raise SourceBlocked('case_not_resumable')
        metadata=self.envelopes.metadata_for_job(row['job_id'])
        prepared=self._restore(row['payload']);fresh=self._fresh(prepared,metadata,row['business_proof'])
        saved=json.loads(row['payload'])
        if saved.get('snapshot_contract')!='ole-reply-run-snapshot-v1' or not saved.get('thread_sources'):raise SourceBlocked('legacy_snapshot_review')
        current={s.message_id:(sha(s),stamp) for s,stamp in fresh.sources}
        if any(current.get(mid)!=(value,stamp) for mid,value,stamp in saved['thread_sources']):raise SourceBlocked('thread_source_changed')
        if fresh.company.get('notionQuellen')!=prepared.company_snapshot.get('notionQuellen'):raise SourceBlocked('crm_source_snapshot_changed')
        if self.receipts.get(fresh.source,fresh.binding,thread_sources=fresh.sources)!=saved['semantic']:raise SourceBlocked('semantic_receipt_changed')
        with self.co.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT * FROM gtm_reply_cases WHERE message_id=?',(message_id,)).fetchone()
            job=c.execute('SELECT * FROM lifecycle_jobs WHERE job_id=?',(row['job_id'],)).fetchone()
            if current is None or job is None or dict(current)!=dict(row) or dict(job)!=dict(native):raise SourceBlocked('case_resume_race')
            c.execute("UPDATE gtm_reply_cases SET state='recovering',reason=NULL,next_check=?,recovery_checks=0 WHERE message_id=?",(self.clock(),message_id))
        return {'state':'recovering','source_hash':row['source_hash'],'writes_replayed':False}
    @staticmethod
    def _serialize(p,receipt,*,thread_sources=(),invitation_only=False):
        return json.dumps({'identity':p.identity.fields(),'note':asdict(p.note_plan),'company':p.company_snapshot,
            'binding':asdict(p.binding),'stage':p.stage,'source':asdict(p.source),'semantic':receipt,
            'snapshot_contract':'ole-reply-run-snapshot-v1','thread_sources':thread_sources,
            'execution':'invitation_only' if invitation_only else 'crm'},sort_keys=True,separators=(',',':'))
    @staticmethod
    def _restore(payload):
        d=json.loads(payload);identity=Identity(**d['identity']);n=d['note'];n['identity']=identity
        n['statements']=tuple(Statement(**s) for s in n['statements'])
        s=d['source'];s['recipients']=tuple(s['recipients']);s['labels']=tuple(s['labels'])
        return PreparedReply(identity,NotePlan(**n),d['company'],ExistingLeadBinding(**d['binding']),d['stage'],OriginalMail(**s))
    def _snapshot(self,row):
        self.originals.profile(self.gmail)
        sources=tuple(self.originals.thread(self.gmail,row['threadId'],profile_checked=True))
        matches=[pair for pair in sources if pair[0].message_id==row['id']]
        if len(matches)!=1:raise SourceBlocked('message_identity_mismatch')
        source,occurred=self.originals.capture_full_message(self.gmail,row,'acribic',profile_checked=True,source_pair=matches[0])
        mapping_reads=PhaseReads(self.twenty)
        try:
            mapping=(self.twenty.lookup_existing_sender(source.sender,record_reader=mapping_reads)
                     if isinstance(self.twenty,ExistingReplyReadClient) else self.twenty.lookup_existing_sender(source.sender))
        except ValueError:raise SourceBlocked('crm_mapping_review') from None
        except RuntimeError:raise SourceBlocked('crm_read_unavailable') from None
        company=verify_existing_mapping(mapping_reads,mapping,source.sender)
        proof=self.originals.verify_outreach_thread(self.gmail,row,'acribic',sources=sources)
        return RunSnapshot(source,occurred,mapping,copy.deepcopy(company),sources,proof)
    def _fresh(self,p,row,business):
        snapshot=self._snapshot(row);source=snapshot.source;mapping=snapshot.binding
        if sha(source)!=p.identity.source_hash:raise SourceBlocked('incoming_source_changed')
        if mapping!=p.binding:raise SourceBlocked('crm_binding_changed')
        proof=snapshot.proof
        if json.dumps(proof['sent_sources'],sort_keys=True)!=business:raise SourceBlocked('outreach_source_changed')
        return snapshot
    def _invitation(self,p,row,receipt):
        if not receipt or receipt['acceptance']['decision']!='confirmed':return None
        data=receipt['binding'];proposal_id=data['proposal_id']
        actual_reader=self.originals.acceptance_reader(self.gmail,thread_id=row['threadId'],proposal_id=proposal_id,incoming_id=row['id'],include_sources=True)
        def reader():
            proposal,incoming,sources=actual_reader()
            if data.get('offer_context_hash') is not None:
                ctx=thread_offer_context(proposal,sources)
                offers=offered_slots(proposal,source_time=ctx.source_time,duration_sources=ctx.duration_sources)
                if ctx.fingerprint!=data['offer_context_hash'] or offers_hash(offers)!=data.get('offers_hash'):
                    raise SourceBlocked('offer_context_changed')
            return proposal,incoming
        proposal,incoming,sources=actual_reader()
        ctx=thread_offer_context(proposal,sources) if data.get('offer_context_hash') is not None else None
        proof=AcceptanceProof(CODE,incoming.message_id,sha(incoming),data['source_version'],proposal_id,data['proposal_hash'],
            data['proposal_version'],row['threadId'],incoming.sender,receipt['acceptance']['quote'],receipt['acceptance']['selected_offer_id'],
            data.get('offer_context_hash'),data.get('offers_hash'))
        resolver=SemanticAcceptanceResolver(proof,ctx)
        offers=offered_slots(proposal,source_time=ctx.source_time,duration_sources=ctx.duration_sources) if ctx else offered_slots(proposal)
        chosen=[o for o in offers if o['offer_id']==proof.selected_offer_id]
        if len(chosen)!=1:raise SourceBlocked('selected_offer_review')
        selected=chosen[0];start=datetime.fromisoformat(selected['start'])
        binding=Binding('code',row['threadId'],incoming.sender,start,selected['duration_minutes'],proposal_id)
        accepted=resolver(binding,proposal,incoming)
        hold_id=InvitationAdapter.owned_hold_id(self.calendar,binding)
        adapter=InvitationAdapter(self.calendar,self.policy,p.identity,accepted,hold_id,source_reader=reader,
            acceptance_resolver=resolver,clock=lambda:datetime.fromtimestamp(self.clock(),timezone.utc))
        if self.cleanup is None:raise SourceBlocked('frozen_cleanup_not_bound')
        return adapter
    def _settle_calendar_request(self,source_id,trigger_id,result):
        """Resume only this canonical calendar case; retain unrelated reviews."""
        with self.co.store.connect()as c:
            prior=c.execute('SELECT * FROM gtm_reply_cases WHERE message_id=?',(source_id,)).fetchone()
            if not prior:return
            prior=dict(prior)
            if prior['planned']or prior['source_hash']not in (None,result['request_source_hash']):return
            if prior['state']=='waiting_calendar_acceptance':job=None
            elif prior['state']=='review_required'and prior['reason']in ('external_calendar_event_not_imported','external_calendar_acceptance_not_imported'):
                checked,job,metadata=self._preplan_rows(c,prior['job_id'])
                if checked!=prior or metadata['id']!=source_id or metadata['from']==CODE:return
            else:return
        if job is not None:
            if job['state']!='failed'or job['error_type']!=prior['reason']or job['attempts']>=job['max_attempts']:return
            self.co.store.retry_job(job['job_id'],owner=job['owner'],lease_generation=job['lease_generation'])
            owner='reply_calendar_resume_'+uuid.uuid4().hex
            claim=self.co.store.start_job(job['job_id'],owner=owner,lease_seconds=300);claim['owner']=owner
            self._finish(claim,json.dumps({'contract':'external-calendar-request-resumed-v1','source_id':source_id,
                'trigger_id':trigger_id,'source_hash':result['request_source_hash'],'cleanup_source_hash':result['source_hash']},sort_keys=True))
        with self.co.store.connect()as c:
            c.execute("UPDATE gtm_reply_cases SET state='completed',reason='confirmed_calendar_cleanup_verified',source_hash=? WHERE message_id=? AND state=? AND reason=? AND planned=0",
                (result['request_source_hash'],source_id,prior['state'],prior['reason']))
    def _confirmed_calendar_message(self,row,case,existing):
        """Settle a real invitation through this existing durable intake only."""
        try:result=self.confirmed_cleanup.on_message(row)
        except SourceBlocked as error:
            # Gmail may precede calendar import. Reuse the case's existing
            # recovery counter/backoff, never a second calendar sweep queue.
            if error.code in ('external_calendar_event_not_imported','external_calendar_acceptance_not_imported') and case['recovery_checks']<3:
                self._update(row['id'],state='recovering',next_check=self.clock()+30,
                    recovery_checks=case['recovery_checks']+1,reason='waiting_calendar_import')
                return {'state':'waiting_calendar_import','model_calls':0,'saas_writes':0}
            raise
        if result is None:return None
        if result['state']not in ('completed','waiting_calendar_acceptance','declined_preserved'):raise SourceBlocked('external_calendar_cleanup_readback_required')
        jid=case['job_id'];job=self.co.store.get_job(jid)
        if job['state']=='completed':
            self._update(row['id'],state=result['state'],reason='confirmed_calendar_cleanup_verified'if result['state']=='completed'else result['state'])
            return {**result,'model_calls':0,'reused_native_completion':True}
        if job['state']not in ('queued','failed','stalled'):return {'state':'waiting_owner','model_calls':0}
        if job['state']!='queued':self.co.store.retry_job(jid,owner=job['owner'],lease_generation=job['lease_generation'])
        owner='reply_calendar_'+uuid.uuid4().hex
        claimed=self.co.store.start_job(jid,owner=owner,lease_seconds=300);claimed['owner']=owner
        self._finish(claimed,json.dumps({'contract':'external-calendar-cleanup-v1','source_id':row['id'],
            'source_hash':result['source_hash'],'eligible_holds':result['eligible_holds'],'state':result['state']},sort_keys=True))
        changes=dict(state=result['state'],source_hash=result['source_hash'],reason='confirmed_calendar_cleanup_verified'if result['state']=='completed'else result['state'])
        if result['state']=='waiting_calendar_acceptance':
            changes['payload']=json.dumps({'execution':'external_calendar_waiting','calendar_uid':result['calendar_uid'],
                'event_id':result['event_id'],'request_message_id':row['id']},sort_keys=True)
        self._update(row['id'],**changes)
        original=result.get('source_message_id')
        if result['state']=='completed'and original and original!=row['id']:
            self._settle_calendar_request(original,row['id'],result)
        return {**result,'model_calls':0}
    def run_one(self):
        if not self.crm_enabled and not self.preview_enabled and not self.invitation_enabled and self.confirmed_cleanup is None:return {'state':'disabled','saas_writes':0,'model_calls':0}
        self.co.store.detect_stale_jobs()
        now=self.clock()
        with self.co.store.connect() as c:
            jobs=c.execute("SELECT j.job_id,e.metadata,j.created_at FROM lifecycle_jobs j JOIN gtm_reply_envelopes e ON j.idempotency_key=e.idempotency_key WHERE j.job_kind='gtm_reply_intake'").fetchall()
            for job in jobs:
                metadata=json.loads(job['metadata'])
                c.execute("INSERT OR IGNORE INTO gtm_reply_cases(message_id,job_id,state,created,next_check) VALUES(?,?,'pending',?,?)",
                          (metadata['id'],job['job_id'],now,now))
            states="'pending','waiting_semantics','recovering'"+( ",'previewed'" if self.crm_enabled else '')
            frontier="";params=[now]
            if self.invitation_enabled and not self.crm_enabled and self.invitation_cutoff is not None:
                frontier=" AND message_id IN (SELECT json_extract(metadata,'$.id') FROM gtm_reply_envelopes WHERE CAST(json_extract(metadata,'$.internalDate') AS INTEGER)>=?)"
                params.append(int(self.invitation_cutoff.timestamp()*1000))
            case=c.execute("SELECT * FROM gtm_reply_cases WHERE state IN ("+states+") AND next_check<=?"+frontier+" ORDER BY created,message_id LIMIT 1",params).fetchone()
        if not case:return {'state':'idle','model_calls':0}
        case=dict(case);mid=case['message_id'];jid=case['job_id'];row=self.envelopes.metadata_for_job(jid)
        existing=self.co.store.get_job(jid)
        if existing['state']=='cancelled':
            self._update(mid,state='cancelled',reason='intake_cancelled')
            return {'state':'cancelled','model_calls':0}
        if existing['state']=='completed' and case['planned']:
            prepared=self._restore(case['payload'])
            self._update(mid,state='review_required' if prepared.stage is None else 'completed',reason='classification_review' if prepared.stage is None else None)
            return {'state':'review_required' if prepared.stage is None else 'completed','reused_native_completion':True,'model_calls':0}
        claimed=None
        try:
            if self.confirmed_cleanup is not None:
                reconciled=self._confirmed_calendar_message(row,case,existing)
                if reconciled is not None:return reconciled
            snapshot=self._snapshot(row);source=snapshot.source;binding=snapshot.binding
            business=json.dumps(snapshot.proof['sent_sources'],sort_keys=True)
            thread_versions=tuple((s.message_id,sha(s),stamp) for s,stamp in snapshot.sources)
            if case['payload']:
                saved=json.loads(case['payload'])
                if saved.get('snapshot_contract')!='ole-reply-run-snapshot-v1' or not saved.get('thread_sources'):
                    raise SourceBlocked('legacy_snapshot_review')
                current={mid:(value,stamp) for mid,value,stamp in thread_versions}
                if any(current.get(mid)!=(value,stamp) for mid,value,stamp in saved['thread_sources']):raise SourceBlocked('thread_source_changed')
            if case['source_hash'] and (case['source_hash']!=sha(source) or case['binding']!=json.dumps(asdict(binding),sort_keys=True) or case['business_proof']!=business):
                raise SourceBlocked('case_scope_changed')
            if not case['source_hash']:
                self._update(mid,source_hash=sha(source),binding=json.dumps(asdict(binding),sort_keys=True),business_proof=business)
            receipt=self.receipts.get(source,binding,thread_sources=snapshot.sources)
            if not case['planned'] and not receipt and self.invitation_enabled and now<case['created']+self.wait:
                self._update(mid,state='waiting_semantics',next_check=case['created']+self.wait)
                return {'state':'waiting_semantics','model_calls':0}
            invitation_only=(json.loads(case['payload']).get('execution')=='invitation_only' if case['planned'] else not self.crm_enabled)
            if case['planned'] and not self.crm_enabled and not invitation_only:
                raise SourceBlocked('crm_disabled_existing_plan')
            if not self.crm_enabled and not case['planned'] and not (self.invitation_enabled and receipt and receipt['acceptance']['decision']=='confirmed'):
                p=prepare_new_reply(self.co,self.originals,self.gmail,self.twenty,row,binding,category='acribic',
                    classification=lambda s:self.receipts.classifier(s,binding),captured=(source,snapshot.occurred),company=snapshot.company,enqueue=False)
                self._update(mid,payload=self._serialize(p,receipt,thread_sources=thread_versions),state='previewed',reason='read_only_plan',planned=0)
                self.reconcile_preview(jid)
                return {'state':'previewed','model_calls':0,'saas_writes':0}
            if not case['planned'] and not receipt and now<case['created']+self.wait:
                self._update(mid,state='waiting_semantics',next_check=case['created']+self.wait)
                return {'state':'waiting_semantics','model_calls':0}
            if case['planned']:
                planned=self._restore(case['payload'])
                with self.co.store.connect() as c:
                    live=c.execute("SELECT MAX(lease_until) AS until_at FROM lifecycle_jobs WHERE idempotency_key LIKE ? AND state IN ('running','checkpointed','verifying') AND lease_until>?",('gtm:'+planned.identity.key+':%',now)).fetchone()['until_at']
                if live:
                    self._update(mid,next_check=live+1)
                    return {'state':'deferred_live_phase','model_calls':0,'writes_performed':False}
            with self.co.store.connect() as c:job=dict(c.execute('SELECT * FROM lifecycle_jobs WHERE job_id=?',(jid,)).fetchone())
            if job['state'] not in ('queued','failed','stalled'):return {'state':'waiting_owner'}
            if job['state']!='queued':self.co.store.retry_job(jid,owner=job['owner'],lease_generation=job['lease_generation'])
            owner='reply_'+uuid.uuid4().hex
            claimed=self.co.store.start_job(jid,owner=owner,lease_seconds=300)
            claimed['owner']=owner # native public snapshots deliberately omit it
            if case['planned']:
                p=self._restore(case['payload']);receipt=json.loads(case['payload'])['semantic']
                current=self.receipts.get(source,binding,thread_sources=snapshot.sources)
                if current!=receipt:raise SourceBlocked('semantic_receipt_changed')
            else:
                p=prepare_new_reply(self.co,self.originals,self.gmail,self.twenty,row,binding,category='acribic',
                    classification=lambda s:self.receipts.classifier(s,binding),captured=(source,snapshot.occurred),company=snapshot.company,enqueue=not invitation_only)
                if invitation_only:
                    self._invitation(p,row,receipt) # exact original acceptance before durable write plan
                    self.co.plan(p.identity,reply_phases=('source','classification','invitation'))
                self._update(mid,payload=self._serialize(p,receipt,thread_sources=thread_versions,invitation_only=invitation_only),planned=1)
            if sha(source)!=p.identity.source_hash:raise SourceBlocked('incoming_source_changed')
            read=lambda:source # this run's complete scoped snapshot; no new model
            reads=PhaseReads(self.twenty)
            adapters=p.adapters(reads)
            adapters['source']=ReplySourceAdapter(p,read)
            class Classification:
                def readback(_,identity):
                    result=adapters['source'].readback(identity)
                    if result.state!='verified':return result
                    if self.receipts.get(p.source,p.binding,thread_sources=snapshot.sources)!=receipt:return Readback('conflict',identity)
                    return Readback('verified',identity,mid+':bound-classification-or-review')
                def preflight(_,identity):raise ValueError('Read-only classification')
                def execute_once(_,identity):raise ValueError('Read-only classification')
            adapters['classification']=Classification()
            adapters['twenty_status'].stage_guard=lambda:snapshot.proof['status_current']
            phases=['source','classification'] if invitation_only else ['source','classification','twenty_note','twenty_link','twenty_status']
            for phase in phases:
                reads.clear()
                def before_write(phase=phase):
                    # Exact destination absence/prerequisites are fresh first;
                    # source/account/mapping checks follow immediately before
                    # the physical call. Native CAS still uses its exact revision.
                    adapters[phase].preflight(p.identity)
                    fresh=self._fresh(p,row,business)
                    if self.receipts.get(fresh.source,fresh.binding,thread_sources=fresh.sources)!=receipt:raise SourceBlocked('semantic_receipt_changed')
                    # Dirty source/mapping snapshots cannot authorize a write.
                    if fresh.company.get('notionQuellen')!=p.company_snapshot.get('notionQuellen'):
                        raise SourceBlocked('crm_source_snapshot_changed')
                    known={s.message_id:(sha(s),stamp) for s,stamp in snapshot.sources}
                    current={s.message_id:(sha(s),stamp) for s,stamp in fresh.sources}
                    if any(current.get(mid)!=value for mid,value in known.items()):raise SourceBlocked('thread_source_changed')
                    if receipt and receipt['binding']['proposal_id'] and (current.get(receipt['binding']['proposal_id']) or (None,None))[0]!=receipt['binding']['proposal_hash']:
                        raise SourceBlocked('proposal_source_changed')
                    if phase=='twenty_status':
                        if self.version_reader()!='v2.42.6':raise SourceBlocked('twenty_version_changed')
                        if snapshot.proof['status_current'] and not fresh.proof['status_current']:raise SourceBlocked('status_superseded')
                reads.before_write=before_write
                if phase=='twenty_status' and p.binding.source_page_id is None:raise SourceBlocked('crm_status_mapping_review')
                self.co.store.heartbeat(jid,owner=claimed['owner'],lease_generation=claimed['lease_generation'],lease_seconds=300)
                result=self.co.run_phase(p.identity,phase,adapters[phase])
                if reads.gate_error:raise reads.gate_error
                if result['state']=='verified_result_recovery_required':result=self.co.recover_verified(p.identity,phase,adapters[phase])
                if result['state']!='completed':
                    self._fresh(p,row,business) # concrete source drift is terminal
                    raise SourceBlocked('destination_'+phase+'_readback_required')
            try:invite=self._invitation(p,row,receipt) if self.invitation_enabled else None
            except ValueError:raise SourceBlocked('invitation_acceptance_review') from None
            if invite:
                for phase,adapter in [('invitation',invite)]:
                    self._fresh(p,row,business)
                    self.co.store.heartbeat(jid,owner=claimed['owner'],lease_generation=claimed['lease_generation'],lease_seconds=300)
                    result=self.co.run_phase(p.identity,phase,adapter)
                    if result['state']=='verified_result_recovery_required':result=self.co.recover_verified(p.identity,phase,adapter)
                    if result['state']!='completed':raise SourceBlocked('destination_'+phase+'_readback_required')
                if self.cleanup.run(invite,p.identity)['state']!='completed':raise SourceBlocked('destination_hold_release_readback_required')
            self._finish(claimed,'reply_invitation_and_exact_cleanup_verified' if invitation_only else 'reply_bound_crm_and_optional_invitation_readbacks_verified')
            self._update(mid,state='review_required' if p.stage is None else 'completed',reason='classification_review' if p.stage is None else None)
            return {'state':'review_required' if p.stage is None else 'completed','reason':'classification_review' if p.stage is None else None,'model_calls':0}
        except Exception as error:
            reason=error.code if isinstance(error,SourceBlocked) else 'reply_case_review'
            status=getattr(getattr(error,'resp',None),'status',None)
            if isinstance(error,(TimeoutError,ConnectionError)) or status==429 or (isinstance(status,int) and 500<=status<=599):reason='source_temporarily_unavailable'
            skipped=reason=='newsletter_or_autoreply_ignored'
            recoverable=(reason.startswith('destination_') or reason in ('source_temporarily_unavailable','crm_read_unavailable')) and case['recovery_checks']<2
            self._update(mid,state='skipped' if skipped else ('recovering' if recoverable else 'review_required'),reason=reason,
                next_check=now+60*(2**case['recovery_checks']),recovery_checks=case['recovery_checks']+1)
            if claimed:
                try:self.co.store.fail_job(jid,owner=claimed['owner'],lease_generation=claimed['lease_generation'],error_type=reason)
                except ValueError:pass
            elif not recoverable and not case['planned'] and not case['source_hash'] and not case['payload'] and isinstance(reason,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',reason):
                with self.co.store.connect() as c:
                    scoped=c.execute('SELECT source_hash,binding,business_proof,payload,planned FROM gtm_reply_cases WHERE message_id=?',(mid,)).fetchone()
                if not any(scoped):self.reconcile_unclaimed(jid)
            return {'state':'skipped' if skipped else ('recovering' if recoverable else 'review_required'),'reason':reason,'model_calls':0,'write_replayed':False}
    def _finish(self,job,proof):
        self.co.store.begin_verification(job['job_id'],owner=job['owner'],lease_generation=job['lease_generation'])
        self.co.store.complete_job(job['job_id'],owner=job['owner'],lease_generation=job['lease_generation'],verification_evidence=proof)
