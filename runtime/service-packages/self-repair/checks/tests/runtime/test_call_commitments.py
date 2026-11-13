"""Call analysis -> commitment store -> reminder tick, with the real validators."""
import datetime as dt
import hashlib
import sys
import tempfile
import unittest
import zoneinfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'brain/src'))
from gtm.call_analysis import CallAnalysis, ReviewItem
from gtm.commitments import CommitmentStore, commitment_tick, record_call_commitments
from gtm.coordinator import Identity
from gtm.twenty import Outcome, Statement

TZ = zoneinfo.ZoneInfo('Europe/Berlin')
TRANSCRIPT = ('Ole: Ich schicke dir bis 20.11.2026 die Unterlagen.\nKunde: Super, ich melde mich nach dem 25.11.2026.\n'
              'Kunde: Ich prüfe das intern.')


def analysis(items, transcript=TRANSCRIPT):
    identity = Identity('acribic', 'operator@primary.example.invalid', 'call', 'call-1', 1,
                        hashlib.sha256(transcript.encode()).hexdigest())
    return CallAnalysis(identity, None, None, 'Call', '2026-11-12T10:00:00+01:00', transcript, 'Kurz.',
                        Outcome('not_discussed'), Outcome('not_discussed'), (), tuple(items))


def stmt(quote, party):
    return Statement('Ole' if party == 'ole' else 'Kunde', quote, 'Transkript', party)


OLE = ReviewItem('send-docs', 'Unterlagen schicken', stmt('Ich schicke dir bis 20.11.2026 die Unterlagen.', 'ole'),
                 assignee='ole', due_date='2026-11-20', deadline_basis='20.11.2026')
THEIRS = ReviewItem('check', 'Intern prüfen', stmt('Ich prüfe das intern.', 'counterparty'), assignee='other')
UNCLEAR = ReviewItem('q', 'Preis klären', stmt('Ich prüfe das intern.', 'counterparty'), kind='open_question')


class Producer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = CommitmentStore(Path(self.dir.name) / 'c.db')

    def test_only_owner_actions_become_commitments_with_evidenced_due(self):
        refs = record_call_commitments(self.store, analysis([OLE, THEIRS, UNCLEAR]))
        self.assertEqual(len(refs), 1)
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM commitment')]
        self.assertEqual(rows[0]['text'], 'Unterlagen schicken')
        self.assertEqual(rows[0]['due'], '2026-11-20T18:00:00+01:00')
        self.assertIn('call-1', rows[0]['ref'])

    def test_rerun_is_idempotent_and_new_version_is_a_new_source(self):
        a = analysis([OLE])
        record_call_commitments(self.store, a)
        record_call_commitments(self.store, a)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM commitment').fetchone()[0], 1)

    def test_unevidenced_quote_or_deadline_blocks_everything(self):
        bad_quote = ReviewItem('x', 'Erfunden', stmt('Das hat niemand gesagt.', 'ole'), assignee='ole')
        with self.assertRaises(ValueError):
            record_call_commitments(self.store, analysis([OLE, bad_quote]))
        bad_date = ReviewItem('y', 'Falsches Datum', stmt('Ich schicke dir bis 20.11.2026 die Unterlagen.', 'ole'),
                              assignee='ole', due_date='2026-11-30', deadline_basis='20.11.2026')
        with self.assertRaises(ValueError):
            record_call_commitments(self.store, analysis([bad_date]))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM commitment').fetchone()[0], 0)

    def test_hash_mismatch_with_transcript_blocks(self):
        a = analysis([OLE])
        tampered = CallAnalysis(**{**a.__dict__, 'transcript': TRANSCRIPT + ' extra'})
        with self.assertRaises(ValueError):
            record_call_commitments(self.store, tampered)

    def test_undated_commitment_is_tracked_but_never_reminded(self):
        item = ReviewItem('n', 'Ohne Datum', stmt('Ich prüfe das intern.', 'ole'), assignee='ole')
        record_call_commitments(self.store, analysis([item]))
        now = dt.datetime(2026, 11, 19, 12, tzinfo=TZ)
        self.assertEqual(self.store.due_for_reminder(now), [])

    def test_chain_reaches_owner_tick_only_after_receipt(self):
        record_call_commitments(self.store, analysis([OLE]))
        sent = []

        class Sender:
            result = {'success': True, 'message_id': 'm-1'}

            def send(self_inner, key, message):
                sent.append(message)
                return self_inner.result
        now = dt.datetime(2026, 11, 20, 9, tzinfo=TZ)
        dry = commitment_tick(self.store, Sender(), now, TZ)
        self.assertEqual((dry['dry_run'], sent), (True, []))
        report = commitment_tick(self.store, Sender(), now, TZ, activate=True)
        self.assertEqual((report['delivered'], len(sent)), (1, 1))
        self.assertIn('Unterlagen schicken', sent[0])
        self.assertIn('Nichts davon wurde nach außen geschickt', sent[0])
        again = commitment_tick(self.store, Sender(), now + dt.timedelta(hours=1), TZ, activate=True)
        self.assertEqual((again['due'], len(sent)), (0, 1))

    def test_unknown_receipt_is_not_replayed(self):
        record_call_commitments(self.store, analysis([OLE]))
        sent = []

        class Sender:
            def send(self_inner, key, message):
                sent.append(message)
                return None
        now = dt.datetime(2026, 11, 20, 9, tzinfo=TZ)
        self.assertEqual(commitment_tick(self.store, Sender(), now, TZ, activate=True)['unknown'], 1)
        commitment_tick(self.store, Sender(), now + dt.timedelta(hours=2), TZ, activate=True)
        self.assertEqual(len(sent), 1)

if __name__ == '__main__':
    unittest.main()
