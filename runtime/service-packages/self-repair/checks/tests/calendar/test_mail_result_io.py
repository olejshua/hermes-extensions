"""Native Gmail MIME/update boundaries against a stateful private service."""
import base64
import copy
from email import message_from_bytes,policy as email_policy
from email.message import EmailMessage
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gtm/calendar/runtime'))
import mail_policy as policy
import mail_result_io as result_io

def payload(message):
    value={'mimeType':message.get_content_type(),'headers':[{'name':k,'value':v}for k,v in message.items()]}
    if message.is_multipart():value['parts']=[payload(p)for p in message.iter_parts()]
    else:value['body']={'data':base64.urlsafe_b64encode(message.get_payload(decode=True)).decode()}
    return value

def mail(text,mid,*,draft=False):
    message=EmailMessage();message['From']='Person <person@example.invalid>'if not draft else'operator@secondary.example.invalid'
    message['To']='operator@secondary.example.invalid'if not draft else'person@example.invalid'
    message['Subject']='Stealth Campus Gespräch';message['Message-ID']='<'+mid+'@example.invalid>'
    if draft:message['In-Reply-To']='<incoming@example.invalid>'
    message.set_content(text)
    if draft:message.add_alternative(policy.body_html(text),subtype='html')
    return dict(id=mid,threadId='thread',labelIds=['DRAFT']if draft else['INBOX'],internalDate='1793271600000',payload=payload(message))

class Request:
    def __init__(self,call):self.call=call
    def execute(self):return self.call()

class Gmail:
    def __init__(self):
        self.source=mail('Welche Schritte wollen Sie automatisieren?','incoming')
        self.draft=mail('Vielen Dank.\n\nBeste Grüße\nOle Beispiel','old',draft=True)
        self.updates=0;self.fail_update=False;self.user_edit_on_second_read=False;self.draft_reads=0
    def users(self):return self
    def drafts(self):return self.Drafts(self)
    def messages(self):return self.Messages(self)
    def threads(self):return self.Threads(self)
    class Drafts:
        def __init__(self,svc):self.svc=svc
        def get(self,**kw):
            def read():
                self.svc.draft_reads+=1
                if self.svc.user_edit_on_second_read and self.svc.draft_reads==2:
                    self.svc.draft=mail('Von Alex manuell geändert.','user-edited',draft=True)
                return {'id':'draft','message':copy.deepcopy(self.svc.draft)}
            return Request(read)
        def list(self,**kw):return Request(lambda:{'drafts':[{'id':'draft','message':{'id':self.svc.draft['id']}}]})
        def update(self,**kw):
            assert kw['id']=='draft'
            def write():
                self.svc.updates+=1
                message=message_from_bytes(base64.urlsafe_b64decode(kw['body']['message']['raw']),policy=email_policy.default)
                self.svc.draft={**self.svc.draft,'id':'new','payload':payload(message)}
                if self.svc.fail_update:raise TimeoutError('lost acknowledgement')
                return {'id':'draft','message':copy.deepcopy(self.svc.draft)}
            return Request(write)
    class Messages:
        def __init__(self,svc):self.svc=svc
        def get(self,**kw):return Request(lambda:copy.deepcopy(self.svc.draft if kw['id']==self.svc.draft['id']else self.svc.source))
    class Threads:
        def __init__(self,svc):self.svc=svc
        def get(self,**kw):return Request(lambda:{'id':'thread','messages':copy.deepcopy([self.svc.source,self.svc.draft])})

@pytest.fixture
def native(tmp_path,monkeypatch):
    path=ROOT/'gtm/calendar/runtime/email_watcher.py'
    if not path.exists():path=ROOT/'auth/staging/email_watcher.py'
    monkeypatch.setitem(sys.modules,'google.auth.transport.requests',SimpleNamespace(Request=Mock()))
    monkeypatch.setitem(sys.modules,'google.oauth2.credentials',SimpleNamespace(Credentials=Mock()))
    monkeypatch.setitem(sys.modules,'googleapiclient.discovery',SimpleNamespace(build=Mock()))
    spec=importlib.util.spec_from_file_location('private_outcome_watcher',path)
    watcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(watcher)
    service=Gmail();watcher.service=lambda _:service;watcher.verified_profile=lambda _:'operator@secondary.example.invalid'
    monkeypatch.setattr(policy,'STATE_DIR',tmp_path/'claims')
    monkeypatch.setitem(sys.modules,'email_realtime',SimpleNamespace(calendar_service=Mock(side_effect=AssertionError('no calendar needed'))))
    return watcher,service

def test_revise_updates_exact_id_without_create_send_or_recipient_change(native):
    w,svc=native;old=w.body(svc.draft['payload']);text='Ich möchte die Recherche vereinfachen.\n\nBeste Grüße\nOle Beispiel'
    result=w.revise_draft('stealth','incoming','draft','old',result_io.body_key(old),text,project='stealth')
    assert result['status']=='updated'and result['draftId']=='draft'and svc.updates==1
    assert w.body(svc.draft['payload']).rstrip('\n')==text
    assert w.headers(svc.draft)['to']=='person@example.invalid'

@pytest.mark.parametrize('during_preflight',[False,True])
def test_user_edit_before_update_is_preserved(native,during_preflight):
    w,svc=native;key=result_io.body_key(w.body(svc.draft['payload']))
    if during_preflight:svc.user_edit_on_second_read=True
    else:svc.draft=mail('Von Alex manuell geändert.','user-edited',draft=True)
    with pytest.raises(w.SourceChangedBeforeDraft):
        w.revise_draft('stealth','incoming','draft','old',key,'Neue Antwort.',project='stealth')
    assert svc.updates==0 and 'manuell' in w.body(svc.draft['payload'])

def test_lost_update_ack_retains_exact_saved_result_for_readback(native):
    w,svc=native;key=result_io.body_key(w.body(svc.draft['payload']));svc.fail_update=True
    text='Ich möchte die Recherche vereinfachen.\n\nBeste Grüße\nOle Beispiel'
    with pytest.raises(w.DraftStateUnverified):w.revise_draft('stealth','incoming','draft','old',key,text,project='stealth')
    assert svc.updates==1 and w.body(svc.draft['payload']).rstrip('\n')==text

def test_current_proves_owned_gmail_body_and_never_guesses_calendar(native):
    w,svc=native
    def normalized(m):
        h=w.headers(m)
        return dict(id=m['id'],thread_id='thread',from_=h.get('from'),to=h.get('to'),cc=h.get('cc'),
                    rfc_message_id=h.get('message-id'),in_reply_to=h.get('in-reply-to'),text=w.body(m['payload']),labels=m['labelIds'])
    source=normalized(svc.source);draft=normalized(svc.draft)
    case={'outcome':{'status':'created','draft_id':'draft','body_sha256':result_io.body_key(draft['text'])}}
    runtime=SimpleNamespace(get_thread=lambda *_:([source,draft],False),read_case=lambda _:case)
    facts=result_io.current(dict(account='stealth',id='incoming',threadId='thread'),runtime=runtime,watcher=w)
    assert facts['owned_draft']['draft_id']=='draft'
    assert facts['hard_facts']['saved_in_gmail']and not facts['hard_facts']['calendar_verified']
    svc.draft=mail('Manuelle Änderung.','changed',draft=True)
    draft.update(id='changed',text=w.body(svc.draft['payload']))
    assert result_io.current(dict(account='stealth',id='incoming',threadId='thread'),runtime=runtime,watcher=w)['owned_draft']is None

def test_unknown_write_is_only_adoptable_after_exact_mime_source_and_recipients(native):
    w,svc=native
    def read_thread(*_):
        result=[]
        for m in(svc.source,svc.draft):
            h=w.headers(m);result.append(dict(id=m['id'],thread_id='thread',text=w.body(m['payload']),
                labels=m['labelIds'],rfc_message_id=h.get('message-id'),in_reply_to=h.get('in-reply-to')))
        return result,False
    case=dict(phase='draft_update_pending',prepared_body=w.body(svc.draft['payload']),prepared_slots=[],
              recipients=['person@example.invalid'],revision_of={'draft_id':'draft'})
    runtime=SimpleNamespace(get_thread=read_thread,read_case=lambda _:case)
    facts=result_io.current(dict(account='stealth',id='incoming',threadId='thread'),runtime=runtime,watcher=w)
    assert facts['recoverable_write']['draft_id']=='draft'and svc.updates==0
    case['prepared_body']='Anderer vorbereiteter Text.'
    assert not result_io.current(dict(account='stealth',id='incoming',threadId='thread'),runtime=runtime,watcher=w).get('recoverable_write')
