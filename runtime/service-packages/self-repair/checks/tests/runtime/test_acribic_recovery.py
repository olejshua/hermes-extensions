"""Static-local tests for safe resolution of unclear Acribic sends (list item 7).

No live Gmail, CRM or server state is touched: fakes stand in for both. The
tests prove the recovery resolves only exact unique read-only Sent evidence,
never retries, and leaves every reservation intact on ambiguity.
"""
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ACRIBIC = Path(__file__).resolve().parents[4] / 'acribic-daily'
sys.path.insert(0, str(ACRIBIC / 'candidate'))
sys.path.insert(0, str(ACRIBIC / 'vendor'))

from acribic_pilot.central import Central, Stop, fingerprint
from acribic_pilot.pilot import Pilot
import versand_abschluss_sicher as closure
import versand_senden_sicher as sender

# The native SMTP validator lives on the host and imports dnspython; tests use
# a minimal stand-in with the same contract (recipient-specific reject = OK).
import types
smtp_stub = types.ModuleType('smtp_pruefen')
smtp_stub.status = lambda real, rand: 'OK' if real[:3] == '250' and rand[:3] == '550' else 'FEHLER'
sys.modules.setdefault('smtp_pruefen', smtp_stub)

BERLIN = ZoneInfo('Europe/Berlin')
DATE = '2026-11-12'
PAGE = 'd0e0d0e0d0e0d0e0d0e0d0e0d0e0d0e0'  # 32 chars, redacted public charset


class FakeTwenty:
    def groups(self):
        return None


class FakeV:
    def __init__(self, state):
        self.STATE = str(state)
        self.SENDER_LOCK = str(state / 'test.lock')
        self.NACHTRAG = str(state / 'nachtrag.csv')
        self.LISTEN = {name: 'db-' + name for name in
                       ('it', 'ingenieur', 'medizin', 'beratung', 's1', 's2', 's3')}
        self.LISTE = 'it'
        self.BACKEND = 'notion'
        self.TWENTY = FakeTwenty()
        self.NICHT_ANSCHREIBEN = re.compile(r'(?!)')
        self.pages = {}

    def basis(self, d):
        return d

    def dom(self, value):
        return value.split('@')[1] if '@' in value else value

    def firma_key(self, value):
        return value.strip().lower()

    def mailtext(self, vorname, firma, buyer):
        return f'Hallo {vorname}, Bezug: {buyer}. Ich studiere an der CODE University.'

    def txt(self, value):
        return value if isinstance(value, str) else ''

    def load(self, path):
        return json.loads(Path(path).read_text())

    def api(self, method, path, body=None):
        pid = path.rsplit('/', 1)[1]
        return dict(self.pages[pid])

    def patch(self, pid, stage=None, notiz=None):
        self.pages[pid]['properties']['Stage'] = stage

    def cmd_abschluss(self, excluded, flag):
        with open(self.NACHTRAG, 'a', encoding='utf-8') as out:
            for mail in self.load(self.MAILS):
                if closure.mailbox(mail['to']) not in excluded:
                    out.write(mail['to'] + ';E-Mail raus;x\n')
        for mail in self.load(self.MAILS):
            if closure.mailbox(mail['to']) not in excluded:
                self.patch(mail['page_id'], 'E-Mail raus', None)


class FakeGmail:
    def __call__(self, slug, data):
        if slug == 'GMAIL_GET_PROFILE':
            return {'emailAddress': self.profile}
        assert slug == 'GMAIL_FETCH_EMAILS', slug
        return {'messages': list(self.messages)}

    def __init__(self, messages, profile=None):
        self.messages = messages
        self.allow_write = False
        self.profile = closure.SENDER if profile is None else profile

    def normalized_body(self, value):
        return value


def page_id(i):
    # 32 chars from the redacted public charset {0,d,e}, unique per index.
    tail = ''
    for _ in range(4):
        tail = '0de'[i % 3] + tail
        i //= 3
    return PAGE[:28] + tail


def make_run(tmp, count=10):
    root = Path(tmp) / 'day'
    run = root / 'runs' / 'acribic-20261112-N10-0703-0721'
    run.mkdir(parents=True)
    v = FakeV(Path(tmp) / 'state')
    v.STATE = str(Path(tmp) / 'state')
    Path(v.STATE).mkdir(parents=True, exist_ok=True)
    Path(v.NACHTRAG).write_text('', encoding='utf-8')
    slots = [(datetime(2026, 11, 12, 7, 3, tzinfo=BERLIN) + timedelta(minutes=2 * i)).isoformat()
             for i in range(10)]
    (root / 'activation.json').write_text(json.dumps(
        {'enabled': True, 'expected': 10, 'date': DATE, 'timezone': 'Europe/Berlin',
         'slots': slots, 'sole_send_host': 'vps', 'run_id': run.name}))
    mails = []
    for i in range(count):
        email = f'person{i}@firma{i}.example.de'
        subject = f'Zuschlag Referenz {i}'
        mail = {'to': email, 'page_id': page_id(i), 'org_domain': f'firma{i}.example.de',
                'family_id': f'fam{i}', 'firma': f'Firma {i}', 'liste': 'it', 'subject': subject,
                'vorname': 'Alex', 'gekuerzt': False, 'ted_publication': 'pub', 'ted_buyer': f'Käufer {i}',
                'flags': [], 'subject_review': {'mode': 'buyer', 'understandable': True,
                                              'fits_request': True, 'reason': 'passt'}}
        mail['body'] = v.mailtext(mail['vorname'], mail['firma'], subject)
        mails.append(mail)
        v.pages[mail['page_id']] = {'properties': {'Stage': 'Nicht kontaktiert', 'E-Mail': email,
                                                   'Gesprochen': '', 'Notizen': ''}}
    batch = {'batch_id': run.name, 'liste': 'it', 'datum': DATE,
             'kandidaten': [{'page_id': m['page_id'], 'email': m['to'], 'smtp': 'OK', 'smtp_probe': False,
                             'smtp_geprueft_am': DATE, 'smtp_antwort': '250 OK',
                             'smtp_zufall': '550 no such user'} for m in mails]}
    records, policy = {'rows': []}, {'policy': 1}
    import hashlib
    source = Path(tmp) / 'source.py'
    source.write_text('# fixture\n')
    sources = {'fixture': hashlib.sha256(source.read_bytes()).hexdigest()}
    ready = {'ready': 10, 'expected': 10, 'run_id': run.name, 'date': DATE, 'slots': slots,
             'mail_sha256': fingerprint(mails), 'batch_sha256': fingerprint(batch),
             'records_sha256': fingerprint(records), 'policy_sha256': fingerprint(policy),
             'sources': sources, 'source_sha256': fingerprint(sources),
             'segment_counts': {'it': count}, 'anchor': DATE + 'T00:00:00Z'}
    for name, value in (('mails.json', mails), ('batch.json', batch), ('records.json', records),
                        ('policy.json', policy), ('ready.json', ready)):
        (run / name).write_text(json.dumps(value, ensure_ascii=False))
    return root, run, v, mails, batch


def event(mail, batch, kind, attempt_id, **fields):
    common = {'schema_version': 1, 'batch_id': batch['batch_id'], 'batch_sha256': closure.fingerprint(batch),
              'page_id': closure.page_id(mail['page_id']), 'email': closure.mailbox(mail['to']),
              'content_sha256': closure.fingerprint(closure.content(mail)), 'attempt_id': attempt_id}
    return {**common, 'at': '2026-11-12T06:05:00+00:00', 'event': kind,
            'request_sha256': fingerprint({'recipient_email': mail['to'], 'subject': mail['subject'],
                                           'body': mail['body'], 'from_email': closure.SENDER}), **fields}


def sent_message(mail, mid):
    return {'messageId': mid, 'sender': 'Ich <' + closure.SENDER + '>', 'to': mail['to'],
            'subject': mail['subject'], 'labelIds': ['SENT', 'INBOX']}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root, self.run, self.v, self.mails, self.batch = make_run(self.tmp.name)
        self.central = Central(Path(self.tmp.name) / 'central')

    def pilot(self, messages):
        return Pilot(self.root, self.v, sender, closure, FakeGmail(messages),
                     source_files={'fixture': str(Path(self.tmp.name) / 'source.py')},
                     date=DATE, run_id=self.run.name, central_root=self.central.root)

    def start_attempt(self, mail, mid=None, journal_ack=True, central_kind='send_ack'):
        attempt_id = 'attempt-' + mail['to']
        journal = self.run / 'send.jsonl'
        attempt = event(mail, self.batch, 'attempt', attempt_id)
        with journal.open('a') as out:
            out.write(json.dumps(attempt, ensure_ascii=False) + '\n')
            if journal_ack:
                out.write(json.dumps(event(mail, self.batch, 'send_ack', attempt_id, message_id=mid),
                                     ensure_ascii=False) + '\n')
        with self.central.lock():
            self.central.reserve(run_id=self.run.name, mailbox=mail['to'],
                                 org_domain=mail['org_domain'], source_id=mail['page_id'], payload=attempt)
            if central_kind in ('send_ack', 'sent_readback'):
                self.central.append('send_ack', run_id=self.run.name, mailbox=mail['to'],
                                    org_domain=mail['org_domain'], source_id=mail['page_id'],
                                    payload=event(mail, self.batch, 'send_ack', attempt_id, message_id=mid))
        return attempt_id

    def test_lost_ack_resolved_by_unique_sent_proof(self):
        mail = self.mails[0]
        self.start_attempt(mail, mid=None, journal_ack=False, central_kind='attempt')
        p = self.pilot([sent_message(mail, 'mid-1')])
        result = p.resolve(mail['to'])
        self.assertEqual(result['state'], 'resolved')
        events = closure.read_journal(self.run / 'send.jsonl')
        kinds = [e['event'] for e in events]
        self.assertEqual(kinds, ['attempt', 'send_ack_recovered', 'sent_readback'])
        recovered = events[1]
        self.assertEqual(recovered['unique_match_count'], 1)
        self.assertEqual(recovered['evidence_source'], 'gmail_sent_read_only')
        state = closure.validate(self.mails, self.batch, events, 10, 'it', details=True)
        self.assertIn(mail['to'], state['confirmed'])
        self.assertEqual(self.central.states()[(self.run.name, mail['to'])]['kind'], 'sent_readback')

    def test_journal_behind_central_ack_is_replayed(self):
        mail = self.mails[1]
        self.start_attempt(mail, mid='mid-2', journal_ack=False, central_kind='send_ack')
        p = self.pilot([sent_message(mail, 'mid-2')])
        result = p.resolve(mail['to'])
        self.assertEqual(result['state'], 'resolved')
        kinds = [e['event'] for e in closure.read_journal(self.run / 'send.jsonl')]
        self.assertEqual(kinds, ['attempt', 'send_ack', 'sent_readback'])

    def test_missing_readback_only_appends_readback(self):
        mail = self.mails[2]
        self.start_attempt(mail, mid='mid-3', journal_ack=True, central_kind='send_ack')
        p = self.pilot([sent_message(mail, 'mid-3')])
        self.assertEqual(p.resolve(mail['to'])['state'], 'resolved')
        kinds = [e['event'] for e in closure.read_journal(self.run / 'send.jsonl')]
        self.assertEqual(kinds, ['attempt', 'send_ack', 'sent_readback'])

    def test_no_sent_evidence_resolves_nothing(self):
        mail = self.mails[3]
        self.start_attempt(mail, mid=None, journal_ack=False, central_kind='attempt')
        journal = self.run / 'send.jsonl'
        before_journal = journal.read_bytes()
        before_central = (self.central.root / 'actions.jsonl').read_bytes()
        result = self.pilot([]).resolve(mail['to'])
        self.assertEqual(result['state'], 'unresolved')
        self.assertEqual(journal.read_bytes(), before_journal)
        self.assertEqual((self.central.root / 'actions.jsonl').read_bytes(), before_central)

    def test_ambiguous_matches_stop_and_change_nothing(self):
        mail = self.mails[4]
        self.start_attempt(mail, mid=None, journal_ack=False, central_kind='attempt')
        before = (self.run / 'send.jsonl').read_bytes()
        with self.assertRaises(Stop):
            self.pilot([sent_message(mail, 'mid-a'), sent_message(mail, 'mid-b')]).resolve(mail['to'])
        self.assertEqual((self.run / 'send.jsonl').read_bytes(), before)

    def test_conflicting_recorded_ack_stops(self):
        mail = self.mails[5]
        self.start_attempt(mail, mid='mid-old', journal_ack=True, central_kind='send_ack')
        before = (self.run / 'send.jsonl').read_bytes()
        with self.assertRaises(Stop):
            self.pilot([sent_message(mail, 'mid-different')]).resolve(mail['to'])
        self.assertEqual((self.run / 'send.jsonl').read_bytes(), before)

    def test_confirmed_recipient_is_not_uncertain(self):
        mail = self.mails[6]
        self.start_attempt(mail, mid='mid-6', journal_ack=True, central_kind='sent_readback')
        journal = self.run / 'send.jsonl'
        with journal.open('a') as out:
            out.write(json.dumps(event(mail, self.batch, 'sent_readback', 'attempt-' + mail['to'],
                                       message_id='mid-6', sender=closure.SENDER, to=mail['to'],
                                       subject=mail['subject'], labels=['SENT']), ensure_ascii=False) + '\n')
        with self.central.lock():
            self.central.append('sent_readback', run_id=self.run.name, mailbox=mail['to'],
                                org_domain=mail['org_domain'], source_id=mail['page_id'],
                                payload=event(mail, self.batch, 'sent_readback', 'attempt-' + mail['to'],
                                              message_id='mid-6'))
        with self.assertRaises(Stop):
            self.pilot([sent_message(mail, 'mid-6')]).resolve(mail['to'])

    def test_complete_open_commits_confirmed_and_refuses_unclear(self):
        for i, mail in enumerate(self.mails):
            attempt_id = 'attempt-' + mail['to']
            with (self.run / 'send.jsonl').open('a') as out:
                out.write(json.dumps(event(mail, self.batch, 'attempt', attempt_id), ensure_ascii=False) + '\n')
                out.write(json.dumps(event(mail, self.batch, 'send_ack', attempt_id, message_id='m%d' % i),
                                     ensure_ascii=False) + '\n')
                out.write(json.dumps(event(mail, self.batch, 'sent_readback', attempt_id, message_id='m%d' % i,
                                           sender=closure.SENDER, to=mail['to'], subject=mail['subject'],
                                           labels=['SENT']), ensure_ascii=False) + '\n')
        with self.central.lock():
            for i, mail in enumerate(self.mails):
                # Fixture setup writes the attempt directly: reserve() rightly
                # refuses a second live attempt while any send is unresolved.
                self.central.append('attempt', run_id=self.run.name, mailbox=mail['to'],
                                    org_domain=mail['org_domain'], source_id=mail['page_id'],
                                    payload=event(mail, self.batch, 'attempt', 'attempt-' + mail['to']))
                self.central.append('send_ack', run_id=self.run.name, mailbox=mail['to'],
                                    org_domain=mail['org_domain'], source_id=mail['page_id'],
                                    payload=event(mail, self.batch, 'send_ack', 'attempt-' + mail['to'], message_id='m%d' % i))
                self.central.append('sent_readback', run_id=self.run.name, mailbox=mail['to'],
                                    org_domain=mail['org_domain'], source_id=mail['page_id'],
                                    payload=event(mail, self.batch, 'sent_readback', 'attempt-' + mail['to'], message_id='m%d' % i))
        p = self.pilot([])
        # One artificially unresolved send blocks the whole completion.
        uncertain_mail = self.mails[9]
        lines = (self.run / 'send.jsonl').read_text().splitlines()
        kept = [l for l in lines if json.loads(l)['email'] != uncertain_mail['to']
                or json.loads(l)['event'] != 'sent_readback']
        (self.run / 'send.jsonl').write_text('\n'.join(kept) + '\n')
        with self.assertRaises(Stop):
            p.complete_open()
        # Resolve it with read-only proof, then completion succeeds for all ten.
        messages = [sent_message(uncertain_mail, 'm9')]
        p2 = self.pilot(messages)
        self.assertEqual(p2.resolve(uncertain_mail['to'])['state'], 'resolved')
        result = p2.complete_open()
        self.assertEqual(len(result['completed']), 10)
        states = self.central.states()
        for mail in self.mails:
            self.assertEqual(states[(self.run.name, mail['to'])]['kind'], 'crm_committed')
            self.assertEqual(self.v.pages[mail['page_id']]['properties']['Stage'], 'E-Mail raus')
        rows = [r for r in Path(self.v.NACHTRAG).read_text().splitlines() if r.strip()]
        self.assertEqual(len(rows), 10)


    def test_partial_recovery_replay_is_idempotent(self):
        # Crash shape: central recorded the recovery ACK, run journal did not.
        mail = self.mails[7]
        attempt_id = self.start_attempt(mail, mid=None, journal_ack=False, central_kind='attempt')
        p = self.pilot([sent_message(mail, 'mid-7')])
        first = p.resolve(mail['to'])
        self.assertEqual(first['state'], 'resolved')
        # A repeated resolve on the resolved mailbox must refuse, not duplicate.
        with self.assertRaises(Stop):
            self.pilot([sent_message(mail, 'mid-7')]).resolve(mail['to'])
        kinds = [e['event'] for e in closure.read_journal(self.run / 'send.jsonl')]
        self.assertEqual(kinds, ['attempt', 'send_ack_recovered', 'sent_readback'])

    def test_wrong_gmail_account_never_resolves(self):
        mail = self.mails[8]
        self.start_attempt(mail, mid=None, journal_ack=False, central_kind='attempt')
        before = (self.run / 'send.jsonl').read_bytes()
        wrong = self.pilot([sent_message(mail, 'mid-8')])
        wrong.gmail.profile = 'other@example.invalid'
        with self.assertRaises(Stop):
            wrong.resolve(mail['to'])
        self.assertEqual((self.run / 'send.jsonl').read_bytes(), before)

    def test_estimate_hint_of_more_pages_stops(self):
        mail = self.mails[9]
        self.start_attempt(mail, mid=None, journal_ack=False, central_kind='attempt')
        before = (self.run / 'send.jsonl').read_bytes()
        p = self.pilot([sent_message(mail, 'mid-9')])
        original = p.gmail.messages
        class EstimateGmail(FakeGmail):
            def __call__(self, slug, data):
                if slug == 'GMAIL_FETCH_EMAILS':
                    return {'messages': list(self.messages), 'resultSizeEstimate': len(self.messages) + 5}
                return super().__call__(slug, data)
        p.gmail = EstimateGmail(original)
        with self.assertRaises(Stop):
            p.resolve(mail['to'])
        self.assertEqual((self.run / 'send.jsonl').read_bytes(), before)

    def test_standalone_sender_lock_blocks_resolution(self):
        import fcntl
        mail = self.mails[0]
        self.start_attempt(mail, mid=None, journal_ack=False, central_kind='attempt')
        before = (self.run / 'send.jsonl').read_bytes()
        with open(self.v.SENDER_LOCK, 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(Stop):
                self.pilot([sent_message(mail, 'mid-x')]).resolve(mail['to'])
        self.assertEqual((self.run / 'send.jsonl').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
