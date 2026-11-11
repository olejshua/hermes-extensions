"""Private boundaries around the real native generate/draft_one and receipt validator.

No network, model process, Telegram or Gmail writes are permitted by these tests.
"""
import base64
import copy
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'second-brain/src'))
from gtm.coordinator import Coordinator
from gtm.reply import ExistingLeadBinding
from gtm.mail_observer.integration import ExistingDraftRunIntegration
from gtm.mail_observer.sources import FullOriginals, SourceBlocked
from gtm.mail_observer.semantics import SemanticReceipts

CODE = 'operator@primary.example.invalid'
SENDER = 'person@example.invalid'

class Request:
    def __init__(self, value): self.value = value
    def execute(self): return copy.deepcopy(self.value)

class Gmail:
    def __init__(self, body):
        self.rows = {}
        stamp = int(datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp() * 1000)
        self.add('proposal', 'An der CODE University forschen wir zu TED Ausschreibungen.\nFreitag, 23.10.2026, 15:00 Uhr für 15 Minuten.', CODE, SENDER, stamp, ['SENT'])
        self.add('incoming', body, SENDER, CODE, stamp + 60000, [], '<proposal>')
    def add(self, mid, body, sender, recipient, stamp, labels, parent=None):
        message = EmailMessage()
        message['From'], message['To'], message['Message-ID'] = sender, recipient, '<'+mid+'>'
        if parent: message['In-Reply-To'] = parent
        message.set_content(body)
        self.rows[mid] = {'id':mid, 'threadId':'thread', 'labelIds':labels, 'internalDate':str(stamp),
                          'raw':base64.urlsafe_b64encode(message.as_bytes()).decode()}
    def users(self): return self
    def messages(self): return self
    def getProfile(self, **kw): return Request({'emailAddress':CODE})
    def threads(self): return types.SimpleNamespace(get=lambda **kw:Request({'id':'thread','messages':list(self.rows.values())}))
    def get(self, **kw): return Request(self.rows[kw['id']])

class MailAcceptanceHookTests(unittest.TestCase):
    def setUp(self):
        from mail_fixtures import runtime
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);self.r=runtime(self.home)
        self.receipts=SemanticReceipts(Coordinator(self.home/'state.sqlite'))
        self.binding=ExistingLeadBinding('company','source','person')
        self.twenty=types.SimpleNamespace(lookup_existing_sender=lambda _:self.binding)
    def prepared(self, body='Passt, gerne.'):
        from mail_fixtures import message
        gmail=Gmail(body);row=dict(gmail.rows['incoming'],account='code',**{'from':SENDER,'subject':'Austausch'})
        bridge=ExistingDraftRunIntegration(FullOriginals(),self.twenty,self.receipts)
        data=bridge.before(gmail,row)
        self.r._reply_context=lambda *a:(bridge,data)
        _,thread=message(body);self.r.get_thread=lambda *a:(thread,False)
        return bridge,data,row,thread
    def output(self,data,decision='confirmed'):
        quote=data['incoming_original']['body'].strip().splitlines()[0]
        return {'binding':data['binding'],'classification':'interest','quote':quote,'summary':'Der Absender stimmt dem Vorschlag zu.',
                'acceptance':{'decision':decision,'quote':quote,'selected_offer_id':data['offers'][0]['offer_id'] if decision=='confirmed' else None}}
    def test_qualified_reply_preserves_exact_source_bound_semantic_receipt_without_invitation(self):
        from mail_fixtures import interpretation,models
        bridge,data,row,thread=self.prepared()
        models(self.r,interpretation(reply=False),{'body':None,'question':None,'proposed_slot':None,
            'referral_recipients':[],'gtm_reply':self.output(data)})
        result=self.r.draft_one(row)
        self.assertEqual(result['crm_status'],'recorded');self.assertNotEqual(result['status'],'calendar_acceptance_queued')
        self.assertEqual([c.args[0]for c in self.r.model_json.call_args_list],['understand','compose','review'])
        ctx=next(c.args[1]for c in self.r.model_json.call_args_list if c.args[0]=='compose')['crm_context']
        self.assertEqual(ctx['binding']['source_hash'],data['binding']['source_hash'])
        self.r.w.make_draft.assert_not_called()
    def test_semantic_review_does_not_turn_into_false_queued_invitation(self):
        from mail_fixtures import interpretation,models
        _,data,row,_=self.prepared('Gerne, aber nur falls mein Kollege kann.')
        models(self.r,interpretation(reply=False),{'body':None,'question':None,'proposed_slot':None,
            'referral_recipients':[],'gtm_reply':self.output(data,'review')})
        result=self.r.draft_one(row);self.assertEqual(result['crm_status'],'recorded')
        self.assertNotEqual(result['status'],'calendar_acceptance_queued')
    def test_foreign_binding_never_creates_semantic_receipt(self):
        from mail_fixtures import interpretation,models
        _,data,row,_=self.prepared();out=self.output(data);out['binding']=dict(out['binding'],sender='other@example.invalid')
        models(self.r,interpretation(reply=False),{'body':None,'question':None,'proposed_slot':None,'referral_recipients':[],'gtm_reply':out})
        result=self.r.draft_one(row);self.assertEqual(result['crm_status'],'receipt_review')
        with self.receipts.co.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM gtm_reply_semantics').fetchone()[0],0)
    def test_crm_source_failure_does_not_block_useful_ordinary_draft(self):
        from mail_fixtures import interpretation,models
        _,_,row,_=self.prepared('Welche Schritte wollt ihr automatisieren?')
        self.r._reply_context=lambda *a:(_ for _ in ()).throw(SourceBlocked('semantic_input_size_review'))
        models(self.r,interpretation(),{'body':'Vielen Dank. Wir untersuchen den Ablauf der Ausschreibungsrecherche.',
            'question':None,'proposed_slot':None,'referral_recipients':[]})
        result=self.r.draft_one(row);self.assertEqual(result['status'],'created');self.assertEqual(result['crm_status'],'context_unavailable')
        self.assertTrue(result['crm_review_required'])
        state={};self.r.queue_draft_notification(state,row,result)
        encoded=json.dumps(state,ensure_ascii=False)
        self.assertIn('CRM-Zuordnung',encoded);self.assertNotIn('Der Entwurf ist bereit.',encoded)
    def test_complete_original_over_four_thousand_is_preserved(self):
        from mail_fixtures import interpretation,models
        body='Passt, gerne.\n'+'Kontext '*700;_,data,row,_=self.prepared(body)
        models(self.r,interpretation(reply=False),{'body':None,'question':None,'proposed_slot':None,'referral_recipients':[],'gtm_reply':self.output(data)})
        self.r.draft_one(row);ctx=next(c.args[1]for c in self.r.model_json.call_args_list if c.args[0]=='compose')['crm_context']
        self.assertGreater(len(ctx['incoming_original']['body']),4000)
    def test_semantic_full_original_size_gate_is_still_enforced(self):
        bridge,_,row,_=self.prepared()
        with self.assertRaisesRegex(SourceBlocked,'semantic_input_size_review'):bridge.before(Gmail('Passt, gerne.\n'+'Kontext '*15000),row)

if __name__=='__main__':unittest.main()
