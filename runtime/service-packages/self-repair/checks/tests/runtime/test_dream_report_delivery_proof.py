"""Item 8: stdout hand-off is legacy_unverified; confirmed needs a receipt bound to the recorded attempt."""
import json, sqlite3, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'brain' / 'src'))
from hermes_second_brain.dreaming.store import DreamStore  # noqa: E402
from hermes_second_brain.dreaming import delivery  # noqa: E402


def good(attempt_args, mid='42'):
    return {'success': True, 'message_id': mid}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'dream.sqlite3'
        self.store = DreamStore(self.db)
        self.report = self.root / 'r.md'; self.report.write_text('Bericht')
        self.rid = self.store.record_report(run_id='run-x', path=self.report, fingerprint='fp1')

    def state(self):
        with self.store.transaction() as c:
            r = c.execute('SELECT delivered_at, delivery_state FROM reports WHERE report_id=?', (self.rid,)).fetchone()
        return r[0], r[1]


class Store(Base):
    def test_characterization_plain_ack_still_sets_delivered_at_but_is_legacy_unverified(self):
        """Legacy consumers keep working (delivered_at set); the state no longer pretends it is confirmed."""
        self.store.claim_report(owner='cli'); self.assertTrue(self.store.ack_report(self.rid, 'cli'))
        at, st = self.state()
        self.assertIsNotNone(at); self.assertEqual(st, 'legacy_unverified')
        self.assertEqual(self.store.report_delivery_counts(), {'confirmed': 0, 'legacy_unverified': 1, 'attempt_unknown': 0})

    def test_migration_labels_old_delivered_rows_legacy_unverified(self):
        self.store.claim_report(owner='cli'); self.store.ack_report(self.rid, 'cli')
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE reports SET delivery_state=''")
        DreamStore(self.db)  # reopen runs the migration backfill
        self.assertEqual(self.state()[1], 'legacy_unverified')

    def attempt(self, **kw):
        a = {'attempt_id': 'a1', 'report_id': self.rid, 'fingerprint': 'fp1', 'arguments_sha256': 'h', 'target_sha256': 't',
             'platform': 'telegram', 'state': 'attempted'}
        a.update(kw); return a

    def receipt(self, **kw):
        r = {'success': True, 'message_id': '42', 'attempt_id': 'a1', 'arguments_sha256': 'h', 'target_sha256': 't',
             'platform': 'telegram', 'report_id': self.rid}
        r.update(kw); return r

    def test_receipt_must_bind_to_the_recorded_attempt(self):
        self.store.claim_report(owner='cli')
        self.assertFalse(self.store.ack_report_receipt(self.rid, 'cli', self.receipt()))  # no attempt recorded
        self.assertFalse(self.store.begin_report_attempt(self.rid, 'cli', self.attempt(fingerprint='other')))
        self.assertTrue(self.store.begin_report_attempt(self.rid, 'cli', self.attempt()))
        self.assertFalse(self.store.begin_report_attempt(self.rid, 'cli', self.attempt()))  # no second attempt
        for bad in ({'attempt_id': 'a2'}, {'arguments_sha256': 'x'}, {'target_sha256': 'x'}, {'platform': 'slack'},
                    {'report_id': 'r-other'}, {'message_id': 'abc'}, {'message_id': '0'}, {'success': False},
                    {'error': 'x'}, {'skipped': True}, {'partial_success': True}):
            self.assertFalse(self.store.ack_report_receipt(self.rid, 'cli', self.receipt(**bad)), msg=str(bad))
        self.assertFalse(self.store.ack_report_receipt(self.rid, 'other', self.receipt()))
        self.assertIsNone(self.state()[0])
        self.assertFalse(self.store.ack_report(self.rid, 'cli'))  # plain ack cannot finish an attempted send
        self.assertEqual(self.store.report_delivery_counts()['attempt_unknown'], 1)
        self.assertTrue(self.store.ack_report_receipt(self.rid, 'cli', self.receipt()))
        self.assertEqual(self.state()[1], 'confirmed')
        self.assertFalse(self.store.ack_report_receipt(self.rid, 'cli', self.receipt()))  # no double confirm


class Native(Base):
    def run_send(self, sender, enabled=True, target='telegram:123'):
        cfg = self.root / 'm.json'
        cfg.write_text(json.dumps({'dreaming': {'report_delivery': {'enabled': enabled, 'target': target}}}))
        config = SimpleNamespace(allowed_telegram_user_ids={'123'}, dream_state_db=self.db)
        with patch.object(delivery, 'load_dreaming_config', lambda p: config):
            return delivery.send_stored_report(cfg, sender=sender, owner='o1')

    def test_disabled_and_wrong_target_do_not_send(self):
        calls = []
        self.assertEqual(self.run_send(lambda a: calls.append(a), enabled=False)['state'], 'disabled')
        self.assertEqual(self.run_send(lambda a: calls.append(a), target='telegram:999')['state'], 'blocked')
        self.assertEqual(calls, [])

    def test_success_with_message_id_is_confirmed(self):
        sent = []
        out = self.run_send(lambda a: sent.append(a) or json.dumps({'success': True, 'message_id': 7}))
        self.assertEqual(out['state'], 'delivered'); self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['target'], 'telegram:123')
        self.assertEqual(self.state()[1], 'confirmed')

    def test_success_without_message_id_is_unknown_and_never_replayed(self):
        sent = []
        out = self.run_send(lambda a: sent.append(a) or {'success': True})
        self.assertEqual(out['state'], 'unknown'); self.assertFalse(out['retry_authorized'])
        self.assertIsNone(self.state()[0])
        self.run_send(lambda a: sent.append(a) or {'success': True, 'message_id': 9})
        self.assertEqual(len(sent), 1)  # still claimed/attempted: no second send
        self.assertEqual(self.store.report_delivery_counts()['attempt_unknown'], 1)

    def test_sender_crash_is_unknown(self):
        def boom(a): raise RuntimeError('x')
        self.assertEqual(self.run_send(boom)['state'], 'unknown')
        self.assertIsNone(self.state()[0])

class CliSend(Base):
    def run_cli(self, enabled, sender):
        import contextlib, io
        from hermes_second_brain import cli
        cfg = self.root / 'm.json'
        cfg.write_text(json.dumps({'dreaming': {'report_delivery': {'enabled': enabled, 'target': 'telegram:123'}}}))
        config = SimpleNamespace(allowed_telegram_user_ids={'123'}, dream_state_db=self.db)
        real = delivery.send_stored_report
        out = io.StringIO()
        with patch.object(delivery, 'load_dreaming_config', lambda p: config), \
             patch.object(delivery, 'send_stored_report', lambda path, owner=None: real(path, sender=sender, owner=owner)), \
             contextlib.redirect_stdout(out):
            code = cli.main(['dream-report-send', '--config', str(cfg), '--claim-owner', 'o1'])
        return code, json.loads(out.getvalue())

    def test_cli_disabled_sends_nothing_and_succeeds(self):
        sent = []
        code, out = self.run_cli(False, lambda a: sent.append(a))
        self.assertEqual((code, out['state'], sent), (0, 'disabled', []))

    def test_cli_delivers_once_with_receipt_and_fails_on_unknown(self):
        code, out = self.run_cli(True, lambda a: {'success': True, 'message_id': 5})
        self.assertEqual((code, out['state']), (0, 'delivered'))
        self.assertEqual(self.state()[1], 'confirmed')
        rid2 = self.store.record_report(run_id='run-y', path=self.report, fingerprint='fp2')
        code, out = self.run_cli(True, lambda a: {'success': True})
        self.assertEqual((code, out['state']), (1, 'unknown'))


if __name__ == '__main__':
    unittest.main()
