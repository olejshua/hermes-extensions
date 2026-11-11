"""Pinned original goal and countercases for automatic text-only repairs."""
import copy
import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('business_project_under_test',ROOT/'gtm/calendar/runtime/calendar_routing.py')
route=importlib.util.module_from_spec(spec);spec.loader.exec_module(route)
DEMO='Vielen Dank für Ihr Interesse an Acribic. Wir stellen Ihnen Acribic gerne in einer Demo vor.'

def thread(text=DEMO):
    return [dict(id='own-source',thread_id='thread',from_=None,**{'from':route.CODE},to='person@example.invalid',cc='',
        text=text,labels=['SENT'],internalDate='1000',rfc_message_id='<own-source>',in_reply_to='<prior-external>',truncated=False),
        dict(id='incoming',thread_id='thread',**{'from':'person@example.invalid'},to=route.CODE,cc='',
        text='Gerne bestätige ich den Termin. Schicken Sie eine Einladung?',labels=[],internalDate='2000',
        rfc_message_id='<incoming>',in_reply_to='<own-source>',truncated=False)]

def test_actual_owned_demo_reply_is_project_evidence_without_research_keywords():
    assert route.project_from_thread('code',thread())=='acribic'

@pytest.mark.parametrize('change',['private','friend','incoming_only','quoted_only','wrong_sender','unsent'])
def test_business_text_cannot_replace_owned_source_identity(change):
    rows=thread()
    if change=='private':rows[0]['text']='Treffen wir uns privat auf einen Kaffee?'
    elif change=='friend':rows[0]['text']='Ein Freund zeigt mir Acribic in einer Demo beim Abendessen.'
    elif change=='incoming_only':rows[0]['text']='Hallo.';rows[1]['text']=DEMO
    elif change=='quoted_only':rows[0]['text']='Danke.\n> '+DEMO
    elif change=='wrong_sender':rows[0]['from']='someone-else@example.invalid'
    else:rows[0]['labels']=['DRAFT']
    assert route.project_from_thread('code',rows)is None

@pytest.mark.parametrize('change',['broken_link','duplicate_rfc','foreign_cc','truncated','future_source'])
def test_valid_business_text_never_waives_source_and_recipient_checks(change):
    rows=thread()
    if change=='broken_link':rows[1]['in_reply_to']='<unknown>'
    elif change=='duplicate_rfc':rows[0]['rfc_message_id']='<incoming>'
    elif change=='foreign_cc':rows[1]['cc']='outsider@unrelated.invalid'
    elif change=='truncated':rows[0]['truncated']=True
    else:rows[0]['internalDate']='3000'
    with pytest.raises(route.ProjectRoutingReview):route.project_from_thread('code',rows)
