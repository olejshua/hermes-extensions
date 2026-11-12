"""Dated ten-mail pilot. Existing sender/closure gates, one due slot per call."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import hashlib
import re
import time
import uuid

from .central import Central,Stop,atomic,fingerprint

BERLIN=ZoneInfo('Europe/Berlin')
DATE='2026-11-02'
N=10


def slots(plan=None, *, date=DATE):
    # Retain the missed original plan for the historical fixture/proof only.
    # Every production Pilot call supplies Root's explicit activation data.
    if plan is None:return [datetime(2026,11,2,7,1+2*i,tzinfo=BERLIN)for i in range(N)]
    if (plan.get('date')!=date or type(plan.get('expected'))is not int or plan['expected']!=N
            or plan.get('timezone')!='Europe/Berlin'or not isinstance(plan.get('slots'),list)
            or len(plan['slots'])!=N):raise Stop('explicit_root_ten_slot_plan_required')
    try:values=[datetime.fromisoformat(s)for s in plan['slots']]
    except (ValueError,TypeError):raise Stop('invalid_slot_datetime')
    if any(v.tzinfo is None for v in values):raise Stop('aware_Berlin_slots_required')
    values=[v.astimezone(BERLIN)for v in values]
    if (any(v.date().isoformat()!=date or v.minute%2!=1 or v.second or v.microsecond for v in values)
            or [v.isoformat()for v in values]!=plan['slots']
            or any(b-a!=timedelta(minutes=2)for a,b in zip(values,values[1:]))):
        raise Stop('same_day_odd_minute_two_minute_spacing_required')
    return values


def validate_selection(mails,expected):
    if type(expected)is not int or expected!=N:raise Stop('explicit_pilot_N10_required')
    if not isinstance(mails,list)or len(mails)!=N:raise Stop('complete_ten_ready_required')
    for field in ('to','page_id','org_domain','family_id'):
        values=[str(m.get(field)or'').strip().casefold()for m in mails]
        if field=='page_id':values=[v.replace('-','')for v in values]
        if not all(values)or len(set(values))!=N:raise Stop('missing_or_duplicate_'+field)
    for mail in mails:
        # Fixed, expressly reserved/closed prospect families for this pilot.
        # Buyer names inside TED evidence are intentionally outside this gate.
        if re.search(r'\b(excluded-company-1|excluded-company-2|excluded-company-3|excluded-company-4|excluded-company-5|excluded-company-6|excluded-company-7|excluded-company-8|excluded-company-9)\b',
                mail.get('firma','')+' '+mail.get('family_id',''),re.I):raise Stop('reserved_or_closed_prospect_family')
        if mail.get('liste')not in {'it','ingenieur','medizin','beratung'}:raise Stop('reviewed_pool_segment_required')
        if mail.get('subject_review',{}).get('understandable')is not True or mail['subject_review'].get('fits_request')is not True:
            raise Stop('subject_review_required')
        if any(c in mail['subject']+mail['body']for c in ('\u2013','\u2014')):raise Stop('long_dash')
        if 'Acribic'in mail['body']or'Interview'in mail['body']:raise Stop('wrong_template')
        if 'an der CODE University'not in mail['body']or'Grüßen\n\n'in mail['body']or'Berliner Zeit'in mail['body']:
            raise Stop('mail_style')


class Pilot:
    def __init__(self,root,v,sender,closure,gmail,*,clock=lambda:datetime.now(BERLIN),ted_gate=None,smtp_probe=None,source_files=None,bounce_check=None,family_gate=None,date=DATE,run_id=None,central_root=None,preflight_lead=0,max_lateness=120,sleeper=time.sleep,permission_check=None):
        self.root=Path(root);self.v=v;self.sender=sender;self.closure=closure;self.gmail=gmail
        self.clock=clock;self.ted_gate=ted_gate;self.smtp_probe=smtp_probe
        self.source_files=source_files or {};self.bounce_check=bounce_check
        self.family_gate=family_gate
        self.date=datetime.fromisoformat(date).date().isoformat()
        self.preflight_lead=preflight_lead;self.max_lateness=max_lateness;self.sleeper=sleeper
        self.permission_check=permission_check
        self.central=Central(central_root or self.root/'central')
        self.run_id=run_id or 'acribic-20261102-N10-0701-0719'
        self.run=self.root/'runs'/self.run_id;self.run.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.journal=self.run/'send.jsonl'

    def source_manifest(self):
        if not self.source_files:raise Stop('reviewed_source_files_required')
        return {name:hashlib.sha256(Path(path).read_bytes()).hexdigest()
                for name,path in sorted(self.source_files.items())}

    def activation(self):
        if self.permission_check:self.permission_check()
        value=json.loads((self.root/'activation.json').read_text())
        if (value.get('enabled')is not True or value.get('expected')!=N or value.get('date')!=self.date
                or value.get('sole_send_host')!='vps'or value.get('run_id')!=self.run_id):raise Stop('pilot_not_activated')
        planned=slots(value,date=self.date)
        try:activated=datetime.fromisoformat(value['activated_at'])
        except (KeyError,ValueError,TypeError):raise Stop('explicit_root_activation_time_required')
        if (activated.tzinfo is None or activated.astimezone(BERLIN).date().isoformat()!=self.date
                or activated>=planned[0]or activated>self.clock()):raise Stop('all_ten_slots_must_be_future_at_activation')
        return value

    def planned_slots(self,*,future=False):
        value=json.loads((self.root/'activation.json').read_text());planned=slots(value,date=self.date)
        if future and planned[0]<=self.clock():raise Stop('all_ten_future_slots_required_before_ready')
        return planned

    def selection(self):
        mails=json.loads((self.root/'selected.json').read_text())
        validate_selection(mails,N)
        self.identities(mails)
        self.template(mails)
        return mails

    def template(self,mails):
        for mail in mails:
            buyer=mail['subject']if mail['subject_review']['mode']=='buyer'else mail['ted_buyer']
            if mail['body']!=self.v.mailtext(mail['vorname'],mail['firma'],buyer):raise Stop('approved_template_changed')

    def identities(self,mails):
        domains=[self.v.basis(self.v.dom(m['to']))for m in mails]
        if len(set(domains))!=N or any(d!=m['org_domain']for d,m in zip(domains,mails)):
            raise Stop('actual_parent_mail_domains_not_unique_or_changed')
        names=[self.v.firma_key(m['firma'])for m in mails]
        if len(set(names))!=N:raise Stop('same_actual_company_across_source_batches')

    def scope(self,segment,groups=None):
        self.v.LISTE=segment;self.v.DB=self.v.LISTEN[segment]
        self.v.INDEX=str(Path(self.v.STATE)/f'CRM-Index-{segment}.csv')
        return self.sender.assemble_crm_snapshot(self.v,groups)if groups is not None else None

    def segments(self,mails,batch):
        names=sorted({m['liste']for m in mails});result=[]
        for name in names:
            subset=[m for m in mails if m['liste']==name]
            if len(names)==1:
                child=batch;folder=self.run
            else:
                # Separate source batches preserve the existing single-list
                # completion contract. The parent ten-mail READY gates every
                # child before any one of them can write.
                child={**batch,'batch_id':self.run_id+'-'+name,'liste':name,
                    'parent_count':N,'parent_mail_sha256':fingerprint(mails),
                    'kandidaten':[c for c in batch['kandidaten']if c['page_id']in {m['page_id']for m in subset}]}
                folder=self.run/'segments'/name
            result.append({'liste':name,'mails':subset,'batch':child,'folder':folder,'journal':folder/'send.jsonl'})
        if sum(len(s['mails'])for s in result)!=N:raise Stop('parent_ten_partition_incomplete')
        return result

    def statuses(self,mails,batch):
        result={'confirmed':set(),'uncertain':set(),'not_attempted':set()};events=[]
        for segment in self.segments(mails,batch):
            child_events=self.closure.read_journal(segment['journal'])if segment['journal'].exists()else[]
            child=self.closure.validate(segment['mails'],segment['batch'],child_events,len(segment['mails']),segment['liste'],details=True)
            for key in result:result[key].update(child[key])
            events.extend(child_events)
        return result,events

    def control(self):
        value=json.loads((self.root/'positive-control.json').read_text())
        return value['id'],value['query']

    def freshness(self):
        if self.clock().astimezone(BERLIN).date().isoformat()!=self.date:raise Stop('wrong_pilot_date')

    def candidate_read(self,mail,reader,*args):
        try:return reader(*args)
        except ValueError as error:
            if str(error).startswith(('unreviewed_smtp_exit','wrong_gmail_account')):raise
            raise Stop('candidate_qualification:'+mail['page_id']+':'+str(error))from error

    def prepare(self):
        """Read/RCPT only. Freeze all ten before any potential Gmail create/send."""
        self.freshness();planned=self.planned_slots(future=True);mails=self.selection();known,query=self.control()
        with self.central.lock():
            self.central.assert_clear()
            if self.journal.exists()or(self.run/'ready.json').exists():raise Stop('prepared_or_started_run_cannot_be_overwritten')
            sources=self.source_manifest();started=time.monotonic()
            self.sender.profile_and_control(self.gmail,known,query)
            snapshot=self.sender.crm_snapshot(self.v)
            names={m['liste']for m in mails}
            batch={'batch_id':self.run_id,'crm_backend':'twenty','liste':next(iter(names))if len(names)==1 else'multiple',
                   'datum':self.date,'kandidaten':[]}
            ip=None
            for mail in mails:
                snapshot=self.scope(mail['liste'],snapshot[0])
                self.candidate_read(mail,self.sender.validate_live,self.v,mail,snapshot)
                if self.family_gate is None:raise Stop('family_evidence_gate_required')
                self.candidate_read(mail,self.family_gate,mail,snapshot)
                self.candidate_read(mail,self.sender.no_history,self.gmail,mail,self.v)
                if self.ted_gate is None or self.smtp_probe is None:raise Stop('native_evidence_readers_required')
                self.candidate_read(mail,self.ted_gate,mail)
                probe=self.candidate_read(mail,self.smtp_probe,mail['to'])
                if probe['status']!='OK'or probe['berlin_date']!=self.date:raise Stop('current_smtp_OK_required:'+mail['firma'])
                if probe['ip']!='45.89.127.191':raise Stop('SMTP_exit_changed')
                ip=probe['ip']
                batch['kandidaten'].append({'page_id':mail['page_id'],'email':mail['to'],'smtp':'OK',
                    'smtp_probe':False,'smtp_geprueft_am':self.date,'smtp_antwort':probe['real'],'smtp_zufall':probe['random'],
                    'smtp_evidence':probe})
            batch['smtp_exit']={'ip':ip,'verified':True}
            snapshot=self.sender.crm_refresh(self.v,snapshot,self.v.TWENTY.anchor)
            for mail in mails:
                snapshot=self.scope(mail['liste'],snapshot[0])
                self.candidate_read(mail,self.sender.validate_live,self.v,mail,snapshot);self.candidate_read(mail,self.family_gate,mail,snapshot)
            segments=self.segments(mails,batch)
            for segment in segments:
                self.scope(segment['liste'])
                self.sender.validate_prepared(self.v,segment['mails'],segment['batch'],len(segment['mails']),self.smtp_status,self.date)
                if segment['folder']!=self.run:
                    atomic(segment['folder']/'mails.json',segment['mails']);atomic(segment['folder']/'batch.json',segment['batch'])
            atomic(self.run/'mails.json',mails);atomic(self.run/'batch.json',batch)
            records=self.v.TWENTY.records
            policy=self.v.TWENTY.policy
            atomic(self.run/'records.json',records);atomic(self.run/'policy.json',policy)
            if self.source_manifest()!=sources:raise Stop('source_changed_during_preparation')
            if self.planned_slots(future=True)!=planned:raise Stop('root_plan_changed_during_preparation')
            ready={'contract':'acribic-pilot-ready/v1','run_id':self.run_id,'expected':N,'ready':N,
                   'date':self.date,'prepared_at':self.clock().isoformat(),'mail_sha256':fingerprint(mails),
                   'batch_sha256':fingerprint(batch),'records_sha256':fingerprint(records),
                   'policy_sha256':fingerprint(policy),'sources':sources,'source_sha256':fingerprint(sources),
                   'segment_counts':{s['liste']:len(s['mails'])for s in segments},
                   'anchor':self.v.TWENTY.anchor,'slots':[v.isoformat()for v in planned],
                   'prepare_seconds':round(time.monotonic()-started,3),'external_business_writes':0}
            atomic(self.run/'ready.json',ready)
            return ready

    @staticmethod
    def smtp_status(real,random):
        # Injected unchanged native validator at construction/launcher.
        from smtp_pruefen import status
        return status(real,random)

    def load_ready(self):
        ready=json.loads((self.run/'ready.json').read_text())
        mails=json.loads((self.run/'mails.json').read_text());batch=json.loads((self.run/'batch.json').read_text())
        records=json.loads((self.run/'records.json').read_text())
        policy=json.loads((self.run/'policy.json').read_text())
        if (ready.get('ready')!=N or ready.get('expected')!=N or ready.get('run_id')!=self.run_id
                or ready.get('date')!=self.date or ready.get('slots')!=[v.isoformat()for v in self.planned_slots()]
                or fingerprint(mails)!=ready['mail_sha256']or fingerprint(batch)!=ready['batch_sha256']
                or fingerprint(records)!=ready['records_sha256']or fingerprint(policy)!=ready.get('policy_sha256')
                or self.source_manifest()!=ready.get('sources')
                or fingerprint(ready.get('sources'))!=ready.get('source_sha256')):raise Stop('ready_snapshot_changed')
        validate_selection(mails,N)
        self.identities(mails)
        self.template(mails)
        segments=self.segments(mails,batch)
        if ready.get('segment_counts')!={s['liste']:len(s['mails'])for s in segments}:raise Stop('segment_partition_changed')
        for segment in segments:
            self.scope(segment['liste'])
            self.sender.validate_prepared(self.v,segment['mails'],segment['batch'],len(segment['mails']),self.smtp_status,self.date)
            if segment['folder']!=self.run and (json.loads((segment['folder']/'mails.json').read_text())!=segment['mails']
                    or json.loads((segment['folder']/'batch.json').read_text())!=segment['batch']):raise Stop('source_segment_files_changed')
        self.v.TWENTY.records=records;self.v.TWENTY.policy=policy
        self.v.TWENTY.anchor=ready['anchor'];self.v.TWENTY.groups()
        self.v.MAILS=str(self.run/'mails.json');self.v.BATCH=str(self.run/'batch.json')
        return ready,mails,batch

    def check_ready(self):
        """Scheduled nightly read/RCPT recheck; never replace frozen READY."""
        self.freshness()
        with self.central.lock():
            self.central.assert_clear();ready,mails,batch=self.load_ready()
            if any(e['run_id']==self.run_id for e in self.central.events()):raise Stop('night_check_after_attempt_forbidden')
            proof={'passed':False,'expected':N,'ready_sha256':fingerprint(ready),'checked_at':self.clock().isoformat(),'smtp':[]}
            atomic(self.run/'night-check.json',proof)
            known,query=self.control();self.sender.profile_and_control(self.gmail,known,query)
            snapshot=self.sender.crm_refresh(self.v,None,ready['anchor'])
            for mail in mails:
                snapshot=self.scope(mail['liste'],snapshot[0]);self.candidate_read(mail,self.sender.validate_live,self.v,mail,snapshot)
                self.candidate_read(mail,self.family_gate,mail,snapshot);self.candidate_read(mail,self.sender.no_history,self.gmail,mail,self.v);self.candidate_read(mail,self.ted_gate,mail)
                smtp=self.candidate_read(mail,self.smtp_probe,mail['to'])
                if smtp['status']!='OK'or smtp['berlin_date']!=self.date or smtp['ip']!='45.89.127.191':raise Stop('night_SMTP_gate_failed:'+mail['firma'])
                proof['smtp'].append({'mailbox':mail['to'],**smtp})
            if self.source_manifest()!=ready['sources']:raise Stop('source_changed_during_nightly_check')
            self.planned_slots(future=True)
            proof.update(passed=True,complete_count=N,source_sha256=ready['source_sha256'])
            atomic(self.run/'night-check.json',proof)
            return {'state':'ten_nightly_checks_passed','expected':N,'real_SaaS_writes':0}

    def request(self,mail):
        return {'recipient_email':mail['to'],'subject':mail['subject'],
                'body':mail['body'],'from_email':self.closure.SENDER}

    def event(self,mail,batch,kind,attempt_id,**fields):
        common={'schema_version':1,'batch_id':batch['batch_id'],'batch_sha256':self.closure.fingerprint(batch),
            'page_id':self.closure.page_id(mail['page_id']),'email':self.closure.mailbox(mail['to']),
            'content_sha256':self.closure.fingerprint(self.closure.content(mail)),'attempt_id':attempt_id}
        payload={**common,'at':datetime.now(timezone.utc).isoformat(),'event':kind,
                 'request_sha256':fingerprint(self.request(mail)),**fields}
        central_args={'run_id':self.run_id,'mailbox':mail['to'],'org_domain':mail['org_domain'],'source_id':mail['page_id'],'payload':payload}
        if kind=='attempt':self.central.reserve(**central_args)
        else:self.central.append(kind,**central_args)
        # Central first. A crash here leaves a durable global block even if the
        # legacy per-run append never reached disk. No second provider attempt.
        journal=self.run/'segments'/mail['liste']/'send.jsonl'
        if not journal.parent.exists():journal=self.journal
        self.sender.append_event(journal,payload)

    def due(self,index):
        if type(index)is not int or not 0<=index<N:raise Stop('invalid_slot')
        now=self.clock().astimezone(BERLIN);start=self.planned_slots()[index]
        if now<start-timedelta(seconds=self.preflight_lead):raise Stop('slot_not_due')
        # Native Cron polls once per minute. A valid dispatch may therefore
        # arrive at :59; the slot remains usable only until the next slot.
        if now>=start+timedelta(seconds=self.max_lateness):raise Stop('slot_missed_no_catchup')

    def tick(self,index):
        self.freshness()
        with self.central.lock():
            activation=self.activation()
            self.central.assert_clear();ready,mails,batch=self.load_ready()
            if (activation.get('review_sha256')!=ready['mail_sha256']
                    or activation.get('ready_sha256')!=fingerprint(ready)
                    or activation.get('source_sha256')!=ready['source_sha256']):raise Stop('activation_selection_changed')
            night=json.loads((self.run/'night-check.json').read_text())
            if (night.get('passed')is not True or night.get('complete_count')!=N
                    or night.get('ready_sha256')!=fingerprint(ready)
                    or night.get('source_sha256')!=ready['source_sha256']
                    or datetime.fromisoformat(night['checked_at']).astimezone(BERLIN).date().isoformat()!=self.date):
                raise Stop('complete_nightly_recheck_required')
            status,events=self.statuses(mails,batch)
            if status['uncertain']:raise Stop('prior_attempt_uncertain')
            mail=mails[index]
            selected_segment=next(s for s in self.segments(mails,batch)if s['liste']==mail['liste'])
            if mail['to'].casefold()in status['confirmed']:
                # No send. The committed central state must agree with the
                # native per-run and CRM completion evidence before reuse.
                state=self.central.states().get((self.run_id,mail['to'].casefold()))
                if not state or state['kind']!='crm_committed':raise Stop('confirmed_but_CRM_unresolved')
                return {'state':'already_committed','sent':0}
            for earlier in mails[:index]:
                if earlier['to'].casefold()not in status['confirmed']:raise Stop('earlier_slot_unfinished_no_burst')
            self.due(index)
            if self.source_manifest()!=ready['sources']:raise Stop('source_changed_during_preflight')
            started=time.monotonic()
            known,query=self.control();self.sender.profile_and_control(self.gmail,known,query)
            snapshot=self.sender.crm_refresh(self.v,(None,None,None),ready['anchor'])
            # All ten are rechecked before the first write; completed members
            # are never reinterpreted as new candidates on later slot calls.
            remaining=[m for m in mails if m['to'].casefold()not in status['confirmed']]
            for candidate in remaining:
                snapshot=self.scope(candidate['liste'],snapshot[0])
                self.sender.validate_live(self.v,candidate,snapshot)
                if self.family_gate is None:raise Stop('family_evidence_gate_required')
                self.family_gate(candidate,snapshot)
                if not events:self.sender.no_history(self.gmail,candidate,self.v)
            self.sender.no_history(self.gmail,mail,self.v)
            if self.ted_gate:self.ted_gate(mail)
            if self.bounce_check is None:raise Stop('native_bounce_reader_required')
            bounce=self.bounce_check(events)
            atomic(self.run/'latest-bounce-check.json',bounce)
            if bounce.get('confirmed')or bounce.get('uncertain'):
                self.central.append('bounce'if bounce.get('confirmed')else'run_halted',run_id=self.run_id,payload=bounce)
                raise Stop('bounce_or_uncertain_delivery_report')
            atomic(self.run/f'preflight-{index}.json',{'seconds':round(time.monotonic()-started,3),
                'at':self.clock().isoformat(),'remaining':len(remaining),'segments':{k:len(rows)for k,rows in snapshot[0].items()},
                'delta_anchor':ready['anchor'],'send_due':ready['slots'][index]})
            # A slow preflight must not turn a missed minute into a catchup send.
            self.due(index)
            self.scope(mail['liste'])
            self.v.MAILS=str(selected_segment['folder']/'mails.json');self.v.BATCH=str(selected_segment['folder']/'batch.json')
            child_batch=selected_segment['batch']
            attempt_id=str(uuid.uuid4());self.event(mail,child_batch,'attempt',attempt_id)
            self.gmail.central=self.central;self.gmail.run_id=self.run_id
            self.gmail.before_post=lambda:self.before_post(index,activation)
            ack=self.gmail('GMAIL_SEND_EMAIL',self.request(mail))
            mid=self.sender.message_id(ack);self.event(mail,child_batch,'send_ack',attempt_id,message_id=mid)
            message=self.sender.readback(self.gmail,mail,mid)
            if message.get('body') is None or self.gmail.normalized_body(message['body'])!=self.gmail.normalized_body(mail['body']):
                raise Stop('sent_body_readback_missing_or_different')
            self.event(mail,child_batch,'sent_readback',attempt_id,message_id=mid,
                sender=self.sender.single_address(message['sender']),to=self.sender.single_address(message['to']),
                subject=message['subject'],labels=message['labelIds'],rfc_message_id=message.get('rfc_message_id'),
                sent_internal_date=message.get('internalDate'),planned_slot=self.planned_slots()[index].isoformat(),
                body_sha256=hashlib.sha256(self.gmail.normalized_body(message['body']).encode()).hexdigest())
            self.central.append('crm_attempt',run_id=self.run_id,mailbox=mail['to'],org_domain=mail['org_domain'],source_id=mail['page_id'])
            result=self.closure.complete(self.v,selected_segment['journal'],len(selected_segment['mails']),probe=False)
            if mail['to'].casefold()not in set(result['confirmed'])|set(result['already_committed']):raise Stop('CRM_completion_missing')
            self.central.append('crm_readback',run_id=self.run_id,mailbox=mail['to'],org_domain=mail['org_domain'],source_id=mail['page_id'])
            self.central.append('crm_committed',run_id=self.run_id,mailbox=mail['to'],org_domain=mail['org_domain'],source_id=mail['page_id'])
            return {'state':'sent_and_CRM_committed','sent':1,'slot':index,'message_id':mid}

    def before_post(self,index,bound_activation):
        # Called by the transport after profile/OAuth, directly before POST.
        # The durable attempt deliberately survives a gate/timeout failure.
        if not self.central.held:raise Stop('central_lock_required_at_POST')
        start=self.planned_slots()[index]
        remaining=(start-self.clock().astimezone(BERLIN)).total_seconds()
        if remaining>self.preflight_lead:raise Stop('outside_preflight_window')
        if remaining>0:self.sleeper(remaining)
        if fingerprint(self.activation())!=fingerprint(bound_activation):raise Stop('activation_changed_before_POST')
        self.freshness();self.due(index)
        if self.clock().astimezone(BERLIN)<start:raise Stop('early_POST_forbidden')
        atomic(self.run/f'post-gate-{index}.json',{'scheduled':start.isoformat(),'post_gate_at':self.clock().isoformat(),
            'late_seconds':round((self.clock()-start).total_seconds(),3),'early_send':False})

    def result(self):
        """Only persisted native evidence; never infer outcome from a timeout."""
        ready,mails,batch=self.load_ready()
        status,events=self.statuses(mails,batch)
        states=self.central.states();confirmed=set(status['confirmed']);committed=set()
        for mail in mails:
            address=mail['to'].casefold();value=states.get((self.run_id,address))
            if value and value['kind']=='crm_committed'and address in confirmed:committed.add(address)
        uncertain=set(status['uncertain'])
        for (run_id,address),value in states.items():
            if run_id==self.run_id and address and value['kind']not in {'crm_committed'} and address not in confirmed:
                uncertain.add(address)
        not_attempted=set(status['not_attempted'])-uncertain
        delivery=json.loads((self.run/'latest-bounce-check.json').read_text())if(self.run/'latest-bounce-check.json').exists()else None
        answer={'requested':N,'prepared':N,'sent_confirmed':len(confirmed),'crm_confirmed':len(committed),
                'send_uncertain':len(uncertain),'not_attempted':len(not_attempted),
                'crm_open':len(confirmed-committed),'bounces':None if delivery is None else len(delivery.get('confirmed',[])),
                'delivery_uncertain':None if delivery is None else len(delivery.get('uncertain',[])),
                'at':self.clock().isoformat(),'delivery_checked_at':None if delivery is None else delivery.get('checked_at')}
        if delivery is None:
            delivery_text='Zustellmeldungen noch nicht geprüft'
        else:
            delivery_text=(f"{answer['bounces']} bestätigte Rückläufer, "
                           f"{answer['delivery_uncertain']} unklare Zustellmeldungen")
        answer['text']=(f"Acribic-Test: {len(confirmed)}/{N} Mails in Gesendet bestätigt, {len(committed)}/{N} im CRM verbucht. "
                        f"{len(not_attempted)} noch ohne Versandversuch, {len(uncertain)} Versandversuche unklar, "
                        f"{len(confirmed-committed)} CRM-Abschlüsse offen; {delivery_text}.")
        atomic(self.run/'result.json',answer)
        return answer

    def _attempt_record(self,events,address):
        found=[e for e in events if e.get('event')=='attempt'and self.closure.mailbox(e.get('email'))==address]
        if len(found)!=1:raise Stop('resolve_requires_exactly_one_attempt_record')
        return found[0]

    def _sent_proof(self,mail,address):
        """Read-only Gmail Sent proof: exactly one message, else nothing changes."""
        subject=mail['subject']
        if any(c in subject for c in ('"','\r','\n')):raise Stop('subject_cannot_form_safe_query')
        result=self.sender.fetch(self.gmail,f'in:sent to:{address} subject:"{subject}"',100)
        if result.get('nextPageToken'):raise Stop('sent_search_incomplete_no_resolution')
        found={}
        for message in result['messages']:
            try:
                mid=self.sender.message_id(message);self.sender.verify_sent(message,mid,mail)
            except ValueError:continue
            found[mid]=message
        if len(found)>1:raise Stop('ambiguous_sent_matches_resolve_nothing')
        if not found:return None
        mid,message=next(iter(found.items()))
        return {'message_id':mid,'sender':self.sender.single_address(message['sender']),
                'to':self.sender.single_address(message['to']),'subject':message['subject'],'labels':message['labelIds']}

    def resolve(self,email):
        """Safely resolve one unclear send (aborted attempt/lost ACK); never sends.

        An interrupted attempt deliberately blocks the whole campaign (no blind
        retry). Resolution is allowed only with exact, unique, read-only Gmail
        Sent evidence for the prepared sender/recipient/subject. The central
        account journal is reconciled first, the run journal second - the live
        write order. Zero or ambiguous matches, conflicting message IDs or any
        schema break resolve nothing and leave every reservation untouched.
        No CRM write happens here; the existing completion owns that step.
        """
        address=self.closure.mailbox(email)
        with self.central.lock():
            # No OTHER unresolved send may exist. Proven-sent mails awaiting
            # their CRM commit (sent_readback/crm_*) are a normal halted-run
            # state and never block evidence recovery; halt/bounce events gate
            # the completion step, not the read-only proof gathered here.
            for key,event in self.central.states().items():
                if key!=(self.run_id,address) and event['kind'] in ('attempt','send_ack'):
                    raise Stop('other_unresolved_attempt_blocks_resolution')
            ready,mails,batch=self.load_ready()
            matching=[m for m in mails if self.closure.mailbox(m['to'])==address]
            if len(matching)!=1:raise Stop('resolve_requires_exactly_one_prepared_recipient')
            mail=matching[0]
            segment=next(s for s in self.segments(mails,batch)if s['liste']==mail['liste'])
            journal=segment['journal']
            events=self.closure.read_journal(journal)if journal.exists()else[]
            state=self.closure.validate(segment['mails'],segment['batch'],events,len(segment['mails']),segment['liste'],details=True)
            if address not in state['uncertain']:raise Stop('recipient_is_not_uncertain')
            attempt=self._attempt_record(events,address)
            journal_acks=[e for e in events if e.get('event')=='send_ack'and self.closure.mailbox(e.get('email'))==address]
            central=self.central.states().get((self.run_id,address))
            if central is None or central['kind']not in ('attempt','send_ack','sent_readback'):
                raise Stop('central_state_not_resolvable')
            known_ids=[e['message_id']for e in journal_acks]
            known_ids+= [e['payload']['message_id']for e in self.central.events()
                         if e['run_id']==self.run_id and e['mailbox']==address
                         and e['kind']in ('send_ack','sent_readback') and e['payload'].get('message_id')]
            proof=self._sent_proof(mail,address)
            if proof is None:
                return {'state':'unresolved','email':address,'reason':'no_unique_sent_evidence',
                        'sent':0,'note':'attempt stays reserved; never retry blindly'}
            if any(known!=proof['message_id']for known in known_ids):
                raise Stop('recorded_ack_conflicts_with_sent_evidence')
            child=segment['batch']
            common={'schema_version':1,'batch_id':child['batch_id'],'batch_sha256':self.closure.fingerprint(child),
                    'page_id':self.closure.page_id(mail['page_id']),'email':address,
                    'content_sha256':self.closure.fingerprint(self.closure.content(mail)),
                    'attempt_id':attempt['attempt_id'],'request_sha256':fingerprint(self.request(mail))}
            def stamp(kind,**fields):
                return {**common,'at':datetime.now(timezone.utc).isoformat(),'event':kind,**fields}
            def central_args(payload):
                return dict(run_id=self.run_id,mailbox=mail['to'],org_domain=mail['org_domain'],
                            source_id=mail['page_id'],payload=payload)
            if not journal_acks and central['kind']=='attempt':
                # True lost ACK: the API outcome never reached either journal.
                ack=stamp('send_ack_recovered',message_id=proof['message_id'],sender=proof['sender'],
                          to=proof['to'],subject=proof['subject'],labels=proof['labels'],
                          unique_match_count=1,evidence_source='gmail_sent_read_only',recovery_reason='lost_ack')
                self.central.append('send_ack',**central_args(ack))
                self.sender.append_event(journal,ack)
            elif not journal_acks:
                # Central recorded the ACK; the crash hit before the run journal.
                ack=stamp('send_ack',message_id=proof['message_id'])
                self.sender.append_event(journal,ack)
            readback=stamp('sent_readback',message_id=proof['message_id'],sender=proof['sender'],
                           to=proof['to'],subject=proof['subject'],labels=proof['labels'])
            if central['kind']!='sent_readback':
                self.central.append('sent_readback',**central_args(readback))
            if not any(e.get('event')=='sent_readback'and self.closure.mailbox(e.get('email'))==address for e in events):
                self.sender.append_event(journal,readback)
            after=self.closure.validate(segment['mails'],segment['batch'],self.closure.read_journal(journal),
                                        len(segment['mails']),segment['liste'],details=True)
            if address not in after['confirmed']:raise Stop('resolution_did_not_confirm')
            if self.central.states()[(self.run_id,address)]['kind']!='sent_readback':
                raise Stop('central_resolution_did_not_confirm')
            return {'state':'resolved','email':address,'message_id':proof['message_id'],'sent':0,
                    'note':'send evidence recovered read-only; CRM completion remains a separate step'}

    def complete_open(self):
        """Run the existing fail-closed CRM completion for proven-sent mails whose
        CRM commit is still open. Never sends. Refuses while any send attempt is
        unresolved or any halt/bounce stands; unclear recipients stay untouched.
        """
        with self.central.lock():
            ready,mails,batch=self.load_ready()
            status,events=self.statuses(mails,batch)
            if status['uncertain']:raise Stop('resolve_uncertain_sends_first')
            if any(e['run_id']==self.run_id and e['kind']in('bounce','run_halted')for e in self.central.events()):
                raise Stop('halted_run_not_completable')
            if self.bounce_check is not None and events:
                bounce=self.bounce_check(events)
                if bounce.get('confirmed')or bounce.get('uncertain'):
                    raise Stop('bounce_or_uncertain_delivery_report')
            states=self.central.states();completed=[]
            for mail in mails:
                address=mail['to'].strip().casefold()
                if address not in status['confirmed']:continue
                state=states.get((self.run_id,address))
                if state and state['kind']=='crm_committed':continue
                segment=next(s for s in self.segments(mails,batch)if s['liste']==mail['liste'])
                self.scope(segment['liste'])
                self.v.MAILS=str(segment['folder']/'mails.json');self.v.BATCH=str(segment['folder']/'batch.json')
                self.central.append('crm_attempt',run_id=self.run_id,mailbox=mail['to'],
                                    org_domain=mail['org_domain'],source_id=mail['page_id'])
                result=self.closure.complete(self.v,segment['journal'],len(segment['mails']),probe=False)
                if address not in set(result['confirmed'])|set(result['already_committed']):
                    raise Stop('CRM_completion_missing')
                self.central.append('crm_readback',run_id=self.run_id,mailbox=mail['to'],
                                    org_domain=mail['org_domain'],source_id=mail['page_id'])
                self.central.append('crm_committed',run_id=self.run_id,mailbox=mail['to'],
                                    org_domain=mail['org_domain'],source_id=mail['page_id'])
                completed.append(address)
            return {'state':'open_confirmations_completed','completed':completed,'sent':0}
