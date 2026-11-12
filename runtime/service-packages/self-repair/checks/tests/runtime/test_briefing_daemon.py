import base64
import concurrent.futures
import datetime as dt
import json
import io
import os
import sys
import socket
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from gtm.briefing import due_briefing
from gtm import briefing_daemon as b

NOW = dt.datetime(2026, 10, 12, 10, 0, tzinfo=dt.timezone.utc)

def event(**change):
    result = dict(id='fictional-event', etag='v1', summary=b.ACRIBIC_TITLE, organizer={'email': b.CODE},
                  start={'dateTime': (NOW + dt.timedelta(minutes=15)).isoformat()},
                  attendees=[{'email': b.CODE}, {'email': b.PIET}, {'email': 'external@example.org'}])
    result.update(change)
    return result

def context(e):
    quote = 'Thomas Beispiel bei Example GmbH, Geschäftsführer. Sie: Interesse an Ausschreibungen.'
    return dict(calendar=e, messages=[dict(message_id='m1', internal_date='100', original=quote, labels=['SENT'], account=b.CODE, author=b.CODE, recipients=['external@example.org'], headers={'from':b.CODE,'to':'external@example.org'})])

def answer():
    reference = dict(message_id='m1', quote=context(event())['messages'][0]['original'])
    return json.dumps(dict(abstain=False, person='Thomas Beispiel', person_email='external@example.org', company='Example GmbH', role='Geschäftsführer',
                           company_context='In der Mail wird Example GmbH genannt.', interest=None, conversation_context='Ausschreibungen',
                           questions=['Wie bearbeiten Sie Ausschreibungen?'], addressing='Sie', room=None,
                           evidence={k: reference for k in ['person','company','role','company_context','conversation_context','addressing']}))

class Reads:
    def __init__(self, items=None): self.items = [event()] if items is None else items; self.fetches=0
    def events(self, now): return self.items
    def current_event(self, id): return next(e for e in self.items if e['id']==id)
    def context(self, e, category): self.fetches+=1; return context(e)

class FakeRuntime:
    def __init__(self, store): self.store=store; self.calls=0; self.sends=0
    def model(self, ctx): self.calls+=1; return answer()
    def send(self, identity, message):
        self.sends+=1
        self.store.step(identity,'delivered',receipt='fictional-receipt')
        return self.store.get(identity)


PHONE_THREAD = 'e000000000000011'

def phone_event(**change):
    return event(summary='ANRUFEN: Example - Thomas Beispiel', attendees=[],
                 _hermes_calendar_id=b.ACRIBIC,
                 extendedProperties={'private': {'acribic_thread_id': PHONE_THREAD}}, **change)


def phone_messages():
    def item(mid, author, recipient, stamp, text, labels=(), parent=None):
        headers = {'from': author, 'to': recipient, 'message-id': '<' + mid + '@example.org>'}
        if parent: headers['in-reply-to'] = '<' + parent + '@example.org>'
        return dict(message_id=mid, thread_id=PHONE_THREAD, account=b.CODE,
                    internal_date=str(stamp), labels=list(labels), author=author,
                    recipients=[recipient], original=text, headers=headers, calendar_notification=False)
    return [item('outreach', b.CODE, 'external@example.org', 100,
                 'Ich studiere an der CODE University. Ich möchte TED und öffentliche Ausschreibungen verstehen. Sie sind bei Example GmbH.', ['SENT']),
            item('incoming', 'external@example.org', b.CODE, 200,
                 'Thomas Beispiel bei Example GmbH. Rufen Sie mich morgen um 15 Uhr an.', parent='outreach'),
            item('confirmation', b.CODE, 'external@example.org', 300,
                 'Gerne rufe ich Sie an.', ['SENT'], parent='incoming')]


class PhoneBriefingTests(unittest.TestCase):
    def test_phone_requires_explicit_planning_and_owned_exact_source(self):
        self.assertEqual(b.classify(phone_event())[0], 'acribic')
        self.assertEqual(b.verified_phone_contacts(phone_event(), phone_messages()), ['external@example.org'])
        for changes in [dict(summary='[Acribic] Terminvorschlag - Thomas Beispiel - Example'),
                        dict(extendedProperties={}), dict(_hermes_calendar_id=b.CODE),
                        dict(organizer={'email':'stranger@example.org'}), dict(status='cancelled'),
                        dict(extendedProperties={'private':{'acribic_thread_id':PHONE_THREAD,
                             'hermesOwner':json.dumps(['code','e000000000000012',['external@example.org']])}})]:
            candidate=phone_event();candidate.update(changes)
            self.assertIsNone(b.classify(candidate)[0])

    def test_phone_contact_cannot_be_forged_by_title_draft_or_quoted_outreach(self):
        candidate=phone_event()
        for kind in ('draft_only','foreign_thread','quoted_outreach','foreign_contact','no_incoming'):
            messages=phone_messages()
            if kind=='draft_only':messages[1]['labels']=['DRAFT']
            elif kind=='foreign_thread':messages[1]['thread_id']='e000000000000012'
            elif kind=='quoted_outreach':messages[0]['original']='Hello\n> CODE University TED Ausschreibungen'
            elif kind=='foreign_contact':messages[1]['headers']['to']='stranger@example.org'
            else:messages=[messages[0]]
            with self.subTest(kind=kind), self.assertRaises(b.ReviewRequired):
                b.verified_phone_contacts(candidate,messages)
        candidate['extendedProperties']['private']['hermesOwner']=json.dumps(['code',PHONE_THREAD,['someone@example.org']])
        with self.assertRaisesRegex(b.ReviewRequired,'phone_contact_source_mismatch'):
            b.verified_phone_contacts(candidate,phone_messages())

    def test_phone_private_forward_keeps_references_and_ignores_later_sent_and_draft(self):
        messages=phone_messages()
        messages[1]['headers']['in-reply-to']='<private-forward@example.org>'
        messages[1]['headers']['references']='<outreach@example.org> <private-forward@example.org>'
        messages.append(dict(messages[-1],message_id='draft',labels=['DRAFT'],author='invented@example.org'))
        self.assertEqual(b.verified_phone_contacts(phone_event(),messages),['external@example.org'])

    def test_exact_phone_thread_adapter_does_not_search_mailbox_or_mutate_attendees(self):
        from types import SimpleNamespace
        messages=phone_messages()
        raw={'id':PHONE_THREAD,'messages':[dict(id=m['message_id'], internalDate=m['internal_date'],
             labelIds=m['labels'],payload={'mimeType':'text/plain',
             'headers':[{'name':k,'value':v}for k,v in m['headers'].items()],
             'body':{'data':base64.urlsafe_b64encode(m['original'].encode()).decode()}})for m in messages]}
        gets=[]
        class Threads:
            def list(self,**kwargs):raise AssertionError('exact phone source requires no mailbox search')
            def get(self,**kwargs):gets.append(kwargs);return SimpleNamespace(execute=lambda:raw)
        reads=b.ExistingReads.__new__(b.ExistingReads)
        reads.mail_module=SimpleNamespace(service=lambda alias:SimpleNamespace(users=lambda:SimpleNamespace(threads=lambda:Threads())),
                                          verified_profile=lambda svc:b.CODE)
        reads.crm_reader=None
        candidate=phone_event()
        result=reads.context(candidate,'acribic',enrich_sources=False)
        self.assertEqual([r['id']for r in gets],[PHONE_THREAD])
        self.assertEqual(candidate['attendees'],[])
        self.assertEqual(result['phone_plan']['verified_contacts'],['external@example.org'])
        self.assertIs(result['phone_plan']['invitation_sent'],False)
        self.assertEqual(result['addressing_by_participant']['external@example.org']['addressing'],'Sie')

    def test_phone_output_is_bound_to_original_contact_and_actual_calendar_time(self):
        candidate=phone_event()
        candidate['summary']='ANRUFEN - 09:00 Uhr'
        ctx=context(candidate);ctx['messages']=phone_messages()
        ref={'message_id':'incoming','quote':ctx['messages'][1]['original']}
        value=json.loads(answer());value['role']=None
        value['evidence']={k:ref for k in ('person','company','company_context','conversation_context')}
        value['evidence']['addressing']={'message_id':'confirmation','quote':'Gerne rufe ich Sie an.'}
        text=b.validate_output(json.dumps(value),ctx,starts_at=NOW)
        self.assertIn('12:00 Uhr',text)
        self.assertNotIn('09:00 Uhr',text)
        value['person_email']='forged@example.org'
        with self.assertRaises(b.ReviewRequired):b.validate_output(json.dumps(value),ctx,starts_at=NOW)

    def test_bare_holds_do_not_spawn_external_business_lookup(self):
        reads=b.ExistingReads.__new__(b.ExistingReads)
        with patch.object(b.subprocess,'run',side_effect=AssertionError('unnecessary external process')):
            self.assertIsNone(reads.business_mapping(event(attendees=[],summary='Terminvorschlag')))

    def test_reversed_outlook_display_name_is_accepted_only_for_exact_bound_person(self):
        ctx=context(event());raw=json.loads(answer())
        header='"Beispiel, Thomas" <external@example.org>'
        ctx['messages'][0]['headers']['to']=header
        raw['evidence']['person']={'message_id':'m1','header':'to','quote':header}
        self.assertIn('Thomas Beispiel',b.validate_output(json.dumps(raw),ctx,starts_at=NOW))
        for unsupported in ('Thomas Karl Beispiel','Beispiel Thomas','Someone Else'):
            raw['person']=unsupported
            with self.subTest(name=unsupported),self.assertRaisesRegex(b.ReviewRequired,'named_fact_not_in_original'):
                b.validate_output(json.dumps(raw),ctx,starts_at=NOW)

    def test_mentioned_colleague_cannot_replace_actual_phone_sender(self):
        messages=phone_messages()
        messages[1]['headers']['from']='Alex Sender <external@example.org>'
        messages[1]['original']='Ich bin Alex Sender bei Example GmbH. Meine Kollegin Thomas Beispiel kennt Ausschreibungen. Rufen Sie mich an.'
        ctx=context(phone_event());ctx['messages']=messages
        raw=json.loads(answer());raw['role']=None
        ref={'message_id':'incoming','quote':messages[1]['original']}
        raw['evidence']={k:ref for k in ('person','company','company_context','conversation_context')}
        raw['evidence']['addressing']={'message_id':'confirmation','quote':'Gerne rufe ich Sie an.'}
        with self.assertRaisesRegex(b.ReviewRequired,'phone_person_name_mismatch'):
            b.validate_output(json.dumps(raw),ctx,starts_at=NOW)
        raw['person']='Alex Sender'
        self.assertIn('Alex Sender',b.validate_output(json.dumps(raw),ctx,starts_at=NOW))

    def test_optional_translated_role_is_omitted_without_losing_verified_briefing(self):
        ctx=context(event());ctx['messages'][0]['original']+=' Sales Department'
        raw=json.loads(answer());raw['role']='Vertrieb (Sales Department)'
        raw['evidence']['role']={'message_id':'m1','quote':'Sales Department'}
        text=b.validate_output(json.dumps(raw),ctx,starts_at=NOW)
        self.assertIn('Thomas Beispiel bei Example GmbH',text)
        self.assertNotIn('Vertrieb',text)

class BriefingDaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=b.Store(Path(self.tmp.name)/'state')
    def tearDown(self): self.tmp.cleanup()
    def tick(self, reads, runtime, **opts):
        with patch.object(b.dt, 'datetime', wraps=dt.datetime) as clock:
            clock.now.return_value=NOW
            return b.tick(reads,self.store,runtime,now=NOW,**opts)

    def test_private_unmapped_stealth_and_wrong_owner_never_processed(self):
        self.assertEqual(b.classify(event(visibility='private'))[0],'acribic')
        self.assertIsNone(b.classify(event(visibility='private',summary='Private personal meeting'))[0])
        for e in [event(summary='Stealth meeting'),event(organizer={'email':'other@example.org'}),
                  event(summary=b.ACRIBIC_TITLE+' - private Notiz')]:
            self.assertIsNone(b.classify(e)[0])
        tagged=event(summary='Scoped Stealth',extendedProperties={'private':{'ole_gtm_project':'stealth'}})
        self.assertIsNone(b.classify(tagged)[0]);self.assertEqual(b.classify(tagged,trusted_tags=True)[0],'stealth')

    def test_own_acribic_calls_do_not_require_piet_or_one_exact_title_variant(self):
        for title in [b.ACRIBIC_TITLE, 'Austausch öffentliche Ausschreibungen',
                      '  AUSTAUSCH   ÖFFENTLICHE AUSSCHREIBUNGEN  ']:
            candidate=event(summary=title,attendees=[{'email':b.CODE},{'email':'external@example.org'}])
            self.assertEqual(b.classify(candidate)[0],'acribic')
        candidate=event(summary='Austausch öffentliche Ausschreibungen',
                        attendees=[{'email':'external@example.org'}])
        self.assertEqual(b.classify(candidate)[0],'acribic')
        candidate['organizer']={'email':'other@example.org'}
        self.assertIsNone(b.classify(candidate)[0])

    def test_empty_or_unchanged_calendar_zero_model(self):
        run=FakeRuntime(self.store)
        self.tick(Reads([]),run,activate=True)
        future=event(start={'dateTime':(NOW+dt.timedelta(minutes=16)).isoformat()})
        self.tick(Reads([future]),run,activate=True)
        self.assertEqual(run.calls,0);self.assertEqual(run.sends,0)

    def test_default_dryrun_no_claim_read_context_model_or_send(self):
        reads=Reads();run=FakeRuntime(self.store);report=self.tick(reads,run)
        self.assertTrue(report['dry_run']);self.assertEqual(reads.fetches,0);self.assertEqual(run.calls,0)
        self.assertIsNone(self.store.get(due_briefing(event(),calendar_id=b.CODE,category='acribic',now=NOW).identity))

    def test_one_claim_one_job_delivery_and_no_repeat(self):
        reads=Reads();run=FakeRuntime(self.store)
        first=self.tick(reads,run,activate=True);second=self.tick(reads,run,activate=True)
        self.assertEqual(first['delivered'],1);self.assertEqual(second['claimed'],0)
        self.assertEqual(run.calls,1);self.assertEqual(run.sends,1)

    def test_process_race_and_global_fifteen_minute_cap(self):
        due=due_briefing(event(),calendar_id=b.CODE,category='acribic',now=NOW)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.store.claim(due,clock=NOW.timestamp()),range(8)))
        self.assertEqual(sum(results),1)
        second=due_briefing(event(id='second'),calendar_id=b.CODE,category='acribic',now=NOW)
        third=due_briefing(event(id='third'),calendar_id=b.CODE,category='acribic',now=NOW)
        self.assertTrue(self.store.claim(second,clock=NOW.timestamp()));self.assertFalse(self.store.claim(third,clock=NOW.timestamp()))

    def test_attempt_persisted_before_model_and_unknown_never_replays(self):
        run=FakeRuntime(self.store)
        def lost(ctx):
            identity=due_briefing(event(),calendar_id=b.CODE,category='acribic',now=NOW).identity
            self.assertEqual(self.store.get(identity)['model_attempted'],1)
            run.calls+=1;raise TimeoutError('lost worker')
        run.model=lost
        self.tick(Reads(),run,activate=True);self.tick(Reads(),run,activate=True)
        self.assertEqual(run.calls,1);self.assertEqual(run.sends,1)
        identity=due_briefing(event(),calendar_id=b.CODE,category='acribic',now=NOW).identity
        self.assertEqual(self.store.get(identity)['delivery_kind'],'incomplete_notice')

    def test_missing_sources_notice_is_once_and_zero_model(self):
        reads=Reads();run=FakeRuntime(self.store);delivered=[]
        def missing(*args): raise b.ReviewRequired('missing_originals')
        reads.context=missing
        original_send=run.send
        def send(identity,message): delivered.append(message);return original_send(identity,message)
        run.send=send
        first=self.tick(reads,run,activate=True);self.tick(reads,run,activate=True)
        self.assertEqual(run.calls,0);self.assertEqual(run.sends,1)
        self.assertEqual(first['delivered'],0);self.assertEqual(first['incomplete_notices'],1)
        self.assertIn('**Belegt:**',delivered[0]);self.assertIn('**Offen:**',delivered[0])
        self.assertIn('konkretes Interesse und Zusagen sind daraus noch nicht sicher ableitbar',delivered[0])
        self.assertTrue(delivered[0].endswith('Sag mir Bescheid, wenn der Call durch ist.'))

    def test_source_failure_after_reschedule_never_sends_stale_notice(self):
        reads=Reads();run=FakeRuntime(self.store)
        def move(*args):
            reads.items=[event(start={'dateTime':(NOW+dt.timedelta(hours=1)).isoformat()})]
            raise b.ReviewRequired('changed')
        reads.context=move
        self.tick(reads,run,activate=True)
        self.assertEqual(run.calls,0);self.assertEqual(run.sends,0)

    def test_delivery_readback_recovers_stdout_receipt_without_resend(self):
        due=due_briefing(event(),calendar_id=b.CODE,category='acribic',now=NOW)
        self.store.claim(due);self.store.step(due.identity,'send_attempted',sending=True)
        path=self.store.root/(due.identity+'.send.json')
        path.write_text(json.dumps({'success':True,'message_id':'verified-self-message'}))
        self.assertEqual(self.store.recover_send(due.identity)['state'],'delivered')
        self.assertEqual(self.store.recover_send(due.identity)['receipt'],'verified-self-message')

    def test_missing_or_partial_delivery_receipt_is_unknown_no_replay(self):
        due=due_briefing(event(),calendar_id=b.CODE,category='acribic',now=NOW)
        self.store.claim(due);self.store.step(due.identity,'send_attempted',sending=True)
        self.assertEqual(self.store.recover_send(due.identity)['state'],'delivery_unknown')
        run=FakeRuntime(self.store);self.tick(Reads(),run,activate=True)
        self.assertEqual(run.calls,0);self.assertEqual(run.sends,0)

    def test_full_email_body_and_missing_attachment_abstain(self):
        original='Geschäftliche Originalzeile\n' * 2000
        payload={'mimeType':'text/plain','body':{'data':base64.urlsafe_b64encode(original.encode()).decode().rstrip('=')}}
        self.assertEqual(b.message_text(payload),original)
        with self.assertRaises(b.ReviewRequired):b.message_text({'mimeType':'text/plain','body':{'attachmentId':'not_read'}})

    def test_incomplete_calendar_pages_fail_before_any_jobs(self):
        with self.assertRaises(b.ReviewRequired):b.paginated(lambda _: {'items':[event()],'nextPageToken':'repeat'},item_key='items')
        with self.assertRaises(b.ReviewRequired):b.paginated(lambda _: {'items':'bad'},item_key='items')

    def test_hallucinated_named_fact_old_addressing_and_invented_room_refused(self):
        raw=json.loads(answer())
        raw['person']='Invented Person'
        with self.assertRaises(b.ReviewRequired):b.validate_output(json.dumps(raw),context(event()),starts_at=NOW)
        raw=json.loads(answer());raw['room']='invented'
        with self.assertRaises(b.ReviewRequired):b.validate_output(json.dumps(raw),context(event()),starts_at=NOW)
        ctx=context(event());ctx['messages'].append(dict(message_id='m2',internal_date='200',original='Du kannst mich kontaktieren.'))
        self.assertIn('Anrede: Sie',b.validate_output(answer(),ctx,starts_at=NOW))

    def test_first_call_without_declared_interest_has_evidenced_conversation_focus(self):
        ctx=context(event())
        quote='Ich möchte den Umgang mit Ausschreibungen verstehen.'
        ctx['messages'][0]['original']+=' '+quote
        raw=json.loads(answer());raw['interest']=None
        raw['conversation_context']='Alex möchte den Umgang mit Ausschreibungen verstehen.'
        raw['evidence'].pop('interest',None)
        raw['evidence']['conversation_context']={'message_id':'m1','quote':quote}
        result=b.validate_output(json.dumps(raw),ctx,starts_at=NOW)
        self.assertIn('Gesprächsfokus:',result)
        self.assertIn(raw['conversation_context'],result)
        self.assertNotIn('Konkretes Interesse:',result)
        raw['evidence']['conversation_context']['quote']='Nicht in den Originalen'
        with self.assertRaises(b.ReviewRequired):
            b.validate_output(json.dumps(raw),ctx,starts_at=NOW)

    def test_own_offer_is_not_counterparty_interest(self):
        ctx=context(event());raw=json.loads(answer())
        raw['interest']='Ausschreibungen'
        raw['evidence']['interest']=raw['evidence']['conversation_context']
        # Our own SENT mail can never evidence the counterparty's interest. The
        # claim is dropped with a visible caveat; the briefing continues on the
        # evidenced conversation focus instead of failing as a whole.
        result=b.validate_output(json.dumps(raw),ctx,starts_at=NOW)
        self.assertNotIn('Konkretes Interesse:',result)
        self.assertIn('Gesprächsfokus',result)
        self.assertIn('Das konkrete Interesse der Gegenpartei ist nicht belegt',result)
        raw['conversation_context']=None
        with self.assertRaises(b.ReviewRequired):
            b.validate_output(json.dumps(raw),ctx,starts_at=NOW)
        raw['conversation_context']='Ausschreibungen'
        quote='Wir interessieren uns für Ausschreibungen.'
        ctx['messages'].append(dict(message_id='incoming-interest',internal_date='200',
            original=quote,labels=['INBOX'],account=b.CODE,author='external@example.org',
            recipients=[b.CODE],headers={'from':'external@example.org','to':b.CODE}))
        raw['evidence']['interest']={'message_id':'incoming-interest','quote':quote}
        self.assertIn('Konkretes Interesse:',b.validate_output(json.dumps(raw),ctx,starts_at=NOW))

    def test_failure_retains_safe_cause_and_stage_separate_from_delivery_receipt(self):
        run=FakeRuntime(self.store)
        run.model=lambda _: '{"abstain":true,"reason":"only scheduling so far"}'
        result=self.tick(Reads(),run,activate=True)
        identity=due_briefing(event(),calendar_id=b.CODE,category='acribic',now=NOW).identity
        failure=json.loads((self.store.root/(identity+'.failure.json')).read_text())
        self.assertEqual((failure['stage'],failure['code'],failure['repair_state']),
                         ('validate','briefing_abstained','pending'))
        self.assertEqual(result['delivered'],0)
        self.assertEqual(result['incomplete_notices'],1)
        self.assertEqual((self.store.root/(identity+'.failure.json')).stat().st_mode & 0o777,0o600)
        other='b'*64
        self.store.failure(other,'read_sources',RuntimeError('token=secret, https://private.invalid'))
        data=(self.store.root/(other+'.failure.json')).read_text()
        self.assertNotIn('secret',data);self.assertNotIn('https://',data)

    def test_cli_delivery_requires_claim_and_uses_stdin_not_argv(self):
        runtime=b.Runtime(self.store,target=b.DESTINATION)
        due=due_briefing(event(),calendar_id=b.CODE,category='acribic',now=NOW)
        with self.assertRaises(b.ReviewRequired):runtime.send(due.identity,'PRIVATE_BODY')
        self.store.claim(due);self.store.step(due.identity,'send_attempted',sending=True)
        from types import SimpleNamespace
        def cli(args, **kwargs):
            self.assertNotIn('PRIVATE_BODY',args)
            self.assertEqual(kwargs['input'],'PRIVATE_BODY')
            kwargs['stdout'].write(json.dumps({'success':True,'message_id':'fixture-send'}))
            return SimpleNamespace(returncode=0)
        with patch.object(b.subprocess,'run',side_effect=cli):
            self.assertEqual(runtime.send(due.identity,'PRIVATE_BODY')['state'],'delivered')
        with self.assertRaises(b.ReviewRequired):runtime.send(due.identity,'PRIVATE_BODY')

    def test_live_read_adapter_full_threads_and_account_identity_with_fake_google(self):
        from types import SimpleNamespace
        full='Original body beyond a snippet. ' * 200
        payload={'mimeType':'text/plain','body':{'data':base64.urlsafe_b64encode(full.encode()).decode()},
                 'headers':[{'name':'From','value':'external@example.org'}]}
        request=lambda value:SimpleNamespace(execute=lambda:value)
        class Threads:
            def list(self, **kwargs):return request({'threads':[{'id':'thread-1'}]})
            def get(self, **kwargs):return request({'id':'thread-1','messages':[{'id':'m1','internalDate':'100','payload':payload}]})
        class Users:
            def getProfile(self, **kwargs):return request({'emailAddress':b.CODE})
            def threads(self):return Threads()
        reads=b.ExistingReads.__new__(b.ExistingReads)
        reads.mail_module=SimpleNamespace(service=lambda alias:SimpleNamespace(users=lambda:Users()))
        reads.crm_reader=None
        ctx=reads.context(event(),'acribic',enrich_sources=False)
        self.assertEqual(ctx['messages'][0]['original'],full)
        self.assertFalse(ctx['crm']['available'])
        class Wrong(Users):
            def getProfile(self, **kwargs):return request({'emailAddress':'wrong@example.org'})
        reads.mail_module=SimpleNamespace(service=lambda alias:SimpleNamespace(users=lambda:Wrong()))
        with self.assertRaises(b.ReviewRequired):reads.context(event(),'acribic',enrich_sources=False)

    def test_last_own_sent_du_wins_over_quoted_sie_draft_and_foreign_reply(self):
        ctx=context(event())
        ctx['messages'][0]['original']='Thomas Beispiel bei Example GmbH, Geschäftsführer. Du kannst mir Ausschreibungen zeigen.\nBeste Grüße\nDatenschutz: Sie können widersprechen.\n> Sie sind willkommen.'
        ctx['messages'].append(dict(message_id='draft',internal_date='400',original='Sie werden beraten.',labels=['DRAFT'],account=b.CODE,author=b.CODE,recipients=['external@example.org']))
        ctx['messages'].append(dict(message_id='incoming',internal_date='500',original='Sie erhalten unsere Antwort.',labels=['INBOX'],account=b.CODE,author='external@example.org',recipients=[b.CODE]))
        address=b.latest_own_addressing(ctx['messages'],'external@example.org')
        self.assertEqual(address['addressing'],'Du');self.assertEqual(address['message_id'],'m1')
        self.assertNotIn('Datenschutz',address['quote']);self.assertNotIn('> Sie',address['quote'])
        raw=json.loads(answer());raw['addressing']='Du'
        reference={'message_id':'m1','quote':ctx['messages'][0]['original'].split('\n')[0]}
        raw['evidence']={k:reference for k in raw['evidence']}
        self.assertIn('Anrede: Du',b.validate_output(json.dumps(raw),ctx,starts_at=NOW))

    def test_draft_cannot_ground_fake_named_facts_or_addressing(self):
        ctx=context(event());ctx['messages'][0]['labels']=['DRAFT']
        self.assertEqual(b.latest_own_addressing(ctx['messages'],'external@example.org')['addressing'],'unklar')
        with self.assertRaises(b.ReviewRequired):b.validate_output(answer(),ctx,starts_at=NOW)

    def test_google_calendar_sent_invite_is_not_the_last_personal_addressing(self):
        ctx=context(event())
        invite={'message_id':'calendar','internal_date':'999','original':'Invitation from Google Calendar',
                'payload':{'mimeType':'multipart/alternative','parts':[{'mimeType':'text/calendar'}]},
                'labelIds':['SENT']}
        invite.update(b.message_metadata(invite,{'from':b.CODE,'to':'external@example.org'},b.CODE))
        ctx['messages'].append(invite)
        actual=b.latest_own_addressing(ctx['messages'],'external@example.org')
        self.assertEqual(actual['addressing'],'Sie')
        self.assertEqual(actual['message_id'],'m1')

    def test_bound_header_name_supports_missing_signature(self):
        ctx=context(event());ctx['messages'].append(dict(message_id='incoming',internal_date='200',original='Interesse an Ausschreibungen.',labels=['INBOX'],headers={'from':'Thomas Beispiel | Example GmbH <external@example.org>'}))
        raw=json.loads(answer())
        for field in ('person','company'):
            raw['evidence'][field]={'message_id':'incoming','header':'from','quote':'Thomas Beispiel | Example GmbH <external@example.org>'}
        self.assertIn('Thomas Beispiel',b.validate_output(json.dumps(raw),ctx,starts_at=NOW))
        ctx['messages'][-1]['headers']['from']='Thomas Beispiel | Example GmbH <wrong@example.org>'
        with self.assertRaises(b.ReviewRequired):b.validate_output(json.dumps(raw),ctx,starts_at=NOW)

    def test_ambiguous_telegram_home_is_never_a_delivery_target(self):
        with self.assertRaises(b.ReviewRequired):b.Runtime(self.store,target='telegram')

    def test_private_destination_metadata_binding_fails_on_group_or_mapping_change(self):
        home=Path(self.tmp.name)/'routing';(home/'scripts').mkdir(parents=True)
        (home/'scripts/email_realtime.py').write_text("CHAT = 'telegram:5644072286'\n")
        (home/'.env').write_text('TELEGRAM_ALLOWED_USERS=5644072286\nTELEGRAM_HOME_CHANNEL=5644072286\n')
        path=home/'channel_directory.json'
        path.write_text(json.dumps({'platforms':{'telegram':[{'id':'5644072286','type':'dm','thread_id':None}]}}))
        self.assertTrue(b.verify_private_destination(home))
        path.write_text(json.dumps({'platforms':{'telegram':[{'id':'5644072286','type':'group'}]}}))
        with self.assertRaises(b.ReviewRequired):b.verify_private_destination(home)

    def test_physical_hook_spool_privacy_and_native_observability_import(self):
        import importlib.util,threading
        root=Path(__file__).resolve().parents[2]
        def module(name,file):
            spec=importlib.util.spec_from_file_location(name,root/file);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
        telemetry=module('briefing_telemetry','plugins/ole-coding/telemetry.py')
        observer=module('briefing_observer','plugins/ole-observability/__init__.py')
        class Context:
            def __init__(self):self.hooks={}
            def register_hook(self,event,callback):self.hooks[event]=callback
        ctx=Context();store=observer.Store(Path(self.tmp.name)/'observer')
        try:
            b.register_gtm_telemetry(ctx,{'job_id':'fixture-job'},store.spool,telemetry)
            ctx.hooks['post_api_request'](provider='openai-codex',model='gpt-6.1-sol',api_mode='codex_responses',usage={'prompt_tokens':12,'output_tokens':5,'prompt':'PRIVATE_PROMPT'},prompt='PRIVATE_PROMPT',api_key='PRIVATE_CREDENTIAL',ended_at=b.time.time())
            ctx.hooks['api_request_error'](provider='openai-codex',model='gpt-6.1-sol',api_mode='codex_responses',error_type='TimeoutError',error_message='PRIVATE_ERROR',ended_at=b.time.time())
            text=''.join(p.read_text() for p in store.spool.glob('*.json'))
            self.assertNotIn('PRIVATE',text);self.assertIn('worker_physical',text);self.assertIn('gtm_briefing',text)
            threading.Event().wait(.7)
        finally:store.close()
        report=observer.report(Path(self.tmp.name)/'observer')
        group=next(v for k,v in report['physical'].items() if 'gtm_briefing' in k)
        self.assertEqual(group['attempts'],2);self.assertEqual(group['usage_known'],1)
        self.assertEqual(group['errors'],1)

    def test_render_matches_compact_business_format(self):
        text=b.validate_output(answer(),context(event()),starts_at=NOW)
        self.assertIn('Thomas Beispiel, Geschäftsführer bei Example GmbH',text)
        self.assertIn('**Anrede: Sie.**',text);self.assertNotIn('Warum ihr sprecht',text)

class OriginalQuoteBindingTests(unittest.TestCase):
    def test_whitespace_runs_bind_to_verbatim_original_unique_span(self):
        source='Alpha  Beta\r\n\r\nGamma.'
        self.assertEqual(b.original_quote_span('Alpha Beta Gamma.',source),source)
        for quote,text in [('AlphaBeta','Alpha Beta'),('Al pha','Alpha'),('alpha Beta','Alpha Beta'),('Alpha Beta','Alpha  Beta / Alpha\nBeta')]:
            with self.assertRaises(b.ReviewRequired):b.original_quote_span(quote,text)
    def test_linewrapped_real_quote_retains_last_own_sent_addressing_gate(self):
        ctx=context(event());ctx['messages'][0]['original']=ctx['messages'][0]['original'].replace('. Sie:', '.\r\n\r\nSie:')
        self.assertIn('Anrede: Sie',b.validate_output(answer(),ctx,starts_at=NOW))
    def test_body_footer_match_never_becomes_own_top_evidence(self):
        ctx=context(event());ctx['messages'][0]['original']+='\nBeste Grüße\nIhre  Firma steht bereit.'
        raw=json.loads(answer());raw['evidence']['addressing']={'message_id':'m1','quote':'Ihre Firma steht bereit.'}
        text=b.validate_output(json.dumps(raw),ctx,starts_at=NOW)
        self.assertIn('**Anrede: Sie.**',text);self.assertNotIn('Ihre Firma steht bereit',text)

class BriefingLaunchTests(unittest.TestCase):
    def test_runtime_uses_stable_native_command_and_private_failure_diagnostic(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            store=b.Store(Path(tmp)/'state');runtime=b.Runtime(store,target=b.DESTINATION)
            identity='a'*64;ctx={'briefing_job_id':identity,'private_fixture':'body'}
            command=['managed-python','-I','-c','native-bootstrap-file-entry']
            with patch.object(b,'native_worker_command',return_value=command),patch.object(b.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='private-fixture-error'))as run:
                with self.assertRaises(b.ReviewRequired):runtime.model(ctx)
            self.assertEqual(run.call_args.args[0],command);self.assertNotIn('body',command)
            error=store.root/(identity+'.worker-error.log');self.assertEqual(error.read_text(),'private-fixture-error');self.assertEqual(error.stat().st_mode&0o777,0o600)

class BriefingOpenerTests(unittest.TestCase):
    def fixture(self, original='Unser Büro ist in Berlin.', **metadata):
        ctx=context(event())
        incoming=dict(message_id='incoming',internal_date='200',original=original,
                      account=b.CODE,author='external@example.org',recipients=[b.CODE],labels=['INBOX'])
        incoming.update(metadata);ctx['messages'].append(incoming)
        raw=json.loads(answer());raw['opener']={'kind':'company_location','place':'Berlin',
            'evidence':{'message_id':'incoming','quote':original}}
        return ctx,raw
    def render(self,ctx,raw):return b.validate_output(json.dumps(raw),ctx,starts_at=NOW)
    def test_current_counterparty_company_location_and_signature_address(self):
        for source in ['Unser Büro ist in Berlin.','Example GmbH\nBeispielstraße 10\n10115 Berlin', 'Unser Standort ist\n  Berlin.']:
            ctx,raw=self.fixture(source)
            output=self.render(ctx,raw)
            self.assertEqual(output.count('**Einstieg:**'),1)
            self.assertIn('Wie ist es gerade in Berlin?',output)
            self.assertTrue(output.endswith('Sag mir Bescheid, wenn der Call durch ist.'))
    def test_own_piet_draft_wrong_account_and_nonrecipient_clues_omitted(self):
        for metadata in [dict(author=b.CODE,labels=['SENT']),dict(author=b.PIET),dict(labels=['DRAFT']),
                         dict(account='wrong@example.org'),dict(recipients=['other@example.org'])]:
            ctx,raw=self.fixture(**metadata)
            self.assertNotIn('**Einstieg:**',self.render(ctx,raw))
    def test_absent_unknown_duplicate_history_and_nonrelevant_clues_omitted(self):
        for source in ['Berlin ist interessant.','Ich besuche Berlin.','Unser Büro ist nicht in Berlin.', 'Unser Büro wäre in Berlin.','Ihr Büro ist in Berlin.',
                       'Treffpunkt: 10115 Berlin','> Unser Büro ist in Berlin.',
                       'Hallo\nFrom: Alex\nUnser Büro ist in Berlin.',
                       'Unser Büro ist in Berlin.\nUnser Büro ist in Berlin.']:
            ctx,raw=self.fixture(source)
            # Explicit duplicated short evidence must remain ambiguous.
            if source.count('Unser Büro ist in Berlin.')==2:raw['opener']['evidence']['quote']='Unser Büro ist in Berlin.'
            self.assertNotIn('**Einstieg:**',self.render(ctx,raw))
        ctx,raw=self.fixture();raw['opener']['evidence']['message_id']='missing'
        self.assertNotIn('**Einstieg:**',self.render(ctx,raw))
        ctx,raw=self.fixture();raw['opener']['evidence']['quote']='Unser Büro ist in München.'
        self.assertNotIn('**Einstieg:**',self.render(ctx,raw))
    def test_old_answer_null_multiple_and_free_text_are_backward_compatible(self):
        baseline=b.validate_output(answer(),context(event()),starts_at=NOW)
        for optional in [None,[],[{'place':'Berlin'},{'place':'Paris'}],{'text':'Tolles Unternehmen!'}, {'kind':'personal_interest','place':'Berlin'}]:
            raw=json.loads(answer());raw['opener']=optional
            self.assertEqual(self.render(context(event()),raw),baseline)
    def test_malformed_optional_reference_and_fields_omit_without_second_generation(self):
        malformed = [('message_id', item) for item in ([], {}, None, 1, '', 'x' * 257)]
        malformed += [('quote', item) for item in ([], {}, None, 1, '', 'x' * 501)]
        malformed += [('place', item) for item in ([], {}, None, 1, '')]
        malformed += [('evidence', item) for item in ([], None, 'invalid')]
        baseline=b.validate_output(answer(),context(event()),starts_at=NOW)
        for field,item in malformed:
            with self.subTest(field=field,item=item):
                ctx,raw=self.fixture()
                target=raw['opener']['evidence'] if field in ('message_id','quote') else raw['opener']
                target[field]=item
                self.assertEqual(self.render(ctx,raw),baseline)
                with tempfile.TemporaryDirectory() as folder:
                    store=b.Store(folder);reads=Reads();reads.context=lambda *_:ctx;run=FakeRuntime(store)
                    delivered=[]
                    def model(context):run.calls+=1;return json.dumps(raw)
                    def send(identity,text):run.sends+=1;delivered.append(text);store.step(identity,'delivered');return store.get(identity)
                    run.model=model;run.send=send
                    with patch.object(b.dt,'datetime',wraps=dt.datetime)as clock:
                        clock.now.return_value=NOW
                        result=b.tick(reads,store,run,now=NOW,activate=True)
                    self.assertEqual(run.calls,1);self.assertEqual(result['model_jobs'],1)
                    self.assertEqual(result['delivered'],1)
                    expected=b.validate_output(answer(),context(event()),starts_at=NOW+dt.timedelta(minutes=15))
                    self.assertEqual(delivered,[expected])

    def test_tick_still_uses_one_existing_generation(self):
        ctx,raw=self.fixture()
        with tempfile.TemporaryDirectory() as folder:
            store=b.Store(folder);reads=Reads();reads.context=lambda *_:ctx;run=FakeRuntime(store)
            def model(context):run.calls+=1;return json.dumps(raw)
            run.model=model
            with patch.object(b.dt,'datetime',wraps=dt.datetime)as clock:
                clock.now.return_value=NOW
                result=b.tick(reads,store,run,now=NOW,activate=True)
            self.assertEqual(run.calls,1);self.assertEqual(result['model_jobs'],1)


class NativeBriefingFixtureTests(unittest.TestCase):
    @unittest.skipUnless(b.CORE.is_dir(), 'Pinned VPS Core is required for this integration fixture')
    def test_actual_native_empty_tools_sol61_high_without_provider_call(self):
        sys.path.insert(0,str(b.CORE))
        with tempfile.TemporaryDirectory(prefix='briefing-native-fixture-') as root:
            with patch.dict(os.environ,{'HERMES_HOME':root,'HERMES_DISABLE_LAZY_INSTALLS':'1'}), patch.object(b,'TELEMETRY_SPOOL',Path(root)/'telemetry/external-spool'):
                with patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')), patch.object(socket,'create_connection',side_effect=AssertionError('network forbidden')):
                    import run_agent
                    import hermes_cli.auth_codex as auth
                    original=run_agent.AIAgent
                    captured={}
                    def construct(**kwargs):
                        captured.update(kwargs)
                        return original(**kwargs)
                    fixture_context,fixture_answer=BriefingOpenerTests().fixture()
                    def conversation(agent,prompt):
                        self.assertEqual(agent.tools,[])
                        self.assertEqual(agent.valid_tool_names,set())
                        self.assertEqual(agent._api_max_retries,1)
                        self.assertEqual(json.loads(prompt)['calendar']['id'],'fictional-event')
                        from hermes_cli.plugins import invoke_hook
                        invoke_hook('post_api_request',provider='openai-codex',model='gpt-6.1-sol',usage={'prompt_tokens':12,'output_tokens':5},prompt='PRIVATE_PROMPT',api_key='PRIVATE_CREDENTIAL')
                        invoke_hook('api_request_error',provider='openai-codex',model='gpt-6.1-sol',error_type='TimeoutError',error_message='PRIVATE_ERROR')
                        return {'final_response':json.dumps(fixture_answer)}
                    packet=json.dumps({'permission':'reviewed-briefing-readonly-v1','job_id':'a'*64,'context':fixture_context}).encode()
                    output=io.StringIO()
                    fake_runtime={'provider':'openai-codex','auth_mode':'chatgpt','base_url':'https://chatgpt.com/backend-api/codex','api_key':'fixture-not-a-credential'}
                    with patch.object(auth,'resolve_codex_runtime_credentials',return_value=fake_runtime), patch.object(run_agent,'AIAgent',side_effect=construct), patch.object(original,'run_conversation',conversation), patch.object(sys,'stdin',io.TextIOWrapper(io.BytesIO(packet))), contextlib.redirect_stdout(output):
                        b.native_worker()
                    self.assertEqual(captured['model'],'gpt-6.1-sol')
                    self.assertEqual(captured['reasoning_config'],{'effort':'high'})
                    self.assertEqual(captured['enabled_toolsets'],[])
                    self.assertEqual(captured['max_iterations'],2)
                    spooled=list((Path(root)/'telemetry/external-spool').glob('*.json'))
                    self.assertEqual(len(spooled),2)
                    self.assertNotIn('PRIVATE',''.join(p.read_text() for p in spooled))
                    self.assertEqual(json.loads(output.getvalue())['company'],'Example GmbH')
                    self.assertIn('**Einstieg:**',b.validate_output(output.getvalue(),fixture_context,starts_at=NOW))

    @unittest.skipUnless(b.CORE.is_dir(), 'Pinned VPS Core required for fresh native startup')
    def test_fresh_isolated_subprocess_preserves_packet_and_real_native_construction(self):
        import subprocess
        sys.path.insert(0,str(b.CORE))
        from hermes_cli._launchers import runtime_command
        with tempfile.TemporaryDirectory(prefix='briefing-fresh-subprocess-')as directory:
            root=Path(directory);entry=Path(b.__file__).with_name('native_worker_entry.py').resolve()
            script=root/'fixture.py';proof=root/'construction.json'
            # Instrument only this fresh test process. Actual AIAgent construction and
            # broker credentials remain real; the conversation and provider sockets do not run.
            code = r"""
import importlib.abc,importlib.machinery,json,runpy,socket,sys
from pathlib import Path
PROOF=Path(__PROOF__)
ENTRY=__ENTRY__
original_connect=socket.socket.connect
original_connect_ex=socket.socket.connect_ex
def guard(method):
 def connect(self,address):
  if self.family in (socket.AF_INET,socket.AF_INET6) and address[0]not in ('127.0.0.1','::1','localhost'):
   raise AssertionError('provider network forbidden in native construction fixture')
  return method(self,address)
 return connect
socket.socket.connect=guard(original_connect);socket.socket.connect_ex=guard(original_connect_ex)
class Loader:
 def __init__(self,inner,name):self.inner,self.name=inner,name
 def create_module(self,spec):return self.inner.create_module(spec)if hasattr(self.inner,'create_module')else None
 def exec_module(self,module):
  self.inner.exec_module(module)
  if self.name=='gtm.briefing_daemon':module.TELEMETRY_SPOOL=PROOF.parent/'telemetry'
  else:
   original=module.AIAgent.__init__
   def init(agent,*args,**kwargs):
    assert kwargs['model']=='gpt-6.1-sol'and kwargs['reasoning_config']=={'effort':'high'}
    assert kwargs['provider']=='openai-codex'and kwargs['enabled_toolsets']==[]
    assert kwargs['skip_memory']and kwargs['skip_context_files']and kwargs['skip_background_review']
    original(agent,*args,**kwargs)
   def conversation(agent,prompt):
    data=json.loads(prompt);assert data['calendar']['id']=='fictional-event'
    assert agent.tools==[]and agent.valid_tool_names==set()and agent._api_max_retries==1
    PROOF.write_text(json.dumps({'model':'gpt-6.1-sol','effort':'high','tools':[],'stdin_packet_preserved':True,'isolated':bool(sys.flags.isolated),'managed_python':sys.executable,'provider_calls':0}))
    return {'final_response':json.dumps({'abstain':True,'reason':'native-construction-fixture'})}
   module.AIAgent.__init__=init;module.AIAgent.run_conversation=conversation
class Finder(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  if name not in ('run_agent','gtm.briefing_daemon'):return None
  spec=importlib.machinery.PathFinder.find_spec(name,path)
  if spec and spec.loader:spec.loader=Loader(spec.loader,name)
  return spec
assert 'run_agent'not in sys.modules
sys.meta_path.insert(0,Finder())
runpy.run_path(ENTRY,run_name='__main__')
""".replace('__PROOF__',repr(str(proof))).replace('__ENTRY__',repr(str(entry)))
            script.write_text(code)
            command=runtime_command(b.CORE,code=f"runpy.run_path({str(script)!r}, run_name='__main__')",home='/home/hermes/.hermes')
            env=dict(os.environ,HERMES_HOME='/home/hermes/.hermes',HERMES_DISABLE_LAZY_INSTALLS='1',PYTHONPATH='/intentionally/not/needed')
            packet=json.dumps({'permission':'reviewed-briefing-readonly-v1','job_id':'a'*64,'context':context(event())})
            result=subprocess.run(command,input=packet,text=True,capture_output=True,timeout=100,env=env)
            if result.returncode:
                (root/'private-error.log').write_text(result.stderr)
                self.fail('fresh native construction failed; private fixture diagnostic only')
            self.assertEqual(json.loads(result.stdout)['reason'],'native-construction-fixture')
            metadata=json.loads(proof.read_text());self.assertTrue(metadata['stdin_packet_preserved']);self.assertTrue(metadata['isolated']);self.assertEqual(metadata['tools'],[])
            self.assertFalse(list((root/'telemetry').glob('*.json')))

if __name__=='__main__':unittest.main()
