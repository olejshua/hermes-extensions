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


if __name__ == '__main__':
    unittest.main()
