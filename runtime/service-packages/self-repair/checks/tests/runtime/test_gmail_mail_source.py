import base64, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'gtm'))
from gtm.gmail_mail_source import thread_sources, MailSourceError  # noqa: E402
from gtm.mail_commitments import plan_mail_commitments, MailCommitment  # noqa: E402

OWNER = 'owner@example.invalid'
enc = lambda t: base64.urlsafe_b64encode(t.encode()).decode().rstrip('=')


def msg(mid, labels, body='ich melde mich am Montag', frm=f'Ole <{OWNER}>', stamp='1794560000000', mime='text/plain', nested=False):
    part = {'mimeType': mime, 'headers': [{'name': 'From', 'value': frm}], 'body': {'data': enc(body)}}
    payload = {'mimeType': 'multipart/alternative', 'headers': part['headers'], 'parts': [part]} if nested else part
    return {'id': mid, 'threadId': 't1', 'labelIds': labels, 'internalDate': stamp, 'payload': payload}


class Svc:
    def __init__(self, profile, messages):
        self.profile, self.messages = profile, messages
    def users(self): return self
    def getProfile(self, userId): return type('R', (), {'execute': lambda s: {'emailAddress': self.profile}})()
    def threads(self): return self
    def get(self, userId, id, format):
        assert format == 'full'
        return type('R', (), {'execute': lambda s: {'id': 't1', 'messages': self.messages}})()


class T(unittest.TestCase):
    def test_own_sent_message_becomes_plannable_source(self):
        sources, gaps = thread_sources(Svc(OWNER, [msg('m1', ['SENT'], nested=True), msg('m2', ['INBOX'], frm='anna@example.invalid')]), OWNER, 't1')
        self.assertEqual((len(sources), gaps), (1, []))
        s = sources[0]
        self.assertEqual((s.message_id, s.mailbox_account, s.labels), ('m1', OWNER, ('SENT',)))
        self.assertTrue(s.sent_at.startswith('2026-'))
        planned = plan_mail_commitments(OWNER, s, [MailCommitment('a', 'Melden', 'ich melde mich am Montag')])
        self.assertEqual(len(planned), 1)

    def test_other_mailbox_is_refused(self):
        with self.assertRaises(MailSourceError):
            thread_sources(Svc('other@example.invalid', [msg('m1', ['SENT'])]), OWNER, 't1')

    def test_spoofed_inbound_from_owner_is_not_a_source(self):
        sources, _ = thread_sources(Svc(OWNER, [msg('m1', ['INBOX'])]), OWNER, 't1')
        self.assertEqual(sources, [])

    def test_gap_makes_thread_incomplete_and_planning_refuses(self):
        sources, gaps = thread_sources(Svc(OWNER, [msg('m1', ['SENT']), msg('m2', ['INBOX'], stamp='x'), msg('m3', ['SENT'], mime='text/html')]), OWNER, 't1')
        self.assertEqual(len(gaps), 2)
        self.assertEqual([s.thread_complete for s in sources], [False])
        with self.assertRaises(ValueError):
            plan_mail_commitments(OWNER, sources[0], [MailCommitment('a', 'x', 'ich melde mich am Montag')])

if __name__ == '__main__':
    unittest.main()
