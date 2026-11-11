"""Immediate durable acceptance/control; work belongs to the detached supervisor."""
import json
import os
from pathlib import Path
import subprocess
import sys
from .contracts import (ContractError, Item, approved_path, checked_policy, digest, token, PROFILES)
from .store import Store

class BackgroundService:
    def __init__(self, root, policy, *, fixture=False):
        # Bound control reads/cancel survive runtime drift; admitting/restarting work
        # still requires current hashes, independently of this cached service.
        self.policy = checked_policy(dict(policy), fixture=fixture, verify_runtime=False)
        self.fixture = fixture
        self.store = Store(root)
    def submit(self, *, request_id, goal, manifest_ref, profile, owner, session_id, supersedes=None, launch=True):
        if not self.fixture:checked_policy(self.policy,fixture=False)
        token(request_id); token(session_id, 256)
        if owner != self.policy['owner']: raise ContractError('owner_mismatch')
        if not isinstance(goal,str) or not goal.strip() or len(goal.encode()) > 80000: raise ContractError('bounded_explicit_goal_required')
        if profile not in self.policy['profiles']: raise ContractError('unapproved_profile')
        manifest_path = approved_path(self.policy['manifests_root'], manifest_ref)
        if manifest_path.stat().st_size > 4_000_000: raise ContractError('manifest_too_large')
        manifest = json.loads(manifest_path.read_bytes())
        if set(manifest) != {'contract','items'} or manifest['contract'] != 'background-sources-v1': raise ContractError('source_manifest_schema')
        raw = manifest['items']
        if not isinstance(raw,list) or not 1 <= len(raw) <= 500: raise ContractError('explicit_one_to_500_items_required')
        items = [Item(**v).checked(self.policy) for v in raw]
        if len({i.item_id for i in items}) != len(items): raise ContractError('duplicate_item_id')
        for i in items:
            p = approved_path(self.policy['sources_root'], i.source_ref)
            if p.stat().st_size > self.policy.get('max_source_bytes',16_000_000): raise ContractError('source_too_large_no_truncation')
        if supersedes:
            previous = self.store.row(supersedes,owner,session_id)
            if previous['state'] not in {'cancelled','completed','completed_with_blockers'}: raise ContractError('cancel_prior_scope_first')
        contract = {'request_id':request_id,'goal':goal,'owner':owner,'account':self.policy['account'],
                    'session_id':session_id,'profile':profile,'manifest_hash':digest(manifest),
                    'items':[i.fields() for i in items],'per_item_attempts':self.policy['per_item_attempts'],
                    'per_item_iterations':self.policy['per_item_iterations'],'supersedes':supersedes}
        if supersedes and profile=='readonly-research':
            prior=json.loads(previous['contract']);root=self.store.budget_root(supersedes)
            if root is None:raise ContractError('legacy_iteration_budget_unknown')
            allowed={v['item_id']:v for v in prior['items']}
            if any(allowed.get(v['item_id'])!=v for v in contract['items']):raise ContractError('revision_scope_expansion_unsupported')
            contract['budget_root']=root
        job_id = self.store.add(contract)
        if launch: self.ensure_supervisor()
        return self.status(job_id,owner,session_id)
    def accepted_proof(self,job_id,owner,session_id):
        row=self.store.row(job_id,owner,session_id);contract=json.loads(row['contract'])
        expected={'contract':'background-accepted-v1','job_id':job_id,'owner':contract['owner'],'session_id':contract['session_id'],'scope_hash':digest(contract),'total':len(contract['items'])}
        ref='accepted/'+job_id+'.'+digest(expected)+'.json'
        if row['submission_ref']!=ref:raise ContractError('submission_receipt_mismatch')
        path=approved_path(self.store.root,ref)
        if path.read_bytes()!=json.dumps(expected,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode():raise ContractError('submission_receipt_mismatch')
        return {'accepted':True,'job_id':job_id,'submission_ref':ref,'scope_hash':digest(contract),'owner':owner,'session_id':session_id,'total':len(contract['items']),'account':contract['account'],'profile':contract['profile'],'source_check_state':'deferred_to_item_phase','source_originals_verified':False}
    def foreground_started(self,owner,session_id,seconds=30):
        if owner!=self.policy['owner']:raise ContractError('owner_mismatch')
        token(session_id,256)
        from .slots import session_foreground
        return session_foreground(self.policy['slot_root'],owner,session_id,seconds=seconds)
    def foreground_finished(self,owner,session_id):
        if owner!=self.policy['owner']:raise ContractError('owner_mismatch')
        token(session_id,256)
        from .slots import session_foreground
        return session_foreground(self.policy['slot_root'],owner,session_id,finished=True)
    def status(self, job_id, owner, session_id):
        row=self.store.status(job_id,owner,session_id)
        with self.store.lifecycle.connect() as c:
            amendment=c.execute('SELECT state,next_job,blocker FROM background_revision_requests WHERE parent_job=?',(job_id,)).fetchone()
        if amendment:
            row.update(state={'requested':'revising','applied':'superseded','blocked':'revision_blocked'}[amendment['state']],
                       replacement_job=amendment['next_job'],revision_blocker=amendment['blocker'])
        if self.store.budget_root(job_id) is not None:
            family=self.family_page(job_id,owner,session_id,offset=0,length=500)
            row.update(family_total=family['total'],family_verified=sum(i['state']=='verified' for i in family['items']),
                       family_members=len({i['origin_job_id'] for i in family['items']}))
        return row
    def list_owned(self,owner,session_id):
        return [self.status(r['job_id'],owner,session_id) for r in self.store.recent_owned(owner,session_id)]
    def revise(self,job_id,owner,session_id,expected_revision,inbound_id,correction):
        if type(expected_revision) is not int or expected_revision<1:raise ContractError('expected_revision_required')
        token(inbound_id,256)
        if not isinstance(correction,str) or not correction.strip() or len(correction.encode())>20000:raise ContractError('bounded_correction_required')
        request=self.store.request_revision(job_id,owner,session_id,expected_revision,inbound_id,correction)
        if not self.fixture:self.ensure_supervisor()
        return {'state':request['state'],'correction_stored':True,'worker_applied':request['state']=='applied',
                'job_id':job_id,'replacement_job':request.get('next_job'),'revision_request':request['request_key'],
                'message':'Korrektur gespeichert. Der alte Worker wird zuerst sicher angehalten.'}
    def reconcile_revisions(self):
        """Called only by the existing singleton supervisor after reaping children."""
        with self.store.lifecycle.connect() as c:
            requests=[dict(r) for r in c.execute("SELECT * FROM background_revision_requests WHERE state='requested' LIMIT 16")]
        for request in requests:
            job_id=request['parent_job'];row=self.store.row(job_id)
            if row['state']!='cancelled':continue
            with self.store.lifecycle.connect() as c:
                if c.execute('SELECT 1 FROM background_worker_births WHERE worker_id LIKE ?',(job_id+':%',)).fetchone():continue
            try:
                contract=json.loads(row['contract']);remaining=[]
                from .adapters import LocalReadonly
                for item in self.store.items(job_id):
                    if item['state']=='verified':continue
                    identity=json.loads(item['identity'])
                    # Existing attempted local artifacts are inspected, never replayed in place.
                    with self.store.lifecycle.connect() as c:
                        phases=[dict(p) for p in c.execute('SELECT phase,native_job_id FROM background_phases WHERE job_id=? AND item_id=?',(job_id,item['item_id']))]
                    for phase in phases:
                        if self.store.attempted(phase['native_job_id']):
                            ident={'job_id':job_id,'item':identity,'scope_hash':digest(contract),'phase':phase['phase']}
                            proof=LocalReadonly(self.store,self.policy,ident,contract,lambda:False).readback(ident)
                            if proof.get('state') not in ('absent','verified'):raise ContractError('revision_readback_required')
                    remaining.append(identity)
                if not remaining:raise ContractError('revision_no_remaining_items')
                revised=dict(contract,request_id='rev_'+request['request_key'],
                    goal=contract['goal']+'\n\nUser correction for remaining work:\n'+request['correction'],
                    items=remaining,supersedes=job_id,budget_root=self.store.budget_root(job_id),
                    manifest_hash=digest({'contract':'background-sources-v1','items':remaining}))
                next_job=self.store.add(revised)
                with self.store.lifecycle.connect() as c:
                    c.execute("UPDATE background_revision_requests SET state='applied',next_job=? WHERE request_key=? AND state='requested'",(next_job,request['request_key']))
            except (ContractError,ValueError,OSError):
                with self.store.lifecycle.connect() as c:
                    c.execute("UPDATE background_revision_requests SET state='blocked',blocker='revision_readback_or_scope_required' WHERE request_key=?",(request['request_key'],))
    def family_page(self,job_id,owner,session_id,*,offset=0,length=100):
        self.store.row(job_id,owner,session_id)
        if type(offset) is not int or offset<0 or type(length) is not int or not 1<=length<=500:raise ContractError('bounded_item_page')
        jobs=[];current=job_id
        for _ in range(64):
            row=self.store.row(current,owner,session_id);jobs.append(row)
            with self.store.lifecycle.connect() as c:
                parent=c.execute("SELECT parent_job FROM background_revision_requests WHERE next_job=? AND state='applied'",(current,)).fetchone()
            if not parent:break
            current=parent[0]
        else:raise ContractError('revision_lineage_too_long')
        selected={}
        for row in reversed(jobs):
            for item in self.store.items(row['job_id']):
                if row['job_id']==job_id or item['state']=='verified':
                    selected[item['item_id']]={**item,'origin_job_id':row['job_id'],'origin_scope_hash':row['scope_hash']}
        items=[selected[key] for key in sorted(selected)];end=min(offset+length,len(items))
        return {'job_id':job_id,'items':items[offset:end],'total':len(items),'next_offset':end if end<len(items) else None,'truncated':False}
    def item_page(self,job_id,owner,session_id,*,offset=0,length=100):return self.store.item_page(job_id,owner,session_id,offset=offset,length=length)
    def cancel(self, job_id, owner, session_id):
        row = self.store.row(job_id,owner,session_id)
        with self.store.lifecycle.connect() as c:
            c.execute("UPDATE background_revision_requests SET state='blocked',blocker='cancel_requested_after_revision' WHERE parent_job=? AND state='requested'",(job_id,))
        if row['state'] not in {'completed','completed_with_blockers','cancelled'}:
            self.store.update_job(job_id,cancel_requested=1,state='cancelling')
            if not self.fixture:self.ensure_supervisor()
        return self.status(job_id,owner,session_id)
    def resume(self, job_id, owner, session_id, *, launch=True):
        row = self.store.row(job_id,owner,session_id)
        with self.store.lifecycle.connect() as c:
            if c.execute('SELECT 1 FROM background_revision_requests WHERE parent_job=?',(job_id,)).fetchone():raise ContractError('revised_scope_cannot_resume')
        if not self.fixture:checked_policy(self.policy,fixture=False)
        if row['state'] == 'cancelling': raise ContractError('cancel_not_settled')
        if row['state'] != 'completed':
            for item in self.store.items(job_id):
                if item['state']=='blocked':self.store.set_item(job_id,item['item_id'],'pending')
            self.store.update_job(job_id,cancel_requested=0,state='queued',receipt_ref=None,runtime_blocker=None)
            if launch: self.ensure_supervisor()
        return self.status(job_id,owner,session_id)
    def original(self, job_id, item_id, owner, session_id, *, offset=0, length=40000):
        self.store.row(job_id,owner,session_id)
        item = next((i for i in self.store.items(job_id) if i['item_id']==item_id), None)
        if item is None: raise ContractError('unknown_owned_item')
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 40000: raise ContractError('bounded_source_page')
        if not self.store.snapshot_path(job_id,item_id).is_file():raise ContractError('source_acquisition_pending')
        text = self.store.snapshot_path(job_id,item_id).read_bytes().decode('utf-8')
        identity = json.loads(item['identity'])
        if digest(text.encode()) != identity['source_hash']: raise ContractError('immutable_source_corrupt')
        if offset > len(text): raise ContractError('source_offset_out_of_range')
        end = min(offset+length,len(text))
        return {'item_id':item_id,'identity':identity,'offset':offset,'next_offset':end if end<len(text) else None,
                'offset_unit':'unicode_codepoints','text':text[offset:end],'total_characters':len(text),'truncated':False}
    def result_read(self,job_id,item_id,owner,session_id,*,offset=0,length=40000):
        from .contracts import canonical
        row=self.store.row(job_id,owner,session_id);contract=json.loads(row['contract'])
        if contract['profile']!='readonly-research':raise ContractError('review_profile_unavailable')
        if type(offset) is not int or offset<0 or type(length) is not int or not 1<=length<=40000:raise ContractError('bounded_result_page')
        with self.store.lifecycle.connect() as c:
            item=c.execute('SELECT identity,state FROM background_items WHERE job_id=? AND item_id=?',(job_id,item_id)).fetchone()
            if not item:raise ContractError('unknown_owned_item')
            if item['state']!='verified':raise ContractError('review_result_pending')
            proofs={v['phase']:dict(v) for v in c.execute("SELECT p.phase,l.state,b.proof FROM background_phases p JOIN lifecycle_jobs l ON p.native_job_id=l.job_id LEFT JOIN background_proofs b ON p.native_job_id=b.native_job_id WHERE p.job_id=? AND p.item_id=? AND p.phase IN ('analysis','artifact')",(job_id,item_id))}
        identity=json.loads(item['identity']);packets={};paths={}
        for phase in ('analysis','artifact'):
            record=proofs.get(phase)
            if not record or record['state']!='completed' or not record['proof']:raise ContractError('review_result_pending')
            expected={'job_id':job_id,'item':identity,'scope_hash':digest(contract),'phase':phase}
            ref='results/'+job_id+'/'+digest(item_id)+'/'+phase+'.json'
            path=approved_path(self.store.root,ref);body=path.read_bytes();packet=json.loads(body);proof=json.loads(record['proof'])
            if (packet.get('identity')!=expected or packet.get('result_hash')!=digest(packet.get('result')) or proof.get('identity')!=expected
                or proof.get('state')!='verified' or proof.get('destination_id')!=ref or proof.get('proof_sha256')!=digest(body)):
                raise ContractError('review_artifact_corrupt')
            packets[phase]=packet;paths[phase]=ref
        artifact=packets['artifact']['result'];analysis_packet=packets['analysis']['result']
        if artifact.get('analysis_ref')!=paths['analysis'] or artifact.get('analysis_hash')!=digest((self.store.root/paths['analysis']).read_bytes()):raise ContractError('review_artifact_corrupt')
        analysis=analysis_packet.get('analysis')
        if not isinstance(analysis,str):raise ContractError('review_artifact_corrupt')
        if offset>len(analysis):raise ContractError('result_offset_out_of_range')
        end=min(offset+length,len(analysis))
        return {'job_id':job_id,'item_id':item_id,'source_hash':identity['source_hash'],'source_version':identity['source_version'],
                'scope_hash':row['scope_hash'],'analysis_sha256':digest(analysis.encode()),'offset':offset,'next_offset':end if end<len(analysis) else None,
                'offset_unit':'unicode_codepoints','total_characters':len(analysis),'text':analysis[offset:end],'truncated':False,
                'result_review_required':True,'content_approval':None,'ready_for_send':False,'no_claim_approval':True}
    def ensure_supervisor(self):
        # A spawn is bounded local work. The supervisor's flock elects exactly one;
        # racing submitters may create short-lived losing processes, never duplicate workers.
        if self.fixture: raise ContractError('fixture_supervisor_requires_test_entry')
        if self.policy.get('driver')=='systemd':
            result=subprocess.run(['systemctl','--user','start','--no-block','ole-background.service'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=2)
            if result.returncode:raise ContractError('background_service_start_failed')
            return
        entry = Path(__file__).with_name('supervisor_entry.py').resolve()
        core = Path(self.policy.get('core','/home/hermes/.hermes/hermes-agent')).resolve()
        sys.path.insert(0,str(core))
        from hermes_cli._launchers import runtime_command
        command = runtime_command(core,args=[str(self.store.root),self.policy['policy_path']],
                 code=f"runpy.run_path({str(entry)!r}, run_name='__main__')",home=self.policy.get('hermes_home','/home/hermes/.hermes'))
        env = {k:os.environ[k] for k in ('PATH','LANG','LC_ALL','TZ','HOME') if k in os.environ}
        env['HERMES_HOME'] = self.policy.get('hermes_home','/home/hermes/.hermes')
        env['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
        env['HERMES_CHILD_SLOT_ROOT']=self.policy['slot_root']
        with open(os.devnull,'wb') as null:
            subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=null,stderr=null,env=env,start_new_session=True,close_fds=True)
