import base64
import json
import multiprocessing
import tempfile
import unittest
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch, Mock
import mail_policy as p
import email_watcher as w
import email_realtime as r
import draft_batch as d

SLOT='2026-10-12T13:00:00+02:00'
BODY='Hallo,\n\nPasst Montag, 12.10.2026, 13:00 Uhr für unser Gespräch? — Danke.\n\nBeste Grüße\n\nOle'
class Missing(Exception):
    resp=type('Response',(),{'status':404})()
class Request:
    def __init__(self, fn): self.fn=fn
    def execute(self): return self.fn()
class Calendar:
    def __init__(self, root, error=None): self.root=Path(root); self.error=error; self.inserts=0
    def events(self): return self
    def calendarList(self): return self
    def freebusy(self): return self
    def get(self, **kw):
        def run():
            path=self.root/(kw['eventId']+'.event')
            if not path.exists(): raise Missing()
            event=json.loads(path.read_text())
            if self.error=='readback': event['visibility']='public'
            return event
        return Request(run)
    def list(self, **kw):
        if 'calendarId' in kw:
            return Request(lambda: {'items':[json.loads(f.read_text()) for f in self.root.glob('*.event')]})
        return Request(lambda: {'items':[{'id':p.PRIMARY,'primary':True},{'id':'fuxam','summary':'Fuxam Calendar'}]})
    def query(self, **kw):
        if self.error=='calendar': return Request(lambda: {'calendars':{}})
        return Request(lambda: {'calendars':{i['id']:{'busy':[]} for i in kw['body']['items']}})
    def insert(self, **kw):
        def run():
            self.inserts+=1
            assert kw['sendUpdates']=='none' and 'attendees' not in kw['body']
            (self.root/(kw['body']['id']+'.event')).write_text(json.dumps(kw['body']))
            if self.error=='uncertain': raise TimeoutError()
            return kw['body']
        return Request(run)
class Gmail:
    def __init__(self, root, recipient='a@example.invalid', thread='t1', error=None):
        self.root=Path(root); self.recipient=recipient; self.thread=thread; self.error=error; self.created=None
    def users(self): return self
    def messages(self): return self
    def threads(self): return self
    def drafts(self): return self
    def getProfile(self, **kw): return Request(lambda:{'emailAddress':p.PRIMARY})
    def get(self, **kw):
        if kw.get('format')=='full': return Request(lambda:self.created)
        if kw.get('metadataHeaders')==w.HEADERS:
            return Request(lambda:{'threadId':self.thread,'payload':{'headers':[{'name':'From','value':self.recipient},{'name':'Subject','value':'Termin'}]}})
        return Request(lambda:{'messages':[]})
    def create(self, **kw):
        def run():
            with (self.root/'gmail-writes').open('a') as f: f.write(self.recipient+'\n')
            if self.error=='gmail': raise TimeoutError('uncertain Gmail write')
            msg=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(kw['body']['message']['raw']))
            text=msg.get_content()
            self.created={'message':{'threadId':self.thread,'labelIds':['DRAFT'],'payload':{'mimeType':'text/plain','headers':[{'name':k,'value':v} for k,v in msg.items()], 'body':{'data':base64.urlsafe_b64encode(text.encode()).decode()}}}}
            return {'id':'draft1'}
        return Request(run)

def boundary(root, recipient='a@example.invalid', thread='t1', error=None, slot=SLOT):
    p.STATE_DIR=Path(root)/'claims'
    gmail=Gmail(root,recipient,thread,error)
    calendar=Calendar(root,error)
    with patch.object(w,'service',return_value=gmail), patch.object(r,'calendar_service',return_value=calendar):
        result=w.make_draft('code','incoming',BODY,slots=[slot])
    return result,calendar,gmail

def competitor(root, recipient, queue):
    try: queue.put(('ok',boundary(root,recipient,recipient)[0]['status']))
    except Exception as e: queue.put(('failed',str(e)))

class ManagedMailTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name); self.old=p.STATE_DIR; p.STATE_DIR=self.root/'claims'
    def tearDown(self): p.STATE_DIR=self.old; self.temp.cleanup()
    def test_real_boundary_reserves_then_creates_formatted_draft(self):
        result,cal,mail=boundary(self.root)
        self.assertEqual(result['status'],'created'); self.assertEqual(cal.inserts,1)
        event=json.loads(next(self.root.glob('*.event')).read_text())
        self.assertEqual(p.start(event['end']['dateTime'])-p.start(event['start']['dateTime']),p.timedelta(minutes=30))
        text=w.body(mail.created['message']['payload'])
        self.assertIn('Beste Grüße\nOle',text); self.assertNotIn('Beste Grüße\n\nOle',text); self.assertNotIn('—',text)
        self.assertIn('13:00 Uhr',text); self.assertNotIn('13:15',text)
    def test_parallel_workers_at_most_one_gmail_write(self):
        ctx=multiprocessing.get_context('fork'); q=ctx.Queue()
        workers=[ctx.Process(target=competitor,args=(str(self.root),f'{i}@example.invalid',q)) for i in range(2)]
        for proc in workers: proc.start()
        for proc in workers: proc.join(10); self.assertEqual(proc.exitcode,0)
        outcomes=[q.get(timeout=2)[0] for _ in workers]
        self.assertEqual(outcomes.count('ok'),1)
        self.assertEqual(len((self.root/'gmail-writes').read_text().splitlines()),1)
    def test_distinct_thread_or_recipient_cannot_reuse(self):
        boundary(self.root)
        for recipient,thread in [('b@example.invalid','t1'),('a@example.invalid','t2')]:
            with self.assertRaises(w.SchedulingDraftReviewRequired): boundary(self.root,recipient,thread)
    def test_timezone_equivalent_same_owner_reuses_exact_event(self):
        boundary(self.root)
        _,cal,_=boundary(self.root,slot='2026-10-12T11:00:00+00:00')
        self.assertEqual(cal.inserts,0); self.assertEqual(len(list(self.root.glob('*.event'))),1)
    def test_calendar_error_prevents_draft(self):
        with self.assertRaises(w.SchedulingDraftReviewRequired): boundary(self.root,error='calendar')
        self.assertFalse((self.root/'gmail-writes').exists())
    def test_bad_readback_prevents_draft_retains_claim(self):
        with self.assertRaises(w.SchedulingDraftReviewRequired): boundary(self.root,error='readback')
        self.assertFalse((self.root/'gmail-writes').exists()); self.assertTrue(p._read())
    def test_uncertain_insert_recovers_exact_id_without_duplicate(self):
        _,cal,_=boundary(self.root,error='uncertain')
        self.assertEqual(cal.inserts,1); self.assertEqual(len(list(self.root.glob('*.event'))),1)
    def test_gmail_failure_keeps_hold_and_tracked_orphan(self):
        with self.assertRaises(w.DraftStateUnverified): boundary(self.root,error='gmail')
        self.assertEqual(next(iter(p._read().values()))['state'],'held_draft_unverified')
        self.assertEqual(len(list(self.root.glob('*.event'))),1)
    def test_uncertain_gmail_write_cannot_blindly_create_again(self):
        with self.assertRaises(w.DraftStateUnverified): boundary(self.root,error='gmail')
        with self.assertRaises(w.SchedulingDraftReviewRequired): boundary(self.root)
        self.assertEqual(len((self.root/'gmail-writes').read_text().splitlines()),1)
    def test_missing_pending_event_no_blind_retry(self):
        boundary(self.root)
        next(self.root.glob('*.event')).unlink()
        with self.assertRaises(w.SchedulingDraftReviewRequired): boundary(self.root)
        self.assertFalse(list(self.root.glob('*.event')))
    def test_existing_hold_cannot_ignore_new_primary_overlap(self):
        boundary(self.root)
        (self.root/'other.event').write_text(json.dumps({'id':'other'}))
        with self.assertRaises(w.SchedulingDraftReviewRequired): boundary(self.root)
    def test_pending_claims_appear_in_candidate_busy(self):
        boundary(self.root,error='uncertain')
        self.assertEqual(len(p.pending_busy()),1)
    def test_manual_unverified_time_refused_before_gmail(self):
        api=Gmail(self.root)
        with patch.object(w,'service',return_value=api):
            with self.assertRaises(w.SchedulingDraftReviewRequired): w.make_draft('code','incoming','Morgen 13:00 Uhr passt.')
        self.assertFalse((self.root/'gmail-writes').exists())
    def test_range_and_wrong_date_fail_closed(self):
        for text in [BODY.replace('13:00 Uhr','13:00 bis 13:15 Uhr'), BODY.replace('12.10.2026','13.10.2026'), BODY+'\nDienstag passt auch für den Call']:
            with self.assertRaises(ValueError): p.format_body(text,[SLOT])
    def test_lf_crlf_signature_and_exact_history_preservation(self):
        quote='On Monday wrote:\r\n> Hallo — Grüße\r\n'
        for newline in ['\n','\r\n']:
            text=p.format_body(('Hallo – du'+newline+newline+'Viele Grüße'+newline+newline+'Alex'+newline+newline)+quote)
            self.assertIn('Beste Grüße\nOle',text); self.assertTrue(text.endswith(quote)); self.assertNotIn('–',text.split('On Monday')[0])
    def test_actual_worker_passes_chosen_slot(self):
        thread=[{'text':'Hallo Alex, du kannst antworten','truncated':False}]
        answer={'body':BODY,'priority':'normal','incoming_summary':'Termin','company':None,'proposed_slots':[SLOT]}
        with patch.object(r,'get_thread',return_value=(thread,False)), patch.object(r,'draft_worthy',return_value=True), patch.object(r,'generate',return_value=answer), patch.object(w,'make_draft',return_value={'status':'created'}) as create:
            r.draft_one({'account':'code','id':'m','threadId':'t'})
        self.assertEqual(create.call_args.kwargs['slots'],[SLOT])
    def test_multipart_both_bodies_keep_history_and_attachments(self):
        msg=EmailMessage();msg['To']='a@example.invalid';msg['Subject']='Re: Thema — Original'
        quote='On Monday wrote:\n> original — text\n'
        msg.set_content('old\n\n'+quote)
        html_quote='<blockquote>original — text</blockquote></body></html>\n'
        msg.add_alternative('<html><body>old'+html_quote,subtype='html')
        msg.add_attachment(b'abc',maintype='application',subtype='octet-stream',filename='x.bin')
        draft={'message':{'threadId':'t','labelIds':['DRAFT'],'raw':base64.urlsafe_b64encode(msg.as_bytes()).decode()}}
        request,plain,html_body,preserved=d.stage(draft,'Hallo – du\r\n\r\nBeste Grüße\r\n\r\nOle',d.digest(draft))
        saved=d.mime({'message':request['message']})
        self.assertEqual(d.protected(saved),preserved);self.assertTrue(plain.endswith(quote));self.assertTrue(html_body.endswith(html_quote))
        self.assertIn('Beste Grüße<br>\nOle',html_body)
    def test_batch_write_boundary_reserves_and_formats(self):
        msg=EmailMessage();msg['To']='a@example.invalid';msg.set_content('old')
        original={'message':{'threadId':'t1','labelIds':['DRAFT'],'raw':base64.urlsafe_b64encode(msg.as_bytes()).decode()}}
        api=Mock(); saved={}; batch=Mock()
        api.new_batch_http_request.return_value=batch
        def add(request, request_id, callback):
            saved[request_id]={'message':{**request['message'],'labelIds':['DRAFT']}}
        batch.add.side_effect=add
        api.users.return_value.drafts.return_value.update.side_effect=lambda **kw:kw['body']
        bodyfile=self.root/'body.txt';bodyfile.write_text(BODY)
        with patch.object(d,'read_many',side_effect=lambda api,ids:{'d':saved.get('d',original)}):
            result=d.apply(api,[('d',str(bodyfile),d.digest(original))],account='code',slots={'d':[SLOT]},calendar=Calendar(self.root))
        self.assertTrue(result['verified']);self.assertEqual(len(list(self.root.glob('*.event'))),1)
        self.assertIn('Beste Grüße\nOle',d.mime(saved['d']).get_content())
    def test_same_recipient_incoming_acceptance_reuses_own_hold_readonly(self):
        boundary(self.root)
        cal=Calendar(self.root)
        with patch.object(r,'calendar_service',return_value=cal):
            self.assertTrue(r.check_incoming_availability(p.start(SLOT),owner=('code','t1',['a@example.invalid'])))
            self.assertFalse(r.check_incoming_availability(p.start(SLOT),owner=('code','t2',['b@example.invalid'])))
        self.assertEqual(cal.inserts,0)
    def test_generate_accepts_same_owner_held_offer(self):
        from types import SimpleNamespace
        boundary(self.root)
        answer={'body':'{{SLOT}} passt mir gut.', 'priority':'normal','incoming_summary':'Termin','company':None,'proposed_slot':SLOT,'referral_recipients':[]}
        with patch.object(r,'calendar_options',return_value=[]), patch.object(r,'calendar_service',return_value=Calendar(self.root)), patch.object(r.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(answer),stderr='')):
            result=r.generate('code','m',[{'text':'12. Oktober 13:00 Uhr wäre passend.'}],False,now=datetime.fromisoformat('2026-10-06T14:00:00+02:00'),scheduling_owner=('code','t1',['a@example.invalid']))
        self.assertEqual(result['proposed_slots'],[SLOT]);self.assertIn('13:00 Uhr passt mir gut',result['body'])
    def test_incoming_own_hold_still_rejects_new_other_overlap(self):
        boundary(self.root)
        (self.root/'other.event').write_text(json.dumps({'id':'other'}))
        with patch.object(r,'calendar_service',return_value=Calendar(self.root)):
            with self.assertRaisesRegex(ValueError,'another primary'):
                r.check_incoming_availability(p.start(SLOT),owner=('code','t1',['a@example.invalid']))
    def test_worker_boundary_failures_return_manual_alert_without_retry(self):
        thread=[{'text':'Hallo Alex, du kannst antworten','truncated':False}]
        answer={'body':BODY,'priority':'normal','incoming_summary':'Termin','company':None,'proposed_slots':[SLOT]}
        item={'account':'code','id':'m','threadId':'t','from':'Person <a@example.invalid>'}
        for error,status in [(w.SchedulingDraftReviewRequired('conflict'),'needs_manual_review_scheduling'),(w.DraftStateUnverified('uncertain'),'needs_manual_review_draft_state')]:
            with patch.object(r,'get_thread',return_value=(thread,False)), patch.object(r,'draft_worthy',return_value=True), patch.object(r,'generate',return_value=answer), patch.object(w,'make_draft',side_effect=error) as create:
                outcome=r.draft_one(item)
            self.assertEqual(outcome['status'],status);create.assert_called_once()
            state={};r.queue_draft_notification(state,item,outcome)
            self.assertEqual(len(state['notifications']),1)
            if status=='needs_manual_review_draft_state':
                self.assertIn('kann bereits vorhanden sein',state['notifications'][0]);self.assertNotIn('kein Entwurf erstellt',state['notifications'][0])
    def test_overlapping_different_start_cannot_be_reserved(self):
        boundary(self.root)
        cal=Calendar(self.root)
        with self.assertRaisesRegex(ValueError,'conflicts'):
            with p.scheduling(cal,'code','t2',['b@example.invalid'],['2026-10-12T13:15:00+02:00']): pass
        self.assertEqual(cal.inserts,0)
    def test_gmail_readback_extra_content_is_not_verified(self):
        api=Gmail(self.root)
        original_create=api.create
        def create(**kw):
            request=original_create(**kw)
            def execute():
                result=request.execute()
                content=api.created['message']['payload']['body']
                content['data']=base64.urlsafe_b64encode((base64.urlsafe_b64decode(content['data']).decode()+'\nExtra text').encode()).decode()
                return result
            return Request(execute)
        api.create=create
        with patch.object(w,'service',return_value=api), patch.object(r,'calendar_service',return_value=Calendar(self.root)):
            with self.assertRaisesRegex(RuntimeError,'readback'): w.make_draft('code','m',BODY,slots=[SLOT])
        self.assertEqual(next(iter(p._read().values()))['state'],'held_draft_unverified')
    def test_ordinary_dated_business_fact_is_allowed(self):
        text=p.format_body('Die Rechnung wurde am 12.10.2026 erstellt. Die Zahlungsfrist ist der 20.10.2026.')
        self.assertIn('12.10.2026',text)
    def test_nonstandard_times_cannot_bypass_metadata(self):
        for value in ['13h','1pm','13：00','13.00 Uhr','um 13','at 1']:
            with self.assertRaisesRegex(ValueError,'Unverified'): p.format_body('Passt der Call '+value+'?')
    def test_batch_mismatched_readback_retains_unverified_state(self):
        msg=EmailMessage();msg['To']='a@example.invalid';msg.set_content('old')
        original={'message':{'threadId':'t1','labelIds':['DRAFT'],'raw':base64.urlsafe_b64encode(msg.as_bytes()).decode()}}
        api=Mock();bodyfile=self.root/'body.txt';bodyfile.write_text(BODY)
        with patch.object(d,'read_many',return_value={'d':original}):
            result=d.apply(api,[('d',str(bodyfile),d.digest(original))],account='code',slots={'d':[SLOT]},calendar=Calendar(self.root))
        self.assertFalse(result['verified'])
        self.assertEqual(next(iter(p._read().values()))['state'],'held_draft_unverified')
    def test_generator_returns_structured_chosen_start(self):
        from types import SimpleNamespace
        answer={'body':'Passt {{SLOT}}?', 'priority':'normal','incoming_summary':'Termin','company':None,'proposed_slot':SLOT,'referral_recipients':[]}
        with patch.object(r,'calendar_options',return_value=[{'start':SLOT,'label':p.label(SLOT)}]), patch.object(r.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(answer),stderr='')):
            result=r.generate('code','m',[{'text':'Welcher Termin passt?'}],False)
        self.assertEqual(result['proposed_slots'],[SLOT]);self.assertIn(p.label(SLOT),result['body'])
    def test_outreach_creation_policy_rejects_scheduling(self):
        import outreach_draft_batch as outreach
        self.assertIs(outreach.canonical_draft_signature,w.canonical_draft_signature)
        with self.assertRaisesRegex(ValueError,'Unverified'):
            outreach.canonical_draft_signature('Passt Montag um 13h für den Call?',force_formal=True)
        self.assertEqual(outreach.canonical_draft_signature('Hallo — Danke.\n\nViele Grüße\n\nOle',force_formal=True),'Hallo - Danke.\n\nBeste Grüße\nOle Beispiel')
    def test_two_scheduling_batch_owners_no_nested_lock_deadlock(self):
        with p.scheduling_batch(Calendar(self.root),[('code','t1',['a@example.invalid'],[SLOT]),('code','t2',['b@example.invalid'],['2026-10-12T15:00:00+02:00'])]): pass
        self.assertEqual(len(p._read()),2)
        self.assertTrue(all(c['state']=='held_draft_verified' for c in p._read().values()))

if __name__=='__main__': unittest.main()
