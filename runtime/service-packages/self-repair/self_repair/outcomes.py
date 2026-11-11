"""Proactive outcome reconciliation inside the existing repair controller.

The monitor is read-only for Gmail/Calendar. It records durable repair work;
only the existing native recovery owner may create/update a draft.
"""
from __future__ import annotations
import fcntl
import importlib
import json
import re
import sys
import threading
import time
from pathlib import Path
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from self_repair.store import canonical,hash_value,private_json
from self_repair.sources import LIVE,TARGETS
from self_repair.policy import digest
from self_repair.subscription import SubscriptionStatus

GRACE_SECONDS=180
SCAN_SECONDS=900
INTERVAL_SECONDS=60
ITEMS_PER_TICK=3
REVIEW_WINDOW=86400
REVIEW_TIME_BUDGET=1200
REVIEW_RESERVATION=360
BERLIN=ZoneInfo('Europe/Berlin')

class OutcomeStore:
    def __init__(self,incidents):
        self.incidents=incidents;self.root=incidents.root/'outcomes'
        self.root.mkdir(mode=0o700,exist_ok=True)
        with incidents.lifecycle.connect()as db:
            db.execute('''CREATE TABLE IF NOT EXISTS mail_outcome_sources(
                account TEXT NOT NULL,message_id TEXT NOT NULL,thread_id TEXT NOT NULL,
                first_seen REAL NOT NULL,updated REAL NOT NULL,next_check REAL NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending',input_key TEXT NOT NULL DEFAULT '',
                receipt_path TEXT NOT NULL DEFAULT '',model_started REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(account,message_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS mail_outcome_coverage(
                account TEXT PRIMARY KEY,watermark REAL NOT NULL,next_scan REAL NOT NULL,
                upper_bound REAL NOT NULL DEFAULT 0,page_token TEXT NOT NULL DEFAULT '')''')
            db.execute('''CREATE TABLE IF NOT EXISTS mail_outcome_model_runs(
                input_key TEXT PRIMARY KEY,started REAL NOT NULL,finished REAL NOT NULL DEFAULT 0,
                seconds REAL NOT NULL DEFAULT 0)''')
            columns={row['name']for row in db.execute('PRAGMA table_info(mail_outcome_sources)')}
            if 'provider_failure'not in columns:
                db.execute("ALTER TABLE mail_outcome_sources ADD COLUMN provider_failure TEXT NOT NULL DEFAULT ''")
            if 'policy_epoch'not in columns:
                db.execute("ALTER TABLE mail_outcome_sources ADD COLUMN policy_epoch TEXT NOT NULL DEFAULT ''")
    def reserve_review(self,key,now):
        with self.incidents.lifecycle.connect()as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM mail_outcome_model_runs WHERE input_key=?',(key,)).fetchone():return 'already_dispatched'
            used=db.execute('''SELECT COALESCE(SUM(CASE WHEN finished=0 THEN ? ELSE seconds END),0)
                FROM mail_outcome_model_runs WHERE started>?''',(REVIEW_RESERVATION,now-REVIEW_WINDOW)).fetchone()[0]
            if used+REVIEW_RESERVATION>REVIEW_TIME_BUDGET:return 'budget_wait'
            db.execute('INSERT INTO mail_outcome_model_runs(input_key,started)VALUES(?,?)',(key,now))
        return 'reserved'
    def finish_review(self,key,now,seconds):
        with self.incidents.lifecycle.connect()as db:
            db.execute('UPDATE mail_outcome_model_runs SET finished=?,seconds=? WHERE input_key=? AND finished=0',
                (now,max(0,seconds),key))
    def observe(self,item,now):
        if item.get('account')not in{'code','stealth'}or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',str(item.get(k,'')))for k in('id','threadId')):
            raise ValueError('outcome_identity_invalid')
        with self.incidents.lifecycle.connect()as db:
            db.execute('''INSERT OR IGNORE INTO mail_outcome_sources(account,message_id,thread_id,first_seen,updated,next_check)
                VALUES(?,?,?,?,?,?)''',(item['account'],item['id'],item['threadId'],now,now,now+GRACE_SECONDS))
    def update(self,item,now,**fields):
        allowed={'state','next_check','input_key','receipt_path','model_started','provider_failure'}
        if set(fields)-allowed:raise ValueError('outcome_update_invalid')
        with self.incidents.lifecycle.connect()as db:
            db.execute('UPDATE mail_outcome_sources SET updated=?,'+','.join(k+'=?'for k in fields)+
                ' WHERE account=? AND message_id=?',[now,*fields.values(),item['account'],item['id']])
    def pending(self,now):
        with self.incidents.lifecycle.connect()as db:
            return [dict(r)for r in db.execute('''SELECT * FROM mail_outcome_sources
                WHERE state NOT IN ('closed','verified') AND next_check<=?
                ORDER BY next_check,first_seen LIMIT ?''',(now,ITEMS_PER_TICK))]
    def policy_recheck(self,item,epoch,now):
        """One approved evidence change; no job/model budget or attempt reset."""
        if not re.fullmatch(r'[a-f0-9]{64}',str(epoch)):raise ValueError('outcome_policy_epoch_invalid')
        self.observe(item,now)
        with self.incidents.lifecycle.connect()as db:
            db.execute('''UPDATE mail_outcome_sources SET policy_epoch=?,updated=?,next_check=?,
                state=CASE WHEN state='verified' THEN 'pending' ELSE state END
                WHERE account=? AND message_id=? AND thread_id=? AND policy_epoch<>? AND state<>'closed' ''',
                (epoch,now,now,item['account'],item['id'],item['threadId'],epoch))

class NativeReads:
    def __init__(self,home=LIVE):
        self.home=Path(home);sys.path.insert(0,str(self.home/'scripts'))
        self.runtime=importlib.import_module('email_realtime')
        self.watcher=importlib.import_module('email_watcher')
        self.standard=importlib.import_module('mail_outcomes')
        self.result_io=importlib.import_module('mail_result_io')
    def metadata(self,account,message_id):
        service=self.service(account)
        return service.users().messages().get(userId='me',id=message_id,format='metadata',
            metadataHeaders=self.watcher.HEADERS).execute()
    def service(self,account):
        service=self.watcher.service(account)
        expected={'code':'operator@primary.example.invalid','stealth':'operator@secondary.example.invalid'}[account]
        if self.watcher.verified_profile(service).casefold()!=expected:raise ValueError('mailbox_identity_mismatch')
        return service
    def discover(self,account,since,until,page_token=''):
        # Independent of Inbox/category filters and the normal poller's cursor.
        return self.service(account).users().messages().list(userId='me',
            q=f'after:{int(since)-600} before:{int(until)+1} -in:spam -in:trash -in:sent -in:drafts',
            maxResults=100,**({'pageToken':page_token}if page_token else{})).execute()
    def facts(self,item):return self.result_io.current(item,runtime=self.runtime,watcher=self.watcher)
    def calendar_rechecks(self):
        # Reconcile affected own conflict drafts, not a hardcoded customer's
        # record. Local native receipts select candidates; facts() freshly
        # verifies Gmail/calendar before a review or any recovery write.
        now=time.monotonic()
        if now<getattr(self,'_calendar_recheck_next',0):return []
        self._calendar_recheck_next=now+SCAN_SECONDS
        policy=importlib.import_module('mail_policy');result=[];seen=set()
        root=getattr(self.runtime,'DATA',getattr(self,'home',LIVE)/'email_watcher')/'cases'
        items=[]
        for path in sorted(root.glob('*.json')):
            if path.is_symlink()or path.stat().st_size>1_000_000:continue
            try:case=json.loads(path.read_bytes())
            except (ValueError,OSError):continue
            item={'account':case.get('account'),'id':case.get('message_id'),'threadId':case.get('thread_id')}
            if (item['account']in{'code','stealth'}and all(re.fullmatch(r'[A-Za-z0-9_-]{1,100}',str(item[k]or''))for k in('id','threadId'))
                    and path.name==item['account']+'-'+item['id']+'.json'):items.append(item)
        claims=policy._read()
        for item in items:
            key=(item['account'],item['id'])
            if key in seen:continue
            seen.add(key);case=self.runtime.read_case(item);outcome=case.get('outcome')or{}
            original=case.get('original_thread')or[]
            if (case.get('phase')in{'draft_write_pending','draft_update_pending'}
                    or outcome.get('status')not in{'created','updated'}or not outcome.get('draft_id')
                    or(case.get('calendar')or{}).get('relation')!='offer_conflict'
                    or not original or original[-1].get('id')!=item['id']
                    or item['threadId']in self.runtime.CLOSED_THREADS.get(item['account'],set())
                    or self.runtime.closed_source_lineage(item['account'],item['threadId'],original)):continue
            source=policy.CalendarSource(item['account'],item['threadId'],original);epoch=None
            with source.activate():
                owner=policy.source_owner()
                if owner:
                    encoded=json.dumps(owner,separators=(',',':'))
                    for claim in claims.values():
                        if claim.get('state')!='held_draft_verified':continue
                        dt=policy.start(claim['start'])
                        offers=(case.get('understanding')or{}).get('offers')or[]
                        relevant=False
                        for offer in offers:
                            a=policy.start(offer['start']);b=policy.start(offer['end'])if offer.get('end')else None
                            if not b and offer.get('kind')=='window':b=(a+policy.timedelta(days=1)).replace(hour=0,minute=0)
                            if dt==a and not b or b and a<=dt and dt+policy.timedelta(minutes=30)<=b:relevant=True
                        if not relevant:continue
                        selected=policy._owned_claim_key(claims,encoded,dt)
                        if selected in claims and claims[selected].get('state')=='held_draft_verified':epoch=policy.HOLD_POLICY_EPOCH;break
            if not epoch and item==policy.ROOM_RECHECK_SOURCE and source.scope:epoch=policy.ROOM_POLICY_EPOCH
            if epoch:result.append((item,epoch))
        return result
    def review(self,context):return self.standard.assess(context,self.runtime.model_json)
    def reflections(self):
        # Same committed DreamStore reader as the native reflection surface.
        # Merely observing proposals never runs Dreaming or claims delivery.
        manifest=Path('/home/hermes/hermes-extensions-staging/brain/manifest.json')
        raw=json.loads(manifest.read_bytes())
        if not(raw.get('dreaming')or{}).get('enabled'):return []
        from hermes_second_brain.dreaming.config import load_dreaming_config
        from hermes_second_brain.dreaming.reflection import read_reflections
        return read_reflections(load_dreaming_config(manifest),limit=20)['items']
    def refresh(self):
        for name in('calendar_routing','mail_workflow','mail_policy','email_realtime','mail_outcomes','mail_result_io'):
            module=importlib.import_module(name)
            path=Path(module.__file__);key=digest(path)
            cache=getattr(self,'_versions',{})
            if name in cache and cache[name]!=key:importlib.reload(module)
            cache[name]=key;self._versions=cache

class Monitor:
    def __init__(self,incidents,*,reads=None,home=LIVE,targets=TARGETS,clock=time.time,subscriptions=None):
        self.incidents=incidents;self.store=OutcomeStore(incidents)
        self.home=Path(home);self.reads=reads or NativeReads(home);self.targets=targets;self.clock=clock
        self.subscriptions=subscriptions or SubscriptionStatus(root=incidents.root)
    def failure(self,item,code,*,facts=None,details=None):
        versions={rel:digest(path)for rel,path in self.targets.items()}
        facts=facts or{}
        snapshot={'kind':'mail','item':item,'case':facts.get('case')or{},
                  'original_thread':facts.get('thread')or[],
                  'outcome_check_failure':{'code':code,'contract':self.reads.standard.CONTRACT,'details':details}}
        self.incidents.record(source_key=hash_value(['outcome-check',item,code,versions]),
            component='mail',code=code,versions=versions,snapshot=snapshot)
    def intake(self,now):
        for path in sorted((self.home/'email_watcher/intake').glob('*.json')):
            if path.is_symlink():raise ValueError('outcome_intake_symlink')
            self.store.observe(json.loads(path.read_bytes()),now)
        for item,epoch in getattr(self.reads,'calendar_rechecks',lambda:[])():
            self.store.policy_recheck(item,epoch,now)
    def coverage(self,now):
        for account in('code','stealth'):
            with self.incidents.lifecycle.connect()as db:
                row=db.execute('SELECT * FROM mail_outcome_coverage WHERE account=?',(account,)).fetchone()
                if row is None:
                    db.execute('INSERT INTO mail_outcome_coverage(account,watermark,next_scan)VALUES(?,?,0)',(account,now-86400))
                    row=db.execute('SELECT * FROM mail_outcome_coverage WHERE account=?',(account,)).fetchone()
                row=dict(row)
            if row['next_scan']>now:continue
            until=row['upper_bound']or now
            try:
                page=self.reads.discover(account,row['watermark'],until,row['page_token'])
                for message in page.get('messages',[]):
                    self.store.observe({'account':account,'id':message['id'],'threadId':message['threadId']},now)
                token=page.get('nextPageToken','')
                with self.incidents.lifecycle.connect()as db:
                    db.execute('''UPDATE mail_outcome_coverage SET watermark=?,upper_bound=?,page_token=?,next_scan=?
                        WHERE account=?''',(row['watermark']if token else until,until if token else 0,token,
                            now+INTERVAL_SECONDS if token else now+SCAN_SECONDS,account))
            except Exception:
                # A broken account never hides the healthy one or advances its
                # cursor. Existing account-health notifications own auth alerts.
                with self.incidents.lifecycle.connect()as db:
                    db.execute('UPDATE mail_outcome_coverage SET next_scan=? WHERE account=?',(now+300,account))
    def check(self,row,now):
        item={'account':row['account'],'id':row['message_id'],'threadId':row['thread_id']}
        runtime=getattr(self.reads,'runtime',None)
        if runtime and(item['threadId']in runtime.CLOSED_THREADS.get(item['account'],set())):
            self.store.update(item,now,state='closed',next_check=now);return
        # The monitor never competes with the running normal owner of a case.
        path=self.home/'email_watcher/cases'/(item['account']+'-'+item['id']+'.lock')
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with path.open('a')as lock:
            try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                self.store.update(item,now,state='waiting_for_owner',next_check=now+60);return
            facts=self.reads.facts(item)
        if facts['state']=='superseded':
            self.store.update(item,now,state='closed',next_check=now);return
        if facts['state']!='current':
            self.failure(item,'outcome_existing_result_unverified',facts=facts)
            self.store.update(item,now,state='requires_readback',next_check=now+300);return
        case=facts.get('case')or{};thread=facts['thread'];incoming=thread[-1]
        if runtime and runtime.excluded_platform_mail(incoming.get('from')):
            self.store.update(item,now,state='closed',next_check=now);return
        # An explicit user decision remains an explicit decision, never code to
        # repair or permission to send an invitation.
        if(case.get('outcome')or{}).get('status')=='deliberately_closed':
            self.store.update(item,now,state='closed',next_check=now);return
        versions={rel:digest(path)for rel,path in self.targets.items()}
        stable_thread=[{k:v for k,v in m.items()if k not in{'labels','truncated'}}for m in thread]
        # An unchanged failed case never buys another model check just because
        # the date changed. Fresh calendar readbacks alter the key when needed.
        worker=self.home/'scripts/mail_model_worker.py'
        status=self.home/'scripts/mail_model_status.py'
        key_parts=['saved-time-claims-v1',self.reads.standard.CONTRACT,self.reads.standard.REVIEW,
                        digest(worker)if worker.exists()else None,
                        digest(status)if status.exists()else None,
                        stable_thread,facts.get('draft'),facts.get('hard_facts')]
        if row.get('policy_epoch'):key_parts.append({'calendar_policy_epoch':row['policy_epoch']})
        key=hash_value(key_parts)
        wait=json.loads(row.get('provider_failure')or'null')if row['input_key']==key else None
        dispatch=key
        if wait:
            current=self.subscriptions.read('mail',expected_account=(wait.get('availability')or{}).get('account_fingerprint'),restore=True)
            if not self.subscriptions.advances(wait.get('availability'),current,code=wait.get('code')):
                self.store.update(item,now,state='review_provider_wait',next_check=now+300);return
            dispatch=hash_value([key,'provider_progress',self.subscriptions.key(current)])
        receipt=None
        saved_receipt=self.store.root/(key+'.json')
        if row['input_key']==key and row['receipt_path']:
            receipt=json.loads(Path(row['receipt_path']).read_bytes())
        elif saved_receipt.is_file()and not saved_receipt.is_symlink():
            receipt=json.loads(saved_receipt.read_bytes())
        elif row['input_key']==key and row['model_started']and not wait:
            # Interruption after model dispatch is not proof the model did not
            # run. Keep its source for diagnosis instead of buying a loop.
            if row['state']!='review_failed':self.failure(item,'outcome_review_interrupted',facts=facts)
            self.store.update(item,now,state='review_interrupted',next_check=now+900);return
        if receipt is None:
            if hasattr(self.reads,'result_io'):
                receipt=self.reads.result_io.verified_before_save(case,facts)
            if receipt is None:
                current=self.subscriptions.read('mail',restore=True)
                if current.get('state')in{'quota','auth'}:
                    failure={'code':'mail_model_'+current['state'],'availability':current}
                    self.store.update(item,now,state='review_provider_wait',next_check=now+300,
                        input_key=key,receipt_path='',model_started=0,provider_failure=canonical(failure));return
                admission=self.store.reserve_review(dispatch,now)
                if admission=='budget_wait':
                    self.store.update(item,now,state='review_budget_wait',next_check=now+900);return
                if admission=='already_dispatched':
                    self.failure(item,'outcome_review_interrupted',facts=facts)
                    self.store.update(item,now,state='review_interrupted',next_check=now+900,input_key=key,model_started=now);return
                self.store.update(item,now,state='reviewing',next_check=now+300,input_key=key,
                    receipt_path='',model_started=now)
                received=datetime.fromtimestamp(int(incoming['internalDate'])/1000,BERLIN).isoformat()if incoming.get('internalDate')else None
                context={'account':item['account'],'current_message':item['id'],'thread':thread,'review_phase':'saved',
                         'received_at':received,'processing_at':datetime.fromtimestamp(now,BERLIN).isoformat(),
                         'draft':facts.get('draft'),'user_question':(case.get('outcome')or{}).get('question'),
                         'business_context':case.get('business_context'),'hard_facts':facts.get('hard_facts',{}),
                         'require_time_claims':True}
                started=time.monotonic()
                try:receipt=self.reads.review(context)
                except Exception as error:
                    code=getattr(error,'code',None)or type(error).__name__
                    if not re.fullmatch(r'[A-Za-z0-9_]{1,100}',str(code)):code=type(error).__name__
                    if code in {'mail_model_quota','mail_model_auth','mail_model_unavailable'}:
                        failure={'code':code,'availability':getattr(error,'availability',None)or self.subscriptions.read('mail',force=True)}
                        self.store.update(item,now,state='review_provider_wait',next_check=now+300,
                            provider_failure=canonical(failure));return
                    self.failure(item,'outcome_review_'+code,facts=facts,details={
                        'context':context,'rejected':getattr(error,'rejected',[])})
                    self.store.update(item,now,state='review_failed',next_check=now+900);return
                finally:self.store.finish_review(dispatch,self.clock(),time.monotonic()-started)
            private_json(self.store.root/(key+'.json'),receipt)
            self.store.update(item,now,provider_failure='')
        path=str(self.store.root/(key+'.json'))
        if not Path(path).exists():private_json(path,receipt)
        if versions!={rel:digest(p)for rel,p in self.targets.items()}:
            self.store.update(item,now,state='source_version_changed',next_check=now+60,model_started=0);return
        verdict=receipt['review'];hard=facts.get('hard_facts',{})
        # Also bind any positively identified starts in a returned/cached
        # receipt. A scheduling_required=false label cannot waive evidence.
        try:self.reads.standard.validate_write_claims(verdict,{'review_phase':'saved','hard_facts':hard})
        except self.reads.standard.OutcomeReviewError as error:
            self.failure(item,'outcome_review_'+error.code,facts=facts)
            self.store.update(item,now,state='review_failed',next_check=now+900);return
        if verdict['scheduling_required']and facts.get('draft')and not hard.get('calendar_verified'):
            hard={**hard,'hard_errors':sorted(set([*hard.get('hard_errors',[]),'mail_calendar_evidence_missing']))}
        state=verdict['result']
        if state=='not_required'or(state=='needs_user'and not facts.get('draft')and
                                   (case.get('outcome')or{}).get('question')):
            self.store.update(item,now,state='closed',next_check=now,input_key=key,receipt_path=path,model_started=0);return
        uncertain=case.get('phase')in{'draft_write_pending','draft_update_pending'}or(
            (case.get('outcome')or{}).get('status')=='needs_manual_review_draft_state')
        if state=='ready'and not hard.get('hard_errors')and not uncertain:
            self.store.update(item,now,state='verified',next_check=now,input_key=key,receipt_path=path,model_started=0);return
        if uncertain:code='reply_standard_failed'
        elif facts.get('draft')and not facts.get('owned_draft'):
            # Another writer's draft is retained. Its detected content issue is
            # still code evidence, but native recovery may not overwrite it.
            code='reply_existing_draft_needs_review'
        else:code='reply_result_missing'if state=='missing'else'reply_standard_failed'
        snapshot={'kind':'mail','item':item,'case':case,'original_thread':thread,
                  'expectation':{'contract':self.reads.standard.CONTRACT,'code':code,'receipt':receipt,
                      'checked_draft':facts.get('draft'),'hard_facts':hard,
                      'owned_draft':facts.get('owned_draft'),'input_key':key}}
        source_key=hash_value(['outcome',item['account'],item['id'],key,code])
        self.incidents.record(source_key=source_key,component='mail',code=code,versions=versions,snapshot=snapshot)
        self.store.update(item,now,state='repair_recorded',next_check=now+300,input_key=key,receipt_path=path,model_started=0)
    def link_reflections(self):
        """Source bindings are reviewed metadata, never another work queue.

        A session suggestion alone cannot identify a customer mail. Root may
        bind its immutable ID/hash to an independently observed exact source.
        The live reader and native current-evidence gate run again each time.
        """
        path=self.incidents.root/'reflection-bindings.json'
        if not path.exists()or not hasattr(self.reads,'reflections'):return []
        if path.is_symlink()or path.stat().st_size>65536:raise ValueError('reflection_bindings_invalid')
        payload=json.loads(path.read_bytes())
        if payload.get('contract')!='dream-mail-source-bindings/v1':raise ValueError('reflection_bindings_invalid')
        bindings=payload.get('bindings')
        if not isinstance(bindings,list)or len(bindings)>20:raise ValueError('reflection_bindings_invalid')
        records=self.reads.reflections()
        frozen={record['item_id']:record for record in records}
        from self_repair.proposals import admit_reflection
        results=[]
        for binding in bindings[:ITEMS_PER_TICK]:
            record=frozen.get(binding.get('item_id'))
            if not record or record.get('item_sha256')!=binding.get('item_sha256'):continue
            if not re.fullmatch(r'[a-f0-9]{64}',binding.get('source_key','')):continue
            def load(item_id):
                return next((r for r in self.reads.reflections()if r['item_id']==item_id),None)
            results.append(admit_reflection(record,load_reflection=load,monitor=self,item=binding['item'],
                expected_source_key=binding['source_key'],relationship=binding.get('relationship')))
        return results
    def tick(self):
        if hasattr(self.reads,'refresh'):self.reads.refresh()
        now=self.clock();self.intake(now);self.coverage(now)
        for row in self.store.pending(now):
            try:self.check(row,now)
            except Exception as error:
                item={'account':row['account'],'id':row['message_id'],'threadId':row['thread_id']}
                code=getattr(error,'code',None)or type(error).__name__
                if not re.fullmatch(r'[A-Za-z0-9_]{1,100}',str(code)):code=type(error).__name__
                self.failure(item,'outcome_read_'+code)
                self.store.update(item,now,state='check_failed',next_check=now+300)
        self.link_reflections()

def start(incidents,*,home=LIVE,targets=TARGETS):
    stopped=threading.Event();monitor=Monitor(incidents,home=home,targets=targets)
    def run():
        while not stopped.is_set():
            try:monitor.tick()
            except Exception:pass  # Durable rows/cursors remain; no prompts/auth in logs.
            stopped.wait(INTERVAL_SECONDS)
    thread=threading.Thread(target=run,name='mail-outcome-monitor',daemon=True);thread.start()
    return stopped,thread
