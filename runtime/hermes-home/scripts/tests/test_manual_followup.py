"""Manual follow-up: exact recipient, complete MIME readback and no sends/replay."""
import base64
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import email_watcher as watcher


def payload(message):
    node = {'mimeType': message.get_content_type(), 'headers': [{'name': k, 'value': v} for k, v in message.items()]}
    if message.is_multipart():
        node['parts'] = [payload(part) for part in message.iter_parts()]
    else:
        node['body'] = {'data': base64.urlsafe_b64encode(message.get_payload(decode=True)).decode()}
    return node


class ManualFollowupTests(TestCase):
    def source(self, *, labels=('SENT',), sender='Alex <operator@primary.example.invalid>',
               to='Jennifer <contact-4@customer.example.invalid>', cc=None, bcc=None):
        fields = [('From', sender), ('To', to), ('Subject', 'AW: Hochsauerlandkreis'), ('Message-ID', '<contact-5@primary.example.invalid>')]
        if cc is not None: fields.append(('Cc', cc))
        if bcc is not None: fields.append(('Bcc', bcc))
        return {'id': 'sent', 'threadId': 'thread', 'labelIds': list(labels),
                'payload': {'headers': [{'name': k, 'value': v} for k, v in fields]}}

    def mailbox(self, alias='code', original=None, *, readback='full'):
        owner = {'code': 'operator@primary.example.invalid', 'stealth': 'operator@secondary.example.invalid'}[alias]
        original = original or self.source(sender='Alex <' + owner + '>')
        svc = MagicMock()
        svc.users().messages().get().execute.return_value = original
        svc.users().threads().get().execute.return_value = {'messages': [original]}
        svc.users().getProfile().execute.return_value = {'emailAddress': owner}
        capture = {}
        def created(**kwargs):
            capture.update(kwargs['body']['message'])
            return SimpleNamespace(execute=lambda: {'id': 'draft-id'})
        svc.users().drafts().create.side_effect = created
        def saved(**kwargs):
            message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(capture['raw']))
            node = payload(message)
            if readback == 'plain_only':
                node = payload(message.get_body(preferencelist=('plain',)))
                node['headers'] = [{'name': k, 'value': v} for k, v in message.items()]
            if readback == 'wrong_to':
                node['headers'] = [{'name': k, 'value': 'wrong@example.com' if k.lower() == 'to' else v} for k, v in message.items()]
            return SimpleNamespace(execute=lambda: {'message': {'id': 'draft-message', 'threadId': 'thread', 'labelIds': ['DRAFT'], 'payload': node}})
        svc.users().drafts().get.side_effect = saved
        return svc, capture

    def test_followup_in_both_accounts_has_exact_recipient_thread_mime_and_no_send(self):
        for alias in ('code', 'stealth'):
            with self.subTest(account=alias):
                svc, capture = self.mailbox(alias)
                with patch.object(watcher, 'service', return_value=svc):
                    value = watcher.make_draft(alias, 'sent', 'Guten Tag Frau Weigel,\n\n'
                        'ich würde unseren Austausch gerne nachholen. Wann passt es Ihnen?\n\n'
                        'Beste Grüße\nOle Beispiel', followup_to_last_sent=True)
                self.assertEqual(value['status'], 'created')
                self.assertEqual(value['to'].lower(), 'contact-4@customer.example.invalid')
                self.assertEqual(capture['threadId'], 'thread')
                message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(capture['raw']))
                self.assertEqual(message['To'].addresses[0].addr_spec.lower(), 'contact-4@customer.example.invalid')
                self.assertEqual(message['In-Reply-To'], '<contact-5@primary.example.invalid>')
                self.assertTrue(message.is_multipart())
                self.assertIn('Beste Grüße\nOle Beispiel', message.get_body(preferencelist=('plain',)).get_content())
                self.assertIn('Beste Grüße<br>', message.get_body(preferencelist=('html',)).get_content())
                svc.users().messages().send.assert_not_called()
                svc.users().drafts().send.assert_not_called()

    def test_incomplete_or_wrong_readback_is_uncertain_never_a_second_create(self):
        for mode in ('plain_only', 'wrong_to'):
            with self.subTest(readback=mode):
                svc, _ = self.mailbox(readback=mode)
                with patch.object(watcher, 'service', return_value=svc):
                    with self.assertRaises(watcher.DraftStateUnverified):
                        watcher.make_draft('code', 'sent', 'Guten Tag,\n\nNeuer Termin?\n\nBeste Grüße\nOle Beispiel', followup_to_last_sent=True)
                self.assertEqual(svc.users().drafts().create.call_count, 1)
                svc.users().messages().send.assert_not_called()
                svc.users().drafts().send.assert_not_called()

    def test_wrong_source_or_multiple_recipients_blocks_before_write(self):
        for original in (self.source(labels=('INBOX',)), self.source(sender='other@example.com'),
                         self.source(to='a@example.com, b@example.com'), self.source(cc='third@example.com'),
                         self.source(bcc='hidden@example.com'), self.source(to='operator@primary.example.invalid')):
            with self.subTest(source=original):
                svc, _ = self.mailbox(original=original)
                with patch.object(watcher, 'service', return_value=svc):
                    with self.assertRaises(ValueError):
                        watcher.make_draft('code', 'sent', 'test', followup_to_last_sent=True)
                svc.users().drafts().create.assert_not_called()

    def test_newer_reply_or_sent_message_blocks_old_source(self):
        for labels in (['INBOX'], ['SENT']):
            svc, _ = self.mailbox()
            svc.users().threads().get().execute.return_value = {'messages': [self.source(), {'id': 'new', 'labelIds': labels}]}
            with patch.object(watcher, 'service', return_value=svc):
                with self.assertRaises(watcher.SourceChangedBeforeDraft):
                    watcher.make_draft('code', 'sent', 'test', followup_to_last_sent=True)
            svc.users().drafts().create.assert_not_called()

    def test_existing_draft_is_retained_without_creating_another(self):
        svc, _ = self.mailbox()
        svc.users().threads().get().execute.return_value = {'messages': [self.source(), {'id': 'existing', 'labelIds': ['DRAFT']}]}
        with patch.object(watcher, 'service', return_value=svc):
            result = watcher.make_draft('code', 'sent', 'test', followup_to_last_sent=True)
        self.assertEqual(result['status'], 'draft_already_in_thread')
        svc.users().drafts().create.assert_not_called()

    def test_default_reply_to_own_sent_mail_explains_existing_manual_mode(self):
        svc, _ = self.mailbox()
        with patch.object(watcher, 'service', return_value=svc):
            with self.assertRaisesRegex(ValueError, '--followup-to-last-sent'):
                watcher.make_draft('code', 'sent', 'test')
        svc.users().drafts().create.assert_not_called()

    def test_wrong_authenticated_account_blocks_before_create(self):
        svc, _ = self.mailbox()
        svc.users().getProfile().execute.return_value = {'emailAddress': 'wrong@example.com'}
        with patch.object(watcher, 'service', return_value=svc):
            with self.assertRaises(ValueError):
                watcher.make_draft('code', 'sent', 'test', followup_to_last_sent=True)
        svc.users().drafts().create.assert_not_called()
