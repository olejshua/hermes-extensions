import base64
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch, Mock

import draft_batch as d

def fixture():
    msg = EmailMessage()
    msg['To'] = 'Thomas <thomas@example.invalid>'
    msg['Cc'] = 'ole@example.invalid'
    msg['Subject'] = 'Re: Austausch – nächste Woche'
    msg['In-Reply-To'] = '<original@example.invalid>'
    msg['References'] = '<older@example.invalid> <original@example.invalid>'
    msg.set_content('Alte Antwort')
    msg.add_alternative('<p>Alte Antwort</p>', subtype='html')
    msg.add_attachment(b'\x00\xffattachment', maintype='application', subtype='octet-stream', filename='info.bin')
    return {'message': {'raw': base64.urlsafe_b64encode(msg.as_bytes()).decode(),
                        'threadId': 'thread-1', 'labelIds': ['DRAFT']}}

class DraftBatchTests(unittest.TestCase):
    def test_multipart_updates_both_bodies_preserving_headers_and_attachment(self):
        original = fixture()
        request, body, html_body, protected = d.stage(original, 'Hallo Thomas,\n\nneuer Text & Details\n\nBeste Grüße\nOle', d.digest(original))
        saved = {'message': request['message']}
        msg = d.mime(saved)
        self.assertEqual(d.protected(msg), protected)
        self.assertEqual(request['message']['threadId'], 'thread-1')
        parts = d.text_parts(msg)
        self.assertEqual(len(parts), 2)
        self.assertEqual(d.normalized(parts[0].get_content()), body)
        self.assertEqual(d.normalized(parts[1].get_content()), html_body)
        self.assertIn('&amp;', html_body)

    def test_stale_second_draft_aborts_before_any_write(self):
        api = Mock()
        drafts = {'first': fixture(), 'second': fixture()}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'body.txt'
            path.write_text('Neuer Text', encoding='utf-8')
            with patch.object(d, 'read_many', return_value=drafts):
                with self.assertRaisesRegex(ValueError, 'changed since inspection'):
                    d.apply(api, [('first', str(path), d.digest(drafts['first'])),
                                  ('second', str(path), 'stale')])
        api.new_batch_http_request.assert_not_called()

    def test_sent_message_refused(self):
        draft = fixture()
        draft['message']['labelIds'] = ['SENT']
        with self.assertRaisesRegex(ValueError, 'unsent draft'):
            d.stage(draft, 'Neuer Text', d.digest(draft))

if __name__ == '__main__':
    unittest.main()
