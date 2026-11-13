"""CallRuntime._finalize -> commitment store, on a hand-built stored workflow (no adapters, no network)."""
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / 'service-packages/brain/src'))
sys.path.insert(0, str(ROOT / 'service-packages/gtm'))
spec = importlib.util.spec_from_file_location('ole_gtm_runtime', ROOT / 'hermes-home/plugins/ole-gtm/runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
from gtm.commitments import CommitmentStore  # noqa: E402
from gtm.coordinator import Identity  # noqa: E402

TRANSCRIPT = 'Ole: Ich schicke dir bis 20.11.2026 die Unterlagen.\nKunde: Ich prüfe das intern.\n'
BINDING = dict(category='acribic', account='operator@primary.example.invalid', source_id='call-9', version=1,
               title='Call', analysis_contract_version=2, occurred_at='2026-11-12T10:00:00+01:00', source_roles={'ole': 'ole', 'kunde': 'counterparty'})
RAW = dict(summary='Kurz.', demo=dict(state='not_discussed', evidence=[]),
           followup_permission=dict(state='not_discussed', evidence=[]), statements=[],
           review_items=[
               dict(key='docs', title='Unterlagen schicken', assignee='ole', due_date='2026-11-20', deadline_basis='20.11.2026',
                    evidence=dict(speaker='Ole', party='ole', quote='Ich schicke dir bis 20.11.2026 die Unterlagen.', evidence='Zeile 1')),
               dict(key='check', title='Intern prüfen', assignee='other',
                    evidence=dict(speaker='Kunde', party='counterparty', quote='Ich prüfe das intern.', evidence='Zeile 2'))])


def quality(raw):
    from gtm.call_flow.quality import TOPICS, ADDITIONAL_TOPICS
    topics = {k: dict(status='not_discussed', reason='Nicht besprochen.', findings=[]) for k in {**TOPICS, **ADDITIONAL_TOPICS}}
    topics['next_steps'] = dict(status='evidenced', reason='Zusagen im Gespraech.', findings=[
        dict(text=item['title'], kind='fact', evidence=[item['evidence']], action_key=item['key']) for item in raw['review_items']])
    return dict(version=2, topics=topics)


RAW['quality_review'] = quality(RAW)


class Finalize(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name) / 'calls'
        self.rt = runtime.CallRuntime(self.root, readers=None)
        identity = Identity('acribic', BINDING['account'], 'call', 'call-9', 1, hashlib.sha256(TRANSCRIPT.encode()).hexdigest())
        self.rt.coordinator.plan(identity)
        directory = self.root / identity.key
        directory.mkdir(mode=0o700)
        (directory / 'binding.json').write_text(json.dumps(BINDING))
        done = self.rt.coordinator.run_phase(identity, 'source', runtime.LocalArtifact(directory / 'source.txt', TRANSCRIPT.encode(), identity))
        assert done['state'] == 'completed', done
        self.key = identity.key

    def test_default_runtime_records_nothing_and_still_saves_analysis(self):
        result = self.rt._finalize(self.key, RAW)
        self.assertTrue(result['analysis_saved'])
        self.assertEqual(result['commitments'], {'state': 'not_configured'})

    def test_configured_store_gets_only_owner_items_after_analysis_saved(self):
        store = CommitmentStore(Path(self.dir.name) / 'c.db')
        self.rt.commitment_store = store
        result = self.rt._finalize(self.key, RAW)
        self.assertEqual(result['commitments']['state'], 'recorded')
        self.assertEqual(len(result['commitments']['refs']), 1)
        with store.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT text,due_precision FROM commitment')]
        self.assertEqual(rows, [{'text': 'Unterlagen schicken', 'due_precision': 'date'}])
        again = self.rt._finalize(self.key, RAW)
        self.assertEqual(again['commitments']['refs'], result['commitments']['refs'])
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM commitment').fetchone()[0], 1)

    def test_broken_store_never_fails_or_unsaves_the_analysis(self):
        class Broken:
            def add(self, *a, **k):
                raise RuntimeError('disk')
        self.rt.commitment_store = Broken()
        result = self.rt._finalize(self.key, RAW)
        self.assertTrue(result['analysis_saved'])
        self.assertEqual(result['commitments']['state'], 'unavailable')

    def test_invalid_evidence_records_no_commitment(self):
        store = CommitmentStore(Path(self.dir.name) / 'c.db')
        self.rt.commitment_store = store
        bad = json.loads(json.dumps(RAW))
        bad['review_items'][0]['evidence']['quote'] = 'Das wurde nie gesagt.'
        result = self.rt._finalize(self.key, bad)
        self.assertTrue(result.get('abstain'))
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM commitment').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
