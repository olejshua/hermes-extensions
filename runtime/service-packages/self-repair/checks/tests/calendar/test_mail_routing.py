"""Real route boundaries with private mail/model/calendar doubles."""
from datetime import datetime
from unittest.mock import Mock
import pytest
from mail_fixtures import runtime as load, interpretation, message, models

NOW=datetime.fromisoformat('2026-10-20T12:00:00+02:00')
SLOT='2026-10-23T10:00:00+02:00'

def point(text='Am 23. Oktober um 10 Uhr?'):
    return interpretation(mode='offers',offers=[{'start':SLOT,'end':None,'quote':text}])

def test_private_hold_never_proves_an_invitation_was_sent(tmp_path):
    r=load(tmp_path);row,thread=message('Am 23. Oktober um 10 Uhr?','stealth')
    r.get_thread=Mock(return_value=(thread,False))
    models(r,point(),{'body':'{{SLOT}} passt. Ich habe Ihnen eine Kalendereinladung geschickt.',
        'question':None,'proposed_slot':SLOT,'referral_recipients':[]})
    with pytest.raises(ValueError,match='invitation_send_claim_unverified'):r.draft_one(row)
    r.w.make_draft.assert_not_called()

@pytest.mark.parametrize('sentence',['Ich habe Ihnen noch keine Kalendereinladung geschickt.',
                                     'Ich schicke Ihnen anschließend eine Einladung.'])
def test_truthful_pending_invitation_draft_is_allowed(tmp_path,sentence):
    r=load(tmp_path);row,thread=message('Am 23. Oktober um 10 Uhr?','stealth')
    r.get_thread=Mock(return_value=(thread,False))
    models(r,point(),{'body':'{{SLOT}} passt. '+sentence,
        'question':None,'proposed_slot':SLOT,'referral_recipients':[]})
    assert r.draft_one(row)['status']=='created'

@pytest.mark.parametrize('alias',['code','stealth'])
def test_exact_offer_uses_both_calendar_guard_and_prepares_usable_reply(tmp_path,alias):
    r=load(tmp_path);row,thread=message('Am 23. Oktober um 10 Uhr?',alias);r.get_thread=Mock(return_value=(thread,False));models(r,point())
    result=r.draft_one(row)
    assert result['status']=='created' and result['appointment_start']==SLOT
    args=r.w.make_draft.call_args;assert args.args[:2]==(alias,'incoming') and args.kwargs['slots']==[SLOT]
    assert 'Beste Grüße\nOle Beispiel' in args.args[2] and 'Beste Grüße\n\n' not in args.args[2]
    assert '10:00 Uhr' in args.args[2] and 'Berliner Zeit' not in args.args[2]
    assert r.read_case(row)['phase']=='completed'
    assert r.model_json.call_args_list[0].args[0]=='understand'
    r.check_incoming_availability.assert_called_once()

@pytest.mark.parametrize('sender',['Host <contact-9@example.invalid>','CODE Alumni <contact-10@example.invalid>'])
def test_luma_stops_before_model_calendar_and_crm(tmp_path,sender):
    r=load(tmp_path);row,_=message();row['from']=sender;r.get_thread=Mock(side_effect=AssertionError('no Gmail'))
    assert r.draft_one(row)['status']=='out_of_scope_platform_mail'
    r._reply_context.assert_not_called();r.check_incoming_availability.assert_not_called();r.w.make_draft.assert_not_called()

@pytest.mark.parametrize('tid',['e000000000000001','e000000000000002','e000000000000003','e000000000000004'])
def test_deliberately_closed_cases_do_not_reopen(tmp_path,tid):
    r=load(tmp_path);row,_=message(tid=tid);r.get_thread=Mock(side_effect=AssertionError('closed case'))
    assert r.draft_one(row)['status']=='deliberately_closed'
    r.w.make_draft.assert_not_called();r._reply_context.assert_not_called()

def test_cc_information_to_someone_else_is_silent(tmp_path):
    r=load(tmp_path);row,thread=message('Lieber Partner, passt Freitag?','stealth')
    thread[0].update(to='partner@example.invalid',cc='operator@secondary.example.invalid');r.get_thread=Mock(return_value=(thread,False))
    assert r.draft_one(row)['status']=='cc_information';r._reply_context.assert_not_called()

def test_old_original_does_not_create_draft_after_user_answer(tmp_path):
    r=load(tmp_path);row,thread=message();thread.append(dict(thread[0],id='user-answer',labels=['SENT']))
    r.get_thread=Mock(return_value=(thread,False));assert r.draft_one(row)['status']=='not_worthy_or_already_handled'
    r.w.make_draft.assert_not_called()

def test_busy_exact_offer_asks_recipient_for_new_window_without_extra_proposals(tmp_path):
    r=load(tmp_path);_,thread=message('Am 23. Oktober um 10 Uhr?');r.check_incoming_availability=Mock(return_value=False)
    alternative='2026-10-23T12:00:00+02:00';r.calendar_options=Mock(return_value=[{'start':alternative,'label':'Freitag, 23.10.2026, 12:00 Uhr'}])
    models(r,point(),{'body':'Zu der angebotenen Zeit passt es bei mir leider nicht. Hätten Sie ein anderes Zeitfenster?',
                     'question':None,'proposed_slots':[],'referral_recipients':[]})
    result=r.generate('code','incoming',thread,False,now=NOW)
    assert result['proposed_slots']==[] and 'anderes Zeitfenster' in result['body']
    assert r.model_json.call_args_list[1].args[1]['calendar']['relation']=='offer_conflict'
    r.calendar_options.assert_not_called()


def test_busy_offer_never_adds_unasked_alternatives_or_asks_ole_to_check(tmp_path):
    for answer in [dict(body='Dann gerne {{SLOT}}.',question=None,proposed_slots=[SLOT]),
                   dict(body=None,question='Welche Zeiten sind frei?',proposed_slots=[])]:
        r=load(tmp_path);_,thread=message('Am 23. Oktober um 10 Uhr?')
        r.check_incoming_availability=Mock(return_value=False)
        models(r,point(),{**answer,'referral_recipients':[]})
        with pytest.raises(r.MailWorkflowError):
            r.generate('code','incoming',thread,False,now=NOW)


def test_one_available_offered_window_produces_only_one_reply_start(tmp_path):
    r=load(tmp_path);_,thread=message('Am 23. Oktober zwischen 10 und 12 Uhr?')
    starts=[SLOT,'2026-10-23T11:00:00+02:00']
    intent=interpretation(mode='offers',offers=[dict(start=SLOT,end='2026-10-23T12:00:00+02:00',
                                                   quote='23. Oktober zwischen 10 und 12 Uhr')])
    r.calendar_options=Mock(return_value=[{'start':s,'label':s}for s in starts])
    models(r,intent,dict(body='Ginge {{SLOT_1}} oder {{SLOT_2}}?',question=None,
                         proposed_slots=starts,referral_recipients=[]))
    with pytest.raises(r.MailWorkflowError,match='offered_window_needs_one_selected_start'):
        r.generate('code','incoming',thread,False,now=NOW)

def test_unverified_choice_and_false_refusal_are_rejected(tmp_path):
    for body,slot in [('Gerne {{SLOT}}.','2026-10-23T12:00:00+02:00'),('Leider bin ich belegt. {{SLOT}}.',SLOT)]:
        r=load(tmp_path);_,thread=message('Am 23. Oktober um 10 Uhr?')
        models(r,point(),{'body':body,'question':None,'proposed_slot':slot,'referral_recipients':[]})
        with pytest.raises(r.MailWorkflowError):r.generate('code','incoming',thread,False,now=NOW)

def test_general_request_uses_live_choices_without_user_calendar_question(tmp_path):
    r=load(tmp_path);_,thread=message();r.calendar_options=Mock(return_value=[{'start':SLOT,'label':'Freitag, 23.10.2026, 10:00 Uhr'}])
    models(r,interpretation(mode='request'));result=r.generate('code','incoming',thread,False,now=NOW)
    assert result['proposed_slots']==[SLOT];r.calendar_options.assert_called_once_with(now=NOW)

def test_long_history_is_used_instead_of_silent_abandonment(tmp_path):
    r=load(tmp_path);row,latest=message('Welche Schritte wollt ihr automatisieren?')
    thread=[dict(latest[0],id='old-'+str(i),text='Geschäftskontext '+str(i)+' '+('Inhalt '*800)) for i in range(14)]+latest
    r.get_thread=Mock(return_value=(thread,False))
    models(r,interpretation(mode='none'),{'body':'Wir möchten die Recherche zu Ausschreibungen vereinfachen.','question':None,'proposed_slot':None,'referral_recipients':[]})
    result=r.draft_one(row)
    assert result['status']=='created' and len(r.model_json.call_args_list[0].args[1]['thread'])==15
    assert len(r.model_json.call_args_list[0].args[1]['thread'][0]['text'])>4000

def test_new_incoming_reply_at_write_boundary_supersedes_draft(tmp_path):
    r=load(tmp_path);row,thread=message('Am 23. Oktober um 10 Uhr?');r.get_thread=Mock(return_value=(thread,False));models(r,point())
    r.w.make_draft.side_effect=r.w.SourceChangedBeforeDraft()
    assert r.draft_one(row)['status']=='superseded_by_new_reply'
    assert r.read_case(row)['phase']=='completed'

def test_invitation_request_prepares_draft_but_requires_oles_concrete_approval(tmp_path):
    r=load(tmp_path);row,thread=message('Am 23. Oktober um 10 Uhr?');r.get_thread=Mock(return_value=(thread,False))
    intent=point();intent.update(reply_needed=False,invitation_requested=True);models(r,intent)
    assert r.draft_one(row)['status']=='created'
    case=r.read_case(row);assert case['phase']=='awaiting_approval' and case['pending_decision']['authorized'] is False
    assert case['pending_decision']['start']==SLOT

def test_request_for_several_times_produces_every_checked_reserved_start(tmp_path):
    r=load(tmp_path);row,thread=message('Bitte lassen Sie uns einige Terminvorschläge zukommen.','stealth')
    r.get_thread=Mock(return_value=(thread,False))
    starts=['2026-10-23T12:00:00+02:00','2026-10-26T12:00:00+01:00']
    r.calendar_options=Mock(return_value=[{'start':s,'label':s} for s in starts])
    models(r,interpretation(mode='request'),{'body':'Gerne. Passt {{SLOT_1}} oder {{SLOT_2}}?',
        'question':None,'proposed_slots':starts,'referral_recipients':[]})
    result=r.draft_one(row)
    assert result['status']=='created' and r.w.make_draft.call_args.kwargs['slots']==starts
    assert r.read_case(row)['prepared_slots']==starts

def test_multiple_requested_times_cannot_silently_shrink_to_one(tmp_path):
    r=load(tmp_path);_,thread=message('Bitte einige Terminvorschläge.');starts=[SLOT,'2026-10-26T12:00:00+01:00']
    r.calendar_options=Mock(return_value=[{'start':s,'label':s} for s in starts]);models(r,interpretation(mode='request'))
    with pytest.raises(r.MailWorkflowError,match='requested_multiple_options_missing'):
        r.generate('stealth','incoming',thread,False,now=NOW)

def test_select_one_sender_offer_without_unnecessary_conditional_alternatives(tmp_path):
    # Wußmann's actual distinction: two available windows, alternatives only if
    # neither fits. A free offered start needs one reply, not two proposals.
    r=load(tmp_path)
    text=('Do, 29.10.2026, 11:00-13:00 Uhr sowie Fr, 30.10.2026, 11:30-13 Uhr. '
          'Vielleicht passen ja 30 min für ein Erstgespräch auch in Ihre Kalender, '
          'ansonsten schlagen sie bitte gerne ein oder zwei Alternativen vor.')
    row,thread=message(text,'stealth');r.get_thread=Mock(return_value=(thread,False))
    starts=['2026-10-29T11:00:00+01:00','2026-10-30T11:30:00+01:00']
    intent=interpretation(mode='offers',offers=[
        {'start':starts[0],'end':'2026-10-29T13:00:00+01:00','quote':'Do, 29.10.2026, 11:00-13:00 Uhr'},
        {'start':starts[1],'end':'2026-10-30T13:00:00+01:00','quote':'Fr, 30.10.2026, 11:30-13 Uhr'}])
    intent['requested_options']=2
    r.calendar_options=Mock(side_effect=lambda **kw:[{'start':kw['window'][0].isoformat(),'label':'geprüfter Termin'}])
    models(r,intent,{'body':'Vielen Dank. {{SLOT_1}} passt mir für unser Erstgespräch.',
                    'question':None,'proposed_slots':[starts[0]],'referral_recipients':[]})
    result=r.draft_one(row)
    assert result['status']=='created'
    assert result['proposed_slots']==[starts[0]]
    assert r.w.make_draft.call_args.kwargs['slots']==[starts[0]]

def test_repaired_validation_retry_never_repeats_uncertain_draft_write(tmp_path):
    r=load(tmp_path);row,thread=message('Am 23. Oktober um 10 Uhr?')
    r.get_thread=Mock(return_value=(thread,False));models(r,point())
    r.remember_case(row,phase='review_required',error_code='requested_multiple_options_missing',
                    outcome={'status':'workflow_blocked'})
    assert r.draft_one(row,retry_readonly=True)['status']=='created'
    assert r.w.make_draft.call_count==1
    other={**row,'id':'uncertain'}
    r.remember_case(other,phase='review_required',error_code='requested_multiple_options_missing',
                    prepared_body='possibly written',outcome={'status':'needs_manual_review_draft_state'})
    assert r.draft_one(other,retry_readonly=True)['status']=='needs_manual_review_draft_state'
    assert r.w.make_draft.call_count==1

def test_concurrent_automatic_and_manual_processing_only_writes_one_draft(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    r=load(tmp_path);row,thread=message('Am 23. Oktober um 10 Uhr?');r.get_thread=Mock(return_value=(thread,False));models(r,point())
    started=threading.Event();release=threading.Event()
    def write(*a,**kw):
        started.set();assert release.wait(3)
        return {'status':'created','draftId':'only-one'}
    r.w.make_draft.side_effect=write
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(r.draft_one,row);assert started.wait(3)
        second=pool.submit(r.draft_one,row);release.set()
        assert first.result()['draft_id']==second.result()['draft_id']=='only-one'
    assert r.w.make_draft.call_count==1
    assert [call.args[0]for call in r.model_json.call_args_list]==['understand','compose','review']

def test_telegram_cli_calls_the_same_managed_case_flow(tmp_path,capsys):
    r=load(tmp_path);r.draft_one=Mock(return_value={'status':'created','draft_id':'private-draft'})
    result=r.cli(['prepare','--account','stealth','--message-id','incoming','--thread-id','thread'])
    r.draft_one.assert_called_once_with({'account':'stealth','id':'incoming','threadId':'thread'},retry_readonly=True)
    assert result['status']=='created' and 'private-draft' in capsys.readouterr().out

def test_manual_caller_cannot_spoof_current_sender_or_received_date(tmp_path):
    r=load(tmp_path);row,thread=message('Am 23. Oktober um 10 Uhr?');row.update({'from':'spoof@example.invalid','internalDate':'1'})
    r.get_thread=Mock(return_value=(thread,False));models(r,point());r.draft_one(row)
    real_row=r._reply_context.call_args.args[1]
    assert real_row['from']==thread[-1]['from'] and real_row['internalDate']==thread[-1]['internalDate']
