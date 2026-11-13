import contextlib
import datetime as dt
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

BRAIN = Path(__file__).resolve().parents[4] / 'brain'
spec = importlib.util.spec_from_file_location('dream_nightly', BRAIN / 'scripts/dream_nightly.py')
nightly = importlib.util.module_from_spec(spec); spec.loader.exec_module(nightly)
NOW = lambda: dt.datetime(2026, 11, 13, 3, 10, tzinfo=ZoneInfo('Europe/Berlin'))


class Launcher(unittest.TestCase):
    def run_launcher(self, outcome):
        calls = []
        def run(command, **kw):
            calls.append([Path(str(c)).name for c in command])
            out = json.dumps(outcome) if command[0].endswith('dream_sweep.sh') else ''
            return subprocess.CompletedProcess(command, 0, stdout=out)
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / 'manifest.json'
            config.write_text(json.dumps({'dreaming': {'sync_scope': 'dreams_only'}}))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = nightly.main(['--project-dir', str(BRAIN), '--config', str(config)], run=run, now=NOW)
        return code, calls, err.getvalue()

    def test_zero_round_complete_keeps_exit_and_sync_but_logs_no_work(self):
        code, calls, err = self.run_launcher({'status': 'complete', 'health': 'no_work', 'rounds': 0})
        self.assertEqual(code, 0)
        self.assertIn('dream_health=no_work', err)
        self.assertIn('dream_sync.py', [name for call in calls for name in call])

    def test_profile_failure_complete_logs_degraded(self):
        code, _, err = self.run_launcher({'status': 'complete', 'health': 'degraded_profile', 'rounds': 1, 'profile_failures': ['p']})
        self.assertEqual(code, 0); self.assertIn('dream_health=degraded_profile', err)

    def test_failed_status_still_exits_nonzero_without_sync(self):
        code, calls, err = self.run_launcher({'status': 'failed', 'health': 'failed'})
        self.assertEqual(code, 1); self.assertIn('dream_health=failed', err)
        self.assertNotIn('dream_sync.py', [name for call in calls for name in call])

    def test_older_outcome_without_health_logs_unknown(self):
        code, _, err = self.run_launcher({'status': 'complete'})
        self.assertEqual(code, 0); self.assertIn('dream_health=unknown', err)


class ReportDelivery(unittest.TestCase):
    def run_with(self, section, outcome=None, send_rc=0, sync_rc=0, send_out='delivered', send_timeout=False):
        calls = []
        outcome = outcome or {'status': 'complete', 'health': 'ok', 'rounds': 1}
        def run(command, **kw):
            calls.append([str(c) for c in command])
            if command[0].endswith('dream_sweep.sh'):
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(outcome))
            if any(str(c).endswith('dream_report_send.py') for c in command):
                if send_timeout:
                    raise subprocess.TimeoutExpired(command, 45)
                return subprocess.CompletedProcess(command, send_rc, stdout=send_out)
            if any(str(c).endswith('dream_sync.py') for c in command):
                return subprocess.CompletedProcess(command, sync_rc)
            return subprocess.CompletedProcess(command, 0, stdout='')
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / 'manifest.json'
            config.write_text(json.dumps({'dreaming': {'sync_scope': 'dreams_only', **section}}))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = nightly.main(['--project-dir', str(BRAIN), '--config', str(config)], run=run, now=NOW)
        return code, calls, err.getvalue()

    def native(self):
        return {'command': ['/py', '-I', '-c', 'bootstrap', str(BRAIN / 'scripts/dream_native_adapter.py')]}

    def sent(self, calls):
        return [c for c in calls if any(x.endswith('dream_report_send.py') for x in c)]

    def test_default_off_never_sends(self):
        code, calls, err = self.run_with({'model': self.native()})
        self.assertEqual((code, self.sent(calls)), (0, []))

    def test_enabled_sends_once_through_native_python_after_safe_sync(self):
        code, calls, err = self.run_with({'model': self.native(), 'report_delivery': {'enabled': True}})
        sent = self.sent(calls)
        self.assertEqual((code, len(sent)), (0, 1))
        self.assertEqual(sent[0][:4], ['/py', '-I', '-c', 'bootstrap'])
        self.assertIn('dream_report_delivery=ok', err)

    def test_exit_zero_is_not_ok_without_delivered_state(self):
        for out, label in (('disabled', 'disabled'), ('quiet', 'quiet'), ('', 'unknown'), ('weird', 'unknown'), ('unknown', 'unknown')):
            code, _, err = self.run_with({'model': self.native(), 'report_delivery': {'enabled': True}}, send_rc=0, send_out=out)
            self.assertEqual(code, 0); self.assertIn(f'dream_report_delivery={label}', err, msg=out)
            self.assertNotIn('dream_report_delivery=ok', err, msg=out)

    def test_delivered_word_with_failing_exit_is_not_ok(self):
        code, _, err = self.run_with({'model': self.native(), 'report_delivery': {'enabled': True}}, send_rc=1)
        self.assertIn('dream_report_delivery=not_confirmed', err); self.assertNotIn('dream_report_delivery=ok', err)

    def test_sync_outage_does_not_hold_back_the_report(self):
        code, calls, err = self.run_with({'model': self.native(), 'report_delivery': {'enabled': True}}, sync_rc=1)
        self.assertEqual(len(self.sent(calls)), 1); self.assertIn('dream_report_delivery=ok', err)

    def test_send_timeout_is_unknown_and_sync_still_runs(self):
        code, calls, err = self.run_with({'model': self.native(), 'report_delivery': {'enabled': True}}, send_timeout=True)
        self.assertEqual(code, 0); self.assertIn('dream_report_delivery=unknown', err)
        self.assertIn('dream_sync.py', [n for c in calls for n in map(lambda x: Path(x).name, c)])

    def test_unreviewed_native_command_blocks_without_running_anything(self):
        code, calls, err = self.run_with({'model': {'command': ['/py', 'x']}, 'report_delivery': {'enabled': True}})
        self.assertEqual(self.sent(calls), []); self.assertIn('blocked_invalid_native_command', err)

    def test_failed_dream_run_sends_no_report(self):
        code, calls, _ = self.run_with({'model': self.native(), 'report_delivery': {'enabled': True}},
                                       outcome={'status': 'failed', 'health': 'failed'})
        self.assertEqual((code, self.sent(calls)), (1, []))


if __name__ == '__main__':
    unittest.main()
