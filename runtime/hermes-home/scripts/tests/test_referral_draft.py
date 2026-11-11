import base64
import json
import sys
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import email_realtime as realtime
import email_watcher as watcher


class ReferralTests(unittest.TestCase):
    def test_addressing_checks_original_cc_and_sender(self):
        cc = 'Robin <robin@example.com>, Till <till@example.com>'
        to, copied = realtime.referral_addressing(cc, ['till@example.com', 'robin@example.com'],
                                                  'Nicolas <nicolas@example.com>')
        self.assertEqual(to, ['till@example.com', 'robin@example.com'])
        self.assertEqual(copied, ['nicolas@example.com'])
        with self.assertRaises(ValueError):
            realtime.referral_addressing(cc, ['outsider@example.com'], 'nicolas@example.com')

    def test_thread_and_heuristic_expose_explicit_referral(self):
        svc = MagicMock()
        svc.users().threads().get().execute.return_value = {'messages': [{
            'id': 'incoming', 'labelIds': ['INBOX'], 'payload': {'headers': [
                {'name': 'From', 'value': 'nicolas@example.com'},
                {'name': 'Cc', 'value': 'robin@example.com, till@example.com'},
                {'name': 'Subject', 'value': 'Verbindung'}],
                'mimeType': 'text/plain',
                'body': {'data': base64.urlsafe_b64encode(b'Ich empfehle beide Kollegen in CC.').decode()}}}]}
        with patch.object(watcher, 'service', return_value=svc):
            thread, omitted = realtime.get_thread('stealth', 'thread-1')
        self.assertFalse(omitted)
        self.assertIn('robin@example.com', thread[-1]['cc'])
        self.assertTrue(realtime.draft_worthy({'id': 'incoming', 'subject': 'Verbindung'}, thread))

    def test_model_output_routes_one_joint_draft(self):
        thread = [{'id': 'incoming', 'labels': ['INBOX'], 'from': 'nicolas@example.com',
                   'to': 'operator@secondary.example.invalid', 'cc': 'robin@example.com, till@example.com',
                   'subject': 'Verbindung', 'text': 'Ich empfehle beide Kollegen in CC.', 'truncated': False}]
        answer = {'body': 'Hallo zusammen', 'priority': 'normal', 'incoming_summary': 'Kollegen empfohlen',
                  'company': None, 'question': None,
                  'referral_recipients': ['robin@example.com', 'till@example.com']}
        seen = {}
        def fake_run(args, **kwargs):
            seen['prompt'] = Path(args[3]).read_text()
            return SimpleNamespace(returncode=0, stdout=json.dumps(answer), stderr='')
        with patch.object(realtime.subprocess, 'run', side_effect=fake_run):
            parsed = realtime.generate('stealth', 'incoming', thread, False)
        self.assertEqual(parsed['referral_recipients'], answer['referral_recipients'])
        self.assertIn('CC', seen['prompt'])
        with patch.object(realtime, 'get_thread', return_value=(thread, False)), \
             patch.object(realtime, 'draft_worthy', return_value=True), \
             patch.object(realtime, 'generate', return_value=parsed), \
             patch.object(watcher, 'make_draft', return_value={
                 'status': 'created', 'to': answer['referral_recipients']}) as make:
            result = realtime.draft_one({'account': 'stealth', 'id': 'incoming', 'threadId': 'thread-1'})
        self.assertEqual(result['to'], answer['referral_recipients'])
        make.assert_called_once_with('stealth', 'incoming', 'Hallo zusammen\n\nBeste Grüße\nOle Beispiel',
                                     to=answer['referral_recipients'], cc=['nicolas@example.com'])

    def test_watcher_creates_one_threaded_draft_not_send(self):
        svc = MagicMock()
        svc.users().messages().get().execute.return_value = {'threadId': 'thread-1',
            'payload': {'headers': [{'name': 'From', 'value': 'nicolas@example.com'},
                                   {'name': 'Cc', 'value': 'robin@example.com, till@example.com'},
                                   {'name': 'Subject', 'value': 'Introduction'},
                                   {'name': 'Message-ID', 'value': '<original@example.com>'}]}}
        svc.users().threads().get().execute.return_value = {'messages': []}
        captured = {}
        def create_draft(**kwargs):
            captured.update(kwargs['body']['message'])
            return SimpleNamespace(execute=lambda: {'id': 'draft-1'})
        svc.users().drafts().create.side_effect = create_draft
        def readback(**kwargs):
            msg = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(captured['raw']))
            raw = msg.get_body(preferencelist=('plain',)).get_content().encode()
            return SimpleNamespace(execute=lambda: {'message': {
                'threadId': 'thread-1', 'labelIds': ['DRAFT'],
                'payload': {'headers': [{'name': k, 'value': v} for k, v in msg.items()],
                            'mimeType': 'text/plain',
                            'body': {'data': base64.urlsafe_b64encode(raw).decode()}}}})
        svc.users().drafts().get.side_effect = readback
        with patch.object(watcher, 'service', return_value=svc):
            result = watcher.make_draft('stealth', 'incoming', 'Hallo zusammen',
                                        to=['robin@example.com', 'till@example.com'],
                                        cc=['nicolas@example.com'])
        self.assertEqual(result['status'], 'created')
        self.assertEqual(result['to'], ['robin@example.com', 'till@example.com'])
        self.assertEqual(captured['threadId'], 'thread-1')
        self.assertEqual(svc.users().drafts().create.call_count, 1)
        saved = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(captured['raw']))
        self.assertTrue(saved.get_content().strip().endswith('Beste Grüße\nOle Beispiel'))
        svc.users().messages().send.assert_not_called()

    def test_plain_reply_without_cc_passes_draft_readback(self):
        svc = MagicMock()
        svc.users().messages().get().execute.return_value = {'threadId': 'thread-1',
            'payload': {'headers': [{'name': 'From', 'value': 'burkhard@example.com'},
                                   {'name': 'Subject', 'value': 'AW: Update'},
                                   {'name': 'Message-ID', 'value': '<original@example.com>'}]}}
        svc.users().threads().get().execute.return_value = {'messages': []}
        captured = {}
        def create_draft(**kwargs):
            captured.update(kwargs['body']['message'])
            return SimpleNamespace(execute=lambda: {'id': 'draft-plain'})
        svc.users().drafts().create.side_effect = create_draft
        def readback(**kwargs):
            msg = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(captured['raw']))
            return SimpleNamespace(execute=lambda: {'message': {
                'threadId': 'thread-1', 'labelIds': ['DRAFT'],
                'payload': {'headers': [{'name': k, 'value': v} for k, v in msg.items()],
                            'mimeType': 'text/plain',
                            'body': {'data': base64.urlsafe_b64encode(msg.get_content().encode()).decode()}}}})
        svc.users().drafts().get.side_effect = readback
        with patch.object(watcher, 'service', return_value=svc):
            result = watcher.make_draft('code', 'incoming', 'Hallo Herr Ritz')
        self.assertEqual(result['status'], 'created')
        saved = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(captured['raw']))
        self.assertTrue(saved.get_content().strip().endswith('Beste Grüße\nOle Beispiel'))
        svc.users().messages().send.assert_not_called()

    def test_draft_signature_is_always_immediately_followed_by_name(self):
        self.assertEqual(watcher.canonical_draft_signature('Hallo Herr Ritz,\n\ndanke.\n\nBeste Grüße'),
                         'Hallo Herr Ritz,\n\ndanke.\n\nBeste Grüße\nOle Beispiel')
        self.assertEqual(watcher.canonical_draft_signature('Hallo Lars,\n\ndanke dir.\n\nBeste Grüße'),
                         'Hallo Lars,\n\ndanke dir.\n\nBeste Grüße\nOle')
        self.assertEqual(watcher.canonical_draft_signature('Hallo Lars,\n\ndanke.\n\nBeste Grüße\nOle'),
                         'Hallo Lars,\n\ndanke.\n\nBeste Grüße\nOle')

    def test_cold_outreach_requires_full_name_even_with_informal_wording(self):
        self.assertEqual(watcher.canonical_draft_signature('Hallo Julia,\n\nWäre das für dich interessant?\n\nBeste Grüße', force_formal=True),
                         'Hallo Julia,\n\nWäre das für dich interessant?\n\nBeste Grüße\nOle Beispiel')

    def test_draft_signature_removes_duplicate_after_crlf(self):
        text = 'Sehr geehrte Frau Zausch,\r\n\r\ndanke.\r\n\r\nBeste Grüße\r\nOle Beispiel\r\n\r\nOle Beispiel'
        self.assertEqual(watcher.canonical_draft_signature(text),
                         'Sehr geehrte Frau Zausch,\n\ndanke.\n\nBeste Grüße\nOle Beispiel')

    def test_draft_signature_preserves_formal_salutation_when_informal_name_provided(self):
        self.assertEqual(watcher.canonical_draft_signature('Sehr geehrter Herr Ritz,\n\ndanke.\n\nBeste Grüße\nOle'),
                         'Sehr geehrter Herr Ritz,\n\ndanke.\n\nBeste Grüße\nOle Beispiel')

    def test_notification_is_explicitly_draft_only(self):
        state = {'notifications': []}
        realtime.queue_draft_notification(state, {'from': 'Nicolas <nicolas@example.com>'},
                                          {'status': 'created', 'incoming_summary': 'zwei Kontakte empfohlen',
                                           'company': None, 'to': ['robin@example.com', 'till@example.com']})
        self.assertIn('robin@example.com', state['notifications'][0])
        self.assertIn('till@example.com', state['notifications'][0])
        self.assertIn('Nicht versendet', state['notifications'][0])

if __name__ == '__main__':
    unittest.main()
