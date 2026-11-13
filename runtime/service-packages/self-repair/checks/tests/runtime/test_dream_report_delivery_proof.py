"""Item 8: a stored Dream report counts as delivered only with a native send receipt."""
import sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'brain' / 'src'))
from hermes_second_brain.dreaming.store import DreamStore  # noqa: E402


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = DreamStore(Path(self.tmp.name) / 'dream.sqlite3')
        with self.store.transaction() as c:
            cols = [r[1] for r in c.execute('PRAGMA table_info(runs)')]
        self.run_id = self._run()
        self.rid = self.store.record_report(run_id=self.run_id, path=Path(self.tmp.name) / 'r.md', fingerprint='fp1')

    def _run(self):
        for name in ('begin_run', 'start_run', 'create_run'):
            if hasattr(self.store, name):
                break
        else:
            return 'run-x'
        return 'run-x'

    def delivered(self):
        with self.store.transaction() as c:
            return c.execute('SELECT delivered_at, delivery_receipt_json FROM reports WHERE report_id=?', (self.rid,)).fetchone()

    def test_characterization_plain_ack_marks_delivered_without_any_receipt(self):
        row = self.store.claim_report(owner='cli'); self.assertIsNotNone(row)
        self.assertTrue(self.store.ack_report(self.rid, 'cli'))
        self.assertIsNotNone(self.delivered()[0])      # stdout hand-off alone was enough: no proof exists

    def test_receipt_ack_requires_a_native_send_receipt(self):
        self.store.claim_report(owner='cli')
        bad = ({}, {'success': True}, {'success': True, 'message_id': 'abc'}, {'success': True, 'message_id': '0'},
               {'success': False, 'message_id': '5'}, {'success': True, 'message_id': '5', 'error': 'x'},
               {'success': True, 'message_id': '5', 'skipped': True}, {'success': True, 'message_id': '5', 'partial_success': True})
        for receipt in bad:
            self.assertFalse(self.store.ack_report_receipt(self.rid, 'cli', receipt), msg=str(receipt))
        self.assertIsNone(self.delivered()[0])
        self.assertFalse(self.store.ack_report_receipt(self.rid, 'other-owner', {'success': True, 'message_id': '42'}))
        self.assertTrue(self.store.ack_report_receipt(self.rid, 'cli', {'success': True, 'message_id': '42', 'platform': 'telegram'}))
        at, stored = self.delivered()
        self.assertIsNotNone(at); self.assertIn('"42"', stored)

    def test_delivered_without_receipt_is_countable(self):
        self.store.claim_report(owner='cli'); self.store.ack_report(self.rid, 'cli')
        self.assertEqual(self.store.reports_delivered_without_receipt(), 1)

    def test_receipted_report_is_not_counted(self):
        self.store.claim_report(owner='cli')
        self.store.ack_report_receipt(self.rid, 'cli', {'success': True, 'message_id': '42'})
        self.assertEqual(self.store.reports_delivered_without_receipt(), 0)

if __name__ == '__main__':
    unittest.main()


class Cli(unittest.TestCase):
    def test_cli_source_has_no_ack_and_receipt_ack_wiring(self):
        """Source-level wiring check only; the CLI was not run end to end (no offline manifest)."""
        src = (ROOT / 'brain' / 'src' / 'hermes_second_brain' / 'cli.py').read_text()
        for needle in ('--no-ack', 'dream-report-ack', 'acknowledge_dream_report_receipt'):
            self.assertIn(needle, src)
        import ast; ast.parse(src)

if __name__ == '__main__':
    unittest.main()
