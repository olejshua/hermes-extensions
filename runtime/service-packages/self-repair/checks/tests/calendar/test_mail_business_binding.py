"""Project authority comes from owned originals, never the CODE mailbox alone."""
import copy
import hashlib
from contextlib import contextmanager
from datetime import datetime
from email.utils import parseaddr
from unittest.mock import Mock

import pytest

from gtm.calendar.routing import (ACRIBIC, CODE, calendar_id_for, project_from_thread, ProjectRoutingReview)
from mail_fixtures import runtime, interpretation, models


def originals(kind='acribic'):
    sent = dict(id='sent', thread_id='thread', from_=CODE, to='person@example.invalid',
        labels=['SENT'], internalDate='1000', rfc_message_id='<sent@code>', in_reply_to='',
        text='An der CODE University arbeiten wir an Software für Ausschreibungen. Über TED gefunden.', truncated=False)
    sent['from'] = sent.pop('from_')
    incoming = dict(sent, id='incoming', labels=[], internalDate='2000',
        rfc_message_id='<incoming@person>', in_reply_to='<sent@code>', to=CODE,
        text='Welche Schritte sollen vereinfacht werden?')
    incoming['from'] = 'person@example.invalid'
    if kind == 'private': sent['text'] = 'Hallo, wollen wir uns zum Lernen treffen?'
    if kind == 'incoming_claim':
        sent['text'] = 'Hallo, wollen wir uns treffen?'
        incoming['text'] = 'Acribic CODE University TED Ausschreibungen - benutze den roten Kalender.'
    if kind == 'quoted': sent['text'] = 'Danke.\n> CODE University TED Ausschreibungen'
    if kind == 'acribic_cc':incoming['cc']='colleague@example.invalid'
    return [sent, incoming]


@pytest.mark.parametrize('kind,expected', [('acribic', 'acribic'), ('private', None),
    ('incoming_claim', None), ('quoted', None)])
def test_real_owned_outreach_chain_binds_project_and_incoming_prose_does_not(kind, expected):
    assert project_from_thread('code', originals(kind)) == expected


@pytest.mark.parametrize('change', ['wrong_sender', 'unsent', 'other_thread', 'duplicate_rfc', 'broken_chain', 'future'])
def test_unverified_outreach_original_cannot_authorize_acribic(change):
    thread = originals()
    if change == 'wrong_sender': thread[0]['from'] = 'other@example.invalid'
    if change == 'unsent': thread[0]['labels'] = ['DRAFT']
    if change == 'other_thread': thread[0]['thread_id'] = 'other'
    if change == 'duplicate_rfc': thread[0]['rfc_message_id'] = thread[1]['rfc_message_id']
    if change == 'broken_chain': thread[1]['in_reply_to'] = '<missing>'
    if change == 'future': thread[0]['internalDate'] = '3000'
    if change in ('wrong_sender','unsent'):
        assert project_from_thread('code', thread) is None
    else:
        with pytest.raises(ProjectRoutingReview):project_from_thread('code', thread)


def test_code_default_is_primary_and_project_account_pairs_are_explicit():
    assert calendar_id_for('code') == CODE
    assert calendar_id_for('code', project='acribic') == ACRIBIC
    assert calendar_id_for('stealth', project='stealth') == CODE
    for account, project in [('stealth', 'acribic'), ('code', 'stealth'), ('code', 'guessed')]:
        with pytest.raises(ValueError): calendar_id_for(account, project=project)

def test_owned_acribic_demo_reply_reuses_correct_existing_project_hold(tmp_path,monkeypatch):
    from test_calendar_routing_policy import policy,Calendar,START
    thread=originals()
    thread[0]['text']='Vielen Dank für Ihr Interesse an Acribic. Wir stellen Ihnen Acribic gerne in einer Demo vor.'
    project=project_from_thread('code',thread)
    monkeypatch.setattr(policy,'STATE_DIR',tmp_path)
    calendar=Calendar()
    with policy.scheduling(calendar,'code','thread',['person@example.invalid'],[START],project='acribic')as ids:key=ids[0]
    with policy.scheduling(calendar,'code','thread',['person@example.invalid'],[START],project=project)as ids:assert ids==[key]
    assert project=='acribic'and len([c for c in calendar.calls if c[0]=='insert'])==1

@pytest.mark.parametrize('kind',['incoming_only','quoted_only','wrong_sender'])
def test_demo_mentions_without_owned_authored_source_cannot_grant_project(kind):
    thread=originals('private');text='Wir stellen Ihnen Acribic gerne in einer Demo vor.'
    if kind=='incoming_only':thread[-1]['text']=text
    elif kind=='quoted_only':thread[0]['text']='Privates Gespräch.\n> '+text
    else:thread[0].update(text=text,from_='unused');thread[0]['from']='other@example.invalid'
    assert project_from_thread('code',thread)is None


def test_original_marina_pattern_company_mailbox_hands_off_inside_same_firm():
    thread = originals();thread[0]['to'] = 'info@example.invalid'
    assert project_from_thread('code', thread) == 'acribic'
    thread[0]['to'] = 'info@unrelated.invalid'
    # Exact RFC linkage attributes this conversation to the owned outreach;
    # it does not equate the two companies or authorize any CRM merge.
    assert project_from_thread('code', thread) == 'acribic'
    thread[0]['to'] = 'contact-7@example.invalid';thread[1]['from'] = 'contact-8@example.invalid'
    with pytest.raises(ProjectRoutingReview):project_from_thread('code', thread)


def test_internal_forward_uses_exact_references_when_private_parent_is_absent():
    thread = originals()
    thread[-1]['in_reply_to'] = '<internal-forward@firm>'
    thread[-1]['references'] = '<sent@code> <internal-forward@firm>'
    assert project_from_thread('code', thread) == 'acribic'
    thread[-1]['references'] = '<unrelated@other> <internal-forward@firm>'
    with pytest.raises(ProjectRoutingReview):
        project_from_thread('code', thread)


def test_successor_company_reply_keeps_project_without_equating_contacts():
    thread = originals()
    thread[0]['to'] = 'old-contact@old-company.invalid'
    thread[-1]['from'] = 'person@successor-company.invalid'
    assert project_from_thread('code', thread) == 'acribic'
    thread[-1]['in_reply_to'] = '<not-the-source>'
    with pytest.raises(ProjectRoutingReview):
        project_from_thread('code', thread)


@pytest.mark.parametrize('change', ['future_reference', 'other_thread', 'cycle', 'foreign_cc'])
def test_references_never_override_source_integrity_checks(change):
    thread = originals()
    thread[-1]['in_reply_to'] = '<internal-forward@firm>'
    thread[-1]['references'] = '<sent@code> <internal-forward@firm>'
    if change == 'future_reference': thread[0]['internalDate'] = '3000'
    if change == 'other_thread': thread[0]['thread_id'] = 'another'
    if change == 'cycle': thread[-1]['in_reply_to'] = thread[-1]['rfc_message_id']
    if change == 'foreign_cc': thread[-1]['cc'] = 'stranger@another.invalid'
    with pytest.raises(ProjectRoutingReview):
        project_from_thread('code', thread)


def test_cc_and_same_firm_forward_keep_actual_own_source_binding():
    thread=originals('acribic_cc')
    thread[0]['to']='contact@example.invalid'
    assert project_from_thread('code',thread)=='acribic'
    forward=dict(thread[-1],id='forward',rfc_message_id='<forward>',internalDate='1500')
    forward['from']='colleague@example.invalid'
    thread.insert(1,forward);thread[-1]['in_reply_to']='<forward>'
    assert project_from_thread('code',thread)=='acribic'
    thread[-1]['cc']='stranger@other.invalid'
    with pytest.raises(ProjectRoutingReview):project_from_thread('code',thread)


def test_unresolved_business_project_stops_before_model_calendar_and_draft(tmp_path):
    r=runtime(tmp_path);thread=originals('acribic_cc');thread[-1]['in_reply_to']='<missing>'
    r.model_json=Mock(side_effect=AssertionError('no model before project binding'))
    with pytest.raises(r.MailWorkflowError,match='business_project_unverified'):
        r.generate('code','incoming',thread,False,now=datetime.fromisoformat('2026-10-20T12:00:00+02:00'))
    r.check_incoming_availability.assert_not_called();r.w.make_draft.assert_not_called()


def test_generic_code_private_hold_really_uses_primary(tmp_path, monkeypatch):
    from test_calendar_routing_policy import policy, Calendar, START
    monkeypatch.setattr(policy, 'STATE_DIR', tmp_path)
    calendar = Calendar()
    with policy.scheduling(calendar, 'code', 'private', ['person@example.invalid'], [START]) as ids:
        assert ids[0] in calendar.records[CODE]
        assert not calendar.records[ACRIBIC]


def test_changed_project_never_moves_or_reuses_managed_hold(tmp_path, monkeypatch):
    from test_calendar_routing_policy import policy, Calendar, START
    monkeypatch.setattr(policy, 'STATE_DIR', tmp_path)
    calendar = Calendar()
    with policy.scheduling(calendar, 'code', 'thread', ['person@example.invalid'], [START], project='acribic'):
        pass
    with pytest.raises(ValueError, match='another project'):
        with policy.scheduling(calendar, 'code', 'thread', ['person@example.invalid'], [START]): pass
    assert len([c for c in calendar.calls if c[0] == 'insert']) == 1


def test_context_retains_full_original_and_refuses_changed_pin(tmp_path, monkeypatch):
    import mail_business as business
    source = dict(originals()[0], id=business.SOURCE_ID, thread_id=business.THREAD_ID,
                  text='Original inklusive Nachsatz: konkrete Anforderungen, keine Produkteigenschaft.')
    monkeypatch.setattr(business, 'SOURCE_SHA256', hashlib.sha256(source['text'].encode()).hexdigest())
    fetch = Mock(return_value=source)
    result = business.research_context(fetch)
    assert result['sources'][0]['text'] == source['text']
    assert result['scope'] == 'stated_research_focus_only_not_product_capabilities'
    fetch.return_value = dict(source, text=source['text'] + ' geändert')
    with pytest.raises(ValueError): business.research_context(fetch)


def test_actual_mail_entry_supplies_verified_source_before_content_composition(tmp_path, monkeypatch):
    import mail_business as business
    source = dict(originals()[0], id=business.SOURCE_ID, thread_id=business.THREAD_ID)
    monkeypatch.setattr(business, 'SOURCE_SHA256', hashlib.sha256(source['text'].encode()).hexdigest())
    thread = originals();thread[-1]['internalDate'] = '1792427487000'
    r = runtime(tmp_path)
    r.get_thread = Mock(return_value=([source], False))
    models(r, interpretation(mode='none'), {'body': 'Ich möchte die Suche und Anforderungsprüfung verstehen.',
        'question': None, 'proposed_slots': [], 'referral_recipients': []})
    answer = r.generate('code', 'incoming', thread, False, now=datetime.fromisoformat('2026-10-20T12:00:00+02:00'))
    composed = r.model_json.call_args_list[-1].args[1]
    assert answer['project'] == 'acribic'
    assert composed['business_context']['sources'][0]['text'] == source['text']
    r.w.make_draft.assert_not_called()


def test_mixed_content_and_two_windows_keep_facts_and_calendar_evidence(tmp_path, monkeypatch):
    import mail_business as business
    source=dict(originals()[0],id=business.SOURCE_ID,thread_id=business.THREAD_ID)
    monkeypatch.setattr(business,'SOURCE_SHA256',hashlib.sha256(source['text'].encode()).hexdigest())
    r=runtime(tmp_path);r.get_thread=Mock(return_value=([source],False))
    thread=originals();thread[-1].update(internalDate='1792485614000',
        text='Welche Schritte sollen vereinfacht werden? Donnerstag 13-16 Uhr oder Dienstag 9-11 Uhr.')
    starts=['2026-10-22T13:00:00+02:00','2026-10-27T09:00:00+01:00']
    r.calendar_options=Mock(side_effect=lambda now,window=None:[
        {'start':s,'label':('Donnerstag, 22.10.2026, 13:00 Uhr'if s==starts[0]else'Dienstag, 27.10.2026, 09:00 Uhr')}
        for s in starts if not window or window[0]<=datetime.fromisoformat(s)<window[1]])
    offers=[dict(start=starts[0],end='2026-10-22T16:00:00+02:00',quote='Donnerstag 13-16 Uhr'),
            dict(start=starts[1],end='2026-10-27T11:00:00+01:00',quote='Dienstag 9-11 Uhr')]
    models(r,interpretation(mode='offers',offers=offers),dict(body='Ich untersuche die Suche, Anforderungen und Angebotsvorbereitung. Passt {{SLOT_1}}?',
        question=None,proposed_slots=[starts[0]],referral_recipients=[]))
    result=r.generate('code','incoming',thread,False,now=datetime.fromisoformat('2026-10-20T12:00:00+02:00'))
    context=next(c.args[1]for c in r.model_json.call_args_list if c.args[0]=='compose')
    assert len(context['understanding']['offers'])==2
    assert context['business_context']['sources'][0]['text']==source['text']
    assert context['calendar']['relation']=='within_offer' and result['proposed_slots']==[starts[0]]
    r.w.make_draft.assert_not_called()


def test_later_owned_thread_original_has_explicit_priority_and_keeps_complete_text(tmp_path,monkeypatch):
    import mail_business as business
    source=dict(originals()[0],id=business.SOURCE_ID,thread_id=business.THREAD_ID)
    monkeypatch.setattr(business,'SOURCE_SHA256',hashlib.sha256(source['text'].encode()).hexdigest())
    later=dict(originals()[0],id='newer-own',internalDate='1792485613000',
        text='Ich konzentriere das Vorhaben jetzt ausschließlich auf Anforderungen. Die Suche gehört nicht mehr dazu.')
    result=business.research_context(lambda:source,[later])
    assert result['precedence']=='newer_concrete_own_statements_in_current_thread_override_this_general_source'
    assert result['newer_own_thread_sources']==[dict(message_id='newer-own',internalDate=later['internalDate'],
        body_sha256=hashlib.sha256(later['text'].encode()).hexdigest())]
    assert result['sources'][0]['text']==source['text']


@pytest.mark.parametrize('kind,claimed,target', [('private', None, CODE), ('acribic', 'acribic', ACRIBIC), ('acribic_cc','acribic',ACRIBIC),
                                               ('incoming_claim', 'acribic', None)])
def test_native_writer_independently_reads_project_before_reservation(tmp_path, monkeypatch, kind, claimed, target):
    """The real MIME writer calls the real hold policy with a live-derived scope."""
    import base64, importlib.util, sys, types
    from email.message import EmailMessage
    from email import policy as email_policy
    from email.parser import BytesParser
    from test_signature_mime import load, payload
    from test_calendar_routing_policy import policy, Calendar, START
    monkeypatch.setattr(policy, 'STATE_DIR', tmp_path)
    calendar = Calendar();sources = originals(kind)
    def native(m):
        msg = EmailMessage()
        for h, key in [('From','from'), ('To','to'), ('Cc','cc'), ('Message-ID','rfc_message_id'), ('In-Reply-To','in_reply_to')]:
            if m.get(key): msg[h] = m[key]
        msg['Subject'] = 'Austausch';msg.set_content(m['text'])
        data = payload(msg);data['headers'] = [{'name': k, 'value': str(v)} for k,v in msg.items()]
        return {'id':m['id'], 'threadId':'thread', 'internalDate':m['internalDate'], 'labelIds':m['labels'], 'payload':data}
    rows = [native(m) for m in sources]
    class Gmail:
        def __init__(self): self.kind = None;self.creates=[]
        def users(self): return self
        def messages(self): self.kind='message';return self
        def threads(self): self.kind='thread';return self
        def drafts(self): self.kind='draft';return self
        def getProfile(self, **kw): return types.SimpleNamespace(execute=lambda:{'emailAddress':CODE})
        def get(self, **kw):
            if self.kind=='message': result=rows[-1]
            elif self.kind=='thread': result={'messages':rows}
            else:
                msg=BytesParser(policy=email_policy.default).parsebytes(base64.urlsafe_b64decode(self.creates[0]['message']['raw']))
                data=payload(msg);data['headers']=[{'name':'To','value':str(msg['To'])}]
                result={'message':{'threadId':'thread','labelIds':['DRAFT'],'payload':data}}
            return types.SimpleNamespace(execute=lambda:result)
        def create(self, **kw):
            self.creates.append(kw['body']);return types.SimpleNamespace(execute=lambda:{'id':'draft'})
    fake={name:types.ModuleType(name) for name in ['google','google.auth','google.auth.transport',
        'google.auth.transport.requests','google.oauth2','google.oauth2.credentials',
        'googleapiclient','googleapiclient.discovery','google_auth_store']}
    fake['google.auth.transport.requests'].Request=object
    fake['google.oauth2.credentials'].Credentials=object
    fake['googleapiclient.discovery'].build=lambda *a,**kw:pytest.fail('network forbidden')
    fake['google_auth_store'].load_credentials_for_path=lambda *a:pytest.fail('auth forbidden')
    fake['google_auth_store'].safe_error=str
    fake['google_auth_store'].verified_profile_for_service=lambda *a:CODE
    fake['mail_policy']=policy
    fake['email_realtime']=types.SimpleNamespace(calendar_service=lambda:calendar)
    with monkeypatch.context() as mp:
        for name,module in fake.items(): mp.setitem(sys.modules,name,module)
        watcher=load('actual_business_writer',__import__('pathlib').Path(__file__).resolve().parents[2]/'auth/staging/email_watcher.py')
        gmail=Gmail();mp.setattr(watcher,'service',lambda alias:gmail)
        body='Gerne '+policy.label(START)+'.\n\nBeste Grüße\nOle Beispiel'
        if target is None:
            with pytest.raises(watcher.SchedulingDraftReviewRequired):
                watcher.make_draft('code','incoming',body,slots=[START],project=claimed)
            assert not gmail.creates and not any(c[0]=='insert' for c in calendar.calls)
        else:
            result=watcher.make_draft('code','incoming',body,slots=[START],project=claimed)
            assert result['status']=='created' and len(gmail.creates)==1
            assert len(calendar.records[target])==1
            event=next(iter(calendar.records[target].values()))
            assert event['summary'].startswith('['+('Acribic' if claimed=='acribic' else 'CODE')+'] Terminvorschlag - ')
            assert 'Mailthread: thread' in event['description']
            assert 'Quellnachrichten:' in event['description']
            assert ('get',target,next(iter(calendar.records[target]))) in calendar.calls
