"""User outcomes across interpretation, calendars, drafts and the Telegram outbox."""
import copy
import json
import sys
import types
from datetime import datetime
from unittest.mock import Mock, patch
import pytest
from mail_fixtures import runtime as load, interpretation, message, models

NOW = datetime.fromisoformat('2026-10-20T12:00:00+02:00')
MARINA = ('Ich wäre diesen Donnerstag von 13-16 Uhr noch flexibel verfügbar '
          'oder alternativ nächsten Dienstag (27.10.) von 9-11 Uhr.')
OFFERS = [
 {'start':'2026-10-22T13:00:00+02:00','end':'2026-10-22T16:00:00+02:00',
  'quote':'diesen Donnerstag von 13-16 Uhr noch flexibel verfügbar'},
 {'start':'2026-10-27T09:00:00+01:00','end':'2026-10-27T11:00:00+01:00',
  'quote':'nächsten Dienstag (27.10.) von 9-11 Uhr'}]

@pytest.fixture
def runtime(tmp_path): return load(tmp_path)

def test_marina_all_windows_reach_calendar_before_draft(runtime):
    _, thread = message(MARINA)
    chosen = {'start':'2026-10-27T09:00:00+01:00','label':'Dienstag, 27.10.2026, 09:00 Uhr'}
    calls=[]
    def options(**kw):
        calls.append(kw['window']); return [chosen] if kw['window'][0].day == 27 else []
    runtime.calendar_options=Mock(side_effect=options)
    models(runtime,interpretation(mode='offers',offers=OFFERS),
           {'body':'Gerne {{SLOT}}.','question':None,'proposed_slot':chosen['start'],'referral_recipients':[]})
    result=runtime.generate('code','incoming',thread,False,now=NOW)
    assert len(calls)==2 and result['proposed_slots']==[chosen['start']]
    assert '09:00 Uhr' in result['body'] and '11:00 Uhr' not in result['body']
    assert runtime.model_json.call_args_list[0].args[0]=='understand'
    assert runtime.model_json.call_args_list[1].args[1]['calendar']['relation']=='within_offer'

@pytest.mark.parametrize('offer',[
    dict(OFFERS[0], start='2026-10-22T16:00:00+02:00', end=None),
    dict(OFFERS[0], start='2026-10-23T13:00:00+02:00'),
    dict(OFFERS[0], quote='unbelegter Termin'),
    dict(OFFERS[1], start='2026-10-27T09:00:00+02:00'),
])
def test_invented_or_wrongly_resolved_window_never_reaches_calendar(runtime,offer):
    _,thread=message(MARINA);models(runtime,interpretation(mode='offers',offers=[offer]))
    with pytest.raises(runtime.MailWorkflowError):runtime.generate('code','incoming',thread,False,now=NOW)
    runtime.calendar_options.assert_not_called()

@pytest.mark.parametrize('quote,start,end',[
 ('Morgen zwischen 9 und 11 Uhr','2026-10-21T09:00:00+02:00','2026-10-21T11:00:00+02:00'),
 ('Donnerstag von 13–16 Uhr','2026-10-22T13:00:00+02:00','2026-10-22T16:00:00+02:00'),
 ('Dienstag (27.10.) von 9-11 Uhr','2026-10-27T09:00:00+01:00','2026-10-27T11:00:00+01:00'),
])
def test_human_window_forms_keep_their_bounds(runtime,quote,start,end):
    _,thread=message(quote);thread[0]['internalDate']=str(int(NOW.timestamp()*1000))
    choice={'start':start,'label':'Geprüfter Beginn'};runtime.calendar_options=Mock(return_value=[choice])
    models(runtime,interpretation(mode='offers',offers=[dict(start=start,end=end,quote=quote)]),
           {'body':'Gerne {{SLOT}}.','question':None,'proposed_slot':start,'referral_recipients':[]})
    runtime.generate('code','incoming',thread,False,now=NOW)
    assert runtime.calendar_options.call_args.kwargs['window']==(datetime.fromisoformat(start),datetime.fromisoformat(end))

@pytest.mark.parametrize('start', ['2026-10-25T02:30:00+02:00','2027-03-28T02:30:00+01:00'])
def test_ambiguous_or_nonexistent_dst_time_is_never_offered(runtime,start):
    _,thread=message('Um 02:30 Uhr.');models(runtime,interpretation(mode='offers',offers=[dict(start=start,end=None,quote='Um 02:30 Uhr.')]))
    with pytest.raises(runtime.MailWorkflowError):runtime.generate('code','incoming',thread,False,now=NOW)
    runtime.check_incoming_availability.assert_not_called()

def test_morning_window_checks_two_calendars_and_only_legal_starts(runtime):
    requests=[]
    class Calendar:
        def freebusy(self):return self
        def query(self,body):requests.append(body);return self
        def execute(self):return {'calendars':{'primary':{'busy':[]},'imported':{'busy':[]}}}
    runtime.calendar_service=lambda:Calendar()
    policy=types.SimpleNamespace(_calendars=lambda _:('primary','imported'),pending_busy=lambda:[])
    window=tuple(map(datetime.fromisoformat,('2026-10-21T09:00:00+02:00','2026-10-21T11:00:00+02:00')))
    with patch.dict(sys.modules,{'mail_policy':policy}):options=runtime.live_calendar_options(now=NOW,window=window)
    assert options and all(window[0]<=datetime.fromisoformat(x['start']) and datetime.fromisoformat(x['start']).timestamp()+900<=window[1].timestamp() for x in options)
    assert len(requests)==1 and len(requests[0]['items'])==2

def test_calendar_failure_keeps_case_and_does_not_delegate_to_user(runtime):
    row,thread=message('Am 23. Oktober um 10 Uhr?');runtime.get_thread=Mock(return_value=(thread,False))
    models(runtime,interpretation(mode='offers',offers=[dict(start='2026-10-23T10:00:00+02:00',end=None,quote=thread[0]['text'])]))
    runtime.check_incoming_availability=Mock(side_effect=RuntimeError('private provider data'))
    state={'queue':[]};active={'message':row,'attempts':0,'after':0};state['queue'].append(active)
    with pytest.raises(runtime.MailWorkflowError) as err:runtime.draft_one(row)
    runtime.processing_failed(state,active,err.value)
    assert runtime.read_case(row)['phase']=='retry_pending' and active in state['queue']
    runtime.processing_failed(state,active,err.value)
    assert runtime.read_case(row)['phase']=='review_required' and 'code:incoming' in state['open_reviews']
    assert 'manuell' not in state['notifications'][0]['text'] and 'private provider data' not in json.dumps(state)
    runtime.w.make_draft.assert_not_called()

def test_unknown_gmail_write_is_not_repeated_after_restart(runtime):
    row,thread=message('Am 23. Oktober um 10 Uhr?');runtime.get_thread=Mock(return_value=(thread,False))
    models(runtime,interpretation(mode='offers',offers=[dict(start='2026-10-23T10:00:00+02:00',end=None,quote=thread[0]['text'])]))
    runtime.w.make_draft.side_effect=runtime.w.DraftStateUnverified('unknown')
    first=runtime.draft_one(row);second=runtime.draft_one(row)
    assert first['status']==second['status']=='needs_manual_review_draft_state'
    assert runtime.w.make_draft.call_count==1 and runtime.read_case(row)['prepared_slots']

def test_killed_process_at_write_boundary_is_not_replayed(runtime):
    row,_=message();runtime.remember_case(row,phase='draft_write_pending',prepared_body='private')
    assert runtime.draft_one(row)['status']=='needs_manual_review_draft_state'
    runtime.w.make_draft.assert_not_called();runtime._reply_context.assert_not_called()

def test_telegram_uncertain_send_is_saved_and_never_blindly_replayed(runtime):
    row,_=message();runtime.remember_case(row,phase='completed')
    state={};outcome={'case_key':'code:incoming','status':'created','notify':True}
    runtime.queue_draft_notification(state,row,outcome)
    runtime.notify=Mock(side_effect=TimeoutError())
    runtime.flush_notifications(state);runtime.flush_notifications(state)
    runtime.queue_draft_notification(state,row,outcome)
    assert runtime.notify.call_count==1 and not state['notifications']
    assert state['notification_cases']['code:incoming']=='uncertain' and state['delivery_reviews']
    assert runtime.read_case(row)['delivery']['state']=='uncertain'

def test_short_notice_has_result_without_mail_addresses_or_decoration(runtime):
    row,_=message();row['from']='"Nachname, Vorname" <private@example.invalid>'
    outcome={'status':'created','notify':True,'company':'Firma','notification_summary':'möchte einen Termin 🙂 kontakt@example.invalid'}
    state={};runtime.queue_draft_notification(state,row,outcome);text=state['notifications'][0]
    assert 'Vorname Nachname' in text and 'Entwurf ist bereit' in text and len(text)<200
    assert all(x not in text for x in ['@','🙂','CODE-Postfach','thread'])

def test_invitation_question_names_concrete_checked_start(runtime):
    row,_=message();state={}
    runtime.queue_draft_notification(state,row,{'status':'created','notify':True,
        'invitation_approval_required':True,'appointment_start':'2026-10-22T13:00:00+02:00'})
    text=state['notifications'][0];assert '22.10.' in text and '13:00' in text and 'Soll ich die Einladung erstellen?' in text

def test_routine_newsletter_stays_quiet_and_never_loads_crm(runtime):
    row,thread=message('Rückblick SCCON: automatisierte Workflows');runtime.get_thread=Mock(return_value=(thread,False))
    models(runtime,interpretation(kind='newsletter',reply=False,notify=False))
    outcome=runtime.draft_one(row);state={};runtime.queue_draft_notification(state,row,outcome)
    assert not state;runtime._reply_context.assert_not_called();runtime.w.make_draft.assert_not_called()

@pytest.mark.parametrize('receipt',[{'success':True,'skipped':True,'message_id':'1'}, {'success':True}, {'error':'failed'}])
def test_cli_exit_zero_is_not_enough_for_a_delivered_telegram_notice(runtime,receipt):
    with patch.object(runtime.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout=json.dumps(receipt))):
        with pytest.raises(RuntimeError):runtime.notify('Private test')

def test_restarting_before_write_reuses_interpretation_and_only_same_fresh_calendar_plan(runtime):
    _,thread=message('Am 23. Oktober um 10 Uhr?')
    intent=interpretation(mode='offers',offers=[dict(start='2026-10-23T10:00:00+02:00',end=None,quote=thread[0]['text'])])
    models(runtime,intent);journal={}
    runtime.generate('code','incoming',thread,False,now=NOW,journal=lambda **kw:journal.update(kw))
    assert runtime.model_json.call_count==3
    runtime.generate('code','incoming',thread,False,now=NOW,cached=journal['understanding'],cached_plan=journal['draft_plan'],
                     cached_standard=journal['result_standard'])
    assert runtime.model_json.call_count==3 and runtime.check_incoming_availability.call_count==2


def test_model_cannot_duplicate_sender_and_company_in_notification(runtime):
    row,_=message();row['from']='Tin Ma <person@example.invalid>';state={}
    runtime.queue_draft_notification(state,row,{'status':'created','notify':True,'company':'igus',
        'notification_summary':'Tin Ma: igus möchte das Partnerprogramm kennenlernen.'})
    assert state['notifications']==['Tin Ma (igus) möchte das Partnerprogramm kennenlernen. Der Entwurf ist bereit.']

def test_postscript_after_signature_remains_part_of_current_mail(runtime):
    quote='P.S. Am 23. Oktober um 10 Uhr hätte ich Zeit.'
    _,thread=message('Hallo Alex,\n\nDanke.\n\nBeste Grüße\nEin Kontakt\nBürozeiten 9-17 Uhr\n\n'+quote)
    intent=interpretation(mode='offers',offers=[dict(start='2026-10-23T10:00:00+02:00',end=None,quote=quote)])
    models(runtime,intent)
    result=runtime.generate('code','incoming',thread,False,now=NOW)
    assert quote in runtime.model_json.call_args_list[0].args[1]['thread'][-1]['text']
    assert result['proposed_slots']==['2026-10-23T10:00:00+02:00']

def test_explicit_signature_office_hours_cannot_become_a_meeting_offer(runtime):
    text='Danke.\n\nBeste Grüße\nKontakt\nBürozeiten: Donnerstag von 9-17 Uhr'
    _,thread=message(text)
    intent=interpretation(mode='offers',offers=[dict(start='2026-10-22T09:00:00+02:00',end='2026-10-22T17:00:00+02:00',quote='Donnerstag von 9-17 Uhr')])
    models(runtime,intent)
    with pytest.raises(runtime.MailWorkflowError,match='signature_hours_are_not_an_offer'):
        runtime.generate('code','incoming',thread,False,now=NOW)
    runtime.calendar_options.assert_not_called()

def test_personal_call_announcement_does_not_depend_on_calendar_access(runtime):
    text='Ich rufe heute um 17 Uhr an.';_,thread=message(text)
    intent=interpretation(mode='announcement',reply=False,offers=[dict(start='2026-10-20T17:00:00+02:00',end=None,quote=text)])
    intent['summary']='ruft dich heute um 17 Uhr an';models(runtime,intent)
    runtime.check_incoming_availability=Mock(side_effect=AssertionError('not needed for a notice'))
    result=runtime.generate('stealth','incoming',thread,False,now=NOW)
    assert result['information_only'] and result['notify']
    assert [call.args[0]for call in runtime.model_json.call_args_list]==['understand','review']
    runtime.check_incoming_availability.assert_not_called()

def test_bottom_reply_after_quoted_history_is_not_cut_off(runtime):
    quote='Meine Antwort: Am 23. Oktober um 10 Uhr hätte ich Zeit.'
    _,thread=message('Am 19.10. schrieb Alex:\n> Wann passt es Ihnen?\n\n'+quote)
    models(runtime,interpretation(mode='offers',offers=[dict(start='2026-10-23T10:00:00+02:00',end=None,quote=quote)]))
    result=runtime.generate('code','incoming',thread,False,now=NOW)
    assert quote in runtime.model_json.call_args_list[0].args[1]['thread'][-1]['text']
    assert result['proposed_slots']==['2026-10-23T10:00:00+02:00']

def test_quoted_old_time_cannot_be_misreported_as_current_offer(runtime):
    text='Danke.\n\n> Am 23. Oktober um 10 Uhr hätte ich Zeit.';_,thread=message(text)
    models(runtime,interpretation(mode='offers',offers=[dict(start='2026-10-23T10:00:00+02:00',end=None,quote='Am 23. Oktober um 10 Uhr hätte ich Zeit.')]))
    with pytest.raises(runtime.MailWorkflowError,match='quoted_time_is_not_a_current_offer'):
        runtime.generate('code','incoming',thread,False,now=NOW)
    runtime.check_incoming_availability.assert_not_called()

def test_explicit_manual_retry_can_recheck_calendar_but_never_unknown_write(runtime):
    row,thread=message('Am 23. Oktober um 10 Uhr?');runtime.get_thread=Mock(return_value=(thread,False))
    models(runtime,interpretation(mode='offers',offers=[dict(start='2026-10-23T10:00:00+02:00',end=None,quote=thread[0]['text'])]))
    runtime.remember_case(row,phase='review_required',error_code='calendar_unavailable',outcome={'status':'workflow_blocked'})
    assert runtime.draft_one(row)['status']=='workflow_blocked';runtime.model_json.assert_not_called()
    assert runtime.draft_one(row,retry_readonly=True)['status']=='created'
    assert runtime.w.make_draft.call_count==1
    other={**row,'id':'unknown'};runtime.remember_case(other,phase='review_required',error_code='RuntimeError',prepared_body='possibly written',outcome={'status':'needs_manual_review_draft_state'})
    assert runtime.draft_one(other,retry_readonly=True)['status']=='needs_manual_review_draft_state'
    assert runtime.w.make_draft.call_count==1

def test_completed_repair_closes_only_its_stale_review_index(runtime):
    row,_=message();other={**row,'id':'other'}
    runtime.remember_case(row,phase='completed',outcome={'status':'created','draft_id':'confirmed'})
    runtime.remember_case(other,phase='review_required',outcome={'status':'needs_manual_review_draft_state'})
    s={'open_reviews':{'done':{'message':row},'unknown':{'message':other}}}
    assert runtime.reconcile_open_reviews(s)
    assert set(s['open_reviews'])=={'unknown'}

def test_failed_model_json_keeps_complete_input_and_output_without_sdk_stderr(runtime,monkeypatch):
    import types
    context={'account':'code','current_message':'incoming','thread':[{'thread_id':'thread','text':'Original '*900}]}
    monkeypatch.setattr(runtime.subprocess,'run',Mock(return_value=types.SimpleNamespace(returncode=0,stdout='invalid JSON output',stderr='PRIVATE_AUTH_HEADER')))
    with pytest.raises(runtime.MailWorkflowError,match='model_output_format'):
        runtime.model_json('compose',context)
    value=json.loads((runtime.DATA/'cases/code-incoming.compose-failure.json').read_text())
    assert value['context']==context and value['output']=='invalid JSON output'
    assert 'PRIVATE_AUTH_HEADER' not in json.dumps(value)
