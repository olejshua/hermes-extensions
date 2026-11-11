"""Proactive incidents resume the native pipeline, retaining ownership fences."""
import copy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'second-brain/src'),str(ROOT/'tests/calendar'),str(ROOT/'gtm/calendar/runtime')]
from mail_fixtures import runtime,message,models,interpretation
import mail_result_io as io
import mail_outcomes as standard
from self_repair import recover

def case(tmp_path):
    r=runtime(tmp_path);item,thread=message('Welche Schritte wollen Sie automatisieren?','stealth')
    for m in thread:m['thread_id']=item['threadId']
    r.get_thread=Mock(return_value=(thread,False))
    r.notify=Mock(return_value={'message_id':'private-notification'})
    r.remember_case(item,phase='completed',original_thread=thread,outcome={'status':'no_draft_needed'})
    answer=dict(body='Ich möchte die Recherche vereinfachen.',question=None,proposed_slots=[],referral_recipients=[])
    models(r,interpretation(),answer)
    receipt={'contract':standard.CONTRACT,'review':{'reply_required':True,'result':'missing'}}
    snapshot={'item':item,'case':r.read_case(item),'original_thread':thread,
              'expectation':{'contract':standard.CONTRACT,'receipt':receipt,'owned_draft':None}}
    facts=dict(state='current',thread=thread,draft=None,hard_facts={},owned_draft=None)
    return r,item,snapshot,facts

def test_missing_result_runs_real_preparation_once_then_verifies_saved_result(tmp_path):
    r,item,snapshot,facts=case(tmp_path)
    def read(*_,**kw):
        if r.w.make_draft.called:
            # Saved readback is the actual signed body supplied to the native
            # writer, with explicit service evidence rather than just a marker.
            return {**facts,'draft':r.w.make_draft.call_args.args[2],
                    'owned_draft':{'draft_id':'private-draft'},
                    'hard_facts':{'saved_in_gmail':True,'source_bound':True,'single_draft':True,
                                  'calendar_required':False,'calendar_verified':False,'hard_errors':[]}}
        return facts
    with patch.dict(sys.modules,{'email_realtime':r}),patch.object(io,'current',side_effect=read),patch.object(recover,'LIVE',tmp_path):
        assert recover.mail(snapshot,'repair')['state']=='restored'
        assert recover.mail(snapshot,'repair')['reused']is True
    assert r.w.make_draft.call_count==r.notify.call_count==1
    assert r.read_case(item)['standard_verified']

@pytest.mark.parametrize('mode',['unknown_create','deleted_saved_draft','user_edited_draft','changed_source'])
def test_uncertain_or_user_changed_results_never_replay(tmp_path,mode):
    r,item,snapshot,facts=case(tmp_path)
    if mode=='unknown_create':r.remember_case(item,phase='draft_write_pending',prepared_body='Possibly saved')
    elif mode=='deleted_saved_draft':r.remember_case(item,outcome={'status':'created','draft_id':'private-draft'})
    elif mode=='user_edited_draft':facts.update(draft='Von Alex verändert.')
    else:facts['thread']=[{**facts['thread'][0],'text':'Geändertes Original'}]
    with patch.dict(sys.modules,{'email_realtime':r}),patch.object(io,'current',return_value=facts):
        result=recover.mail(snapshot,'repair')
    assert result['state']in{'needs_write_readback','previously_saved_draft_removed','needs_existing_draft_review','source_changed'}
    r.w.make_draft.assert_not_called();r.model_json.assert_not_called()

def test_same_implementation_failure_is_not_retried_on_every_poll(tmp_path):
    r,item,snapshot,facts=case(tmp_path)
    class Failure(ValueError):code='calendar_unavailable'
    r._draft_one=Mock(side_effect=Failure())
    with patch.dict(sys.modules,{'email_realtime':r}),patch.object(io,'current',return_value=facts),patch.object(recover,'LIVE',tmp_path):
        assert recover.mail(snapshot,'repair')['state']=='workflow_blocked'
        assert recover.mail(snapshot,'repair')['state']=='case_changed_or_retry_used'
    assert r._draft_one.call_count==1

def test_restored_invitation_case_keeps_concrete_approval_question(tmp_path):
    r,item,snapshot,facts=case(tmp_path)
    outcome=dict(status='created',notify=True,appointment=True,invitation_approval_required=True,
        appointment_start='2026-11-05T14:00:00+01:00',case_key='stealth:incoming',crm_review_required=True)
    text=recover.mail_notice(r,item,facts['thread'][-1],outcome)
    assert '05.11. um 14:00 Uhr'in text and 'Soll ich die Einladung erstellen?'in text
    assert '@'not in text
    assert outcome['crm_review_required']is True
    r.w.make_draft.assert_not_called();r.model_json.assert_not_called();r.notify.assert_not_called()
