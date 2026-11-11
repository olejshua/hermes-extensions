"""Rendering is deterministic; temporal intent is a structured semantic gate."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace
import pytest
ROOT=Path(__file__).resolve().parents[2]
import mail_outcomes as standard

@pytest.fixture
def policy():
 spec=importlib.util.spec_from_file_location('render_only_policy',ROOT/'gtm/calendar/runtime/mail_policy.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

SLOT='2026-11-04T11:45:00+01:00'

@pytest.mark.parametrize('text',[
 'Morgen klappt es bei mir leider nicht. Welches Zeitfenster passt Ihnen nächste Woche für unseren kurzen Austausch?',
 'Morgen bin ich leider verhindert. Wann hätten Sie nächste Woche Zeit für unseren Austausch?',
 'Ihr Angebot ab 11 Uhr kann ich leider nicht wahrnehmen. Wann hätten Sie kommende Woche Zeit?',
 'Unser vorheriger Termin hat leider nicht geklappt. Welche Möglichkeiten haben Sie nächste Woche?',
])
def test_renderer_does_not_guess_the_intent_of_temporal_language(policy,text):
 assert policy.format_body(text).startswith(text)

@pytest.mark.parametrize('text', ['Kein ausgewiesener Beginn.', 'Zweimal {label}. Nochmals {label}.'])
def test_every_declared_start_still_needs_exactly_one_displayed_label(policy,text):
 with pytest.raises(policy.MailPolicyReviewRequired):policy.format_body(text.format(label=policy.label(SLOT)),[SLOT])

def context(body,slots=()):
 return {'thread':[{'text':'Bitte nennen Sie mir einen passenden Termin.'}], 'draft':body,
         'require_time_claims':True,'hard_facts':{'proposed_slots':list(slots)}}

def review(*,starts=(),offer=False,result='ready'):
 return {'reply_required':True,'scheduling_required':True,'result':result,
  'source_quote':'Bitte nennen Sie mir einen passenden Termin.','issues':[],
  'new_time_offer':offer,'proposed_starts':list(starts)}

@pytest.mark.parametrize('starts,offer,declared', [([SLOT],True,[]),([],True,[]),([],False,[SLOT])])
def test_an_unbound_actual_offer_cannot_receive_ready_even_if_model_says_so(starts,offer,declared):
 model=Mock(return_value=review(starts=starts,offer=offer))
 with pytest.raises(standard.OutcomeReviewError,match='outcome_actual_proposals_unverified'):
  standard.assess(context('Ich könnte morgen.',declared),model)
 assert model.call_count==2

def test_correct_complete_claims_remain_valid():
 value=standard.assess(context('Ein belegter konkreter Beginn.',[SLOT]),
  Mock(return_value=review(starts=[SLOT],offer=True)))
 assert value['review']['result']=='ready'

def test_changed_body_never_reuses_an_earlier_semantic_approval():
 first=context('Welche Zeitfenster haben Sie?')
 receipt=standard.assess(first,Mock(return_value=review()))
 model=Mock(return_value=review(starts=[SLOT],offer=True))
 with pytest.raises(standard.OutcomeReviewError):
  standard.assess(context('Ich kann morgen um 11:45 Uhr.'),model,cached=receipt)
 assert model.call_count==2

def test_new_acribic_private_holds_use_code_main_and_existing_holds_stay_put(policy,tmp_path,monkeypatch):
 import copy
 import calendar_routing as routing
 class Missing(Exception):resp=SimpleNamespace(status=404)
 class Calendar:
  def __init__(self):self.records={cid:{}for cid in routing.AVAILABILITY};self.inserts=[]
  def calendarList(self):return self
  def freebusy(self):return self
  def events(self):return self
  def list(self,**kw):
   if 'calendarId'in kw:value=list(self.records[kw['calendarId']].values())
   else:value=[dict(id=routing.CODE,primary=True,accessRole='owner'),
       dict(id=routing.FUXAM,summary='Fuxam Calendar',accessRole='reader'),
       dict(id=routing.ACRIBIC,summary='Acribic',timeZone='Europe/Berlin',accessRole='owner')]
   return SimpleNamespace(execute=lambda:{'items':copy.deepcopy(value)})
  def query(self,body):
   # A busy private hold must appear in freebusy as in the real Calendar API.
   lower,upper=policy.start(body['timeMin']),policy.start(body['timeMax']);values={}
   for item in body['items']:
    busy=[]
    for event in self.records[item['id']].values():
     if event.get('status')=='cancelled'or event.get('transparency')=='transparent':continue
     a,b=policy.start(event['start']['dateTime']),policy.start(event['end']['dateTime'])
     if a<upper and b>lower:busy.append({'start':max(a,lower).isoformat(),'end':min(b,upper).isoformat()})
    values[item['id']]={'busy':busy}
   return SimpleNamespace(execute=lambda:{'calendars':copy.deepcopy(values)})
  def get(self,*,calendarId,eventId):
   def read():
    if eventId not in self.records[calendarId]:raise Missing()
    return copy.deepcopy(self.records[calendarId][eventId])
   return SimpleNamespace(execute=read)
  def insert(self,*,calendarId,body,sendUpdates):
   assert sendUpdates=='none'and body['visibility']=='private'and not body.get('attendees')
   def write():self.records[calendarId][body['id']]=copy.deepcopy(body);self.inserts.append(calendarId);return body
   return SimpleNamespace(execute=write)
 monkeypatch.setattr(policy,'STATE_DIR',tmp_path/'claims');calendar=Calendar()
 with policy.scheduling(calendar,'code','private-source',['person@example.invalid'],[SLOT],project='acribic')as ids:key=ids[0]
 assert calendar.inserts==[routing.CODE]and key in calendar.records[routing.CODE]
 # A pre-existing reservation in the old target remains the same reservation.
 calendar.records[routing.ACRIBIC][key]=calendar.records[routing.CODE].pop(key)
 claims=policy._read();claims[key]['calendar_id']=routing.ACRIBIC;policy._save(claims)
 with policy.scheduling(calendar,'code','private-source',['person@example.invalid'],[SLOT],project='acribic')as ids:assert ids==[key]
 assert calendar.inserts==[routing.CODE]and key in calendar.records[routing.ACRIBIC]and not calendar.records[routing.CODE]
