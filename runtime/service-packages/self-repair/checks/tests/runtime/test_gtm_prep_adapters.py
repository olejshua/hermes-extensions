import json
import types
import unittest
from gtm.briefing_daemon import ReviewRequired
from gtm.prep_adapters import MailEvidence, PrivateSender, evidence_drafter
from gtm.prep_daemon import validate_draft
from gtm.prep_daemon_entry import config_from


class Call:
    def __init__(self, result): self.result = result
    def execute(self): return self.result


class FakeService:
    def __init__(self): self.queries = []; self.fail_list = False
    def users(self): return self
    def messages(self): return self
    def getProfile(self, userId): return Call({'emailAddress': getattr(self, 'email', 'operator@primary.example.invalid')})
    def list(self, userId, q, maxResults):
        self.queries.append(q); self.fail_list and (_ for _ in ()).throw(RuntimeError('403')); return Call({'messages': [{'id': 'a1'}]})
    def get(self, userId, id, format, metadataHeaders):
        return Call({'id': id, 'snippet': 'Termin Runde', 'payload': {'headers': [
            {'name': 'Subject', 'value': 'Pre-Seed'}, {'name': 'Date', 'value': 'Wed, 12 Nov 2026 10:00:00'}]}})


class FakeReads:
    def __init__(self): self.svc = FakeService(); self.mail_module = types.SimpleNamespace(service=lambda alias: self.svc)


EVENT = {'attendees': [{'email': 'me@example.invalid', 'self': True}, {'email': 'D@outside.invalid'},
                       {'email': 'room@x.invalid', 'resource': True}, {'email': 'bad address'}]}


class AdapterTests(unittest.TestCase):
    def test_mail_only_when_allowed_and_only_attendee_addresses(self):
        reads = FakeReads(); collector = MailEvidence(reads)
        self.assertEqual(collector.collect(EVENT, ('calendar',)), [])
        self.assertEqual(reads.svc.queries, [])
        items = collector.collect(EVENT, ('mail',))
        self.assertEqual(reads.svc.queries, ['(from:d@outside.invalid OR to:d@outside.invalid) newer_than:90d'])
        self.assertEqual(items[0]['ref'], 'mail:a1'); self.assertIn('Pre-Seed', items[0]['text'])

    def test_wrong_mailbox_refused_and_hostile_address_never_queried(self):
        reads = FakeReads(); reads.svc.email = 'someone@else.invalid'
        with self.assertRaises(ReviewRequired):
            MailEvidence(reads).collect(EVENT, ('mail',))
        reads = FakeReads()
        evil = {'attendees': [{'email': 'x@y.com OR in:anywhere'}, {'email': '-label:x@y.com'}, {'email': 'a"b@y.com'}]}
        MailEvidence(reads).collect(evil, ('mail',))
        self.assertEqual(reads.svc.queries, [])

    def test_fallback_drafter_cites_and_passes_validation(self):
        items = MailEvidence(FakeReads()).collect(EVENT, ('mail',))
        known, questions = validate_draft(evidence_drafter(EVENT, items), items)
        self.assertTrue(known and questions)

    def test_sender_uncertain_vs_rejected(self):
        def run(out, code=0):
            return lambda *a, **k: types.SimpleNamespace(returncode=code, stdout=out)
        send = lambda r: PrivateSender('/tmp', run=r).send('id', 'text')
        self.assertEqual(send(run(json.dumps({'success': True, 'message_id': 'm'}))), {'success': True, 'message_id': 'm'})
        self.assertIsNone(send(run('', 1)))
        self.assertIsNone(send(run('not json')))
        self.assertEqual(send(run(json.dumps({'skipped': True}))), {'success': False})

    def test_wrong_destination_refused(self):
        with self.assertRaises(ReviewRequired):
            PrivateSender('/tmp', target='telegram:1')

    def test_policy_marks_configured_and_rejects_unknown(self):
        cfg = config_from({'policy': {'lead_hours': 24, 'important_terms': ['david']}})
        self.assertEqual((cfg.lead_hours, cfg.important_terms, cfg.source('lead_hours'), cfg.source('quiet_start')),
                         (24, ('david',), 'configured', 'default'))
        with self.assertRaises(ReviewRequired):
            config_from({'policy': {'send_to': 'x'}})


if __name__ == '__main__':
    unittest.main()
