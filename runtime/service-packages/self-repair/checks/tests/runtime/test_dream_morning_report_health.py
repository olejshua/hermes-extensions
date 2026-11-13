import datetime as dt
import sys
import types
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'brain/src'))
from hermes_second_brain.operations import build_morning_report


class Store:
    def __init__(self, run): self.run = run
    def run_by_id(self, run_id): return self.run
    def latest_run(self): return self.run
    def publication_counts(self): return {}
    def staging_counts(self): return {}
    def pending_source_count(self): return 0


CONFIG = types.SimpleNamespace(dream_state_db=Path('/nonexistent/a'), lifecycle_db=Path('/nonexistent/b'),
                               improvement_db=Path('/nonexistent/c'), tzinfo=lambda: ZoneInfo('Europe/Berlin'))
NOW = dt.datetime(2026, 11, 13, 7, 0, tzinfo=ZoneInfo('Europe/Berlin'))


def report(**run):
    return build_morning_report(CONFIG, context_db=Path('/nonexistent/d'), now=NOW, store=Store(run))


class MorningReportHealth(unittest.TestCase):
    def line(self, text):
        return next(l for l in text.splitlines() if l.startswith('Dream-Gesundheit'))

    def test_complete_run_without_work_is_not_called_healthy(self):
        line = self.line(report(status='complete', rounds=0, summary={'sessions': 0}))
        self.assertIn('keine Arbeit', line); self.assertNotIn('Gesundheit: complete;', line)

    def test_profile_errors_are_named(self):
        line = self.line(report(status='complete', rounds=2, summary={'sessions': 3, 'profile_failures': 1}))
        self.assertIn('Profilfehler', line)

    def test_real_work_run_stays_ok_and_keeps_round_count(self):
        line = self.line(report(status='complete', rounds=2, summary={'sessions': 3, 'profile_failures': 0}))
        self.assertIn('ok', line); self.assertIn('Runden im letzten Lauf: 2', line)

    def test_failed_and_missing_runs_keep_existing_wording(self):
        self.assertIn('failed', self.line(report(status='failed', rounds=1, summary={})))
        self.assertIn('noch_nie', self.line(build_morning_report(CONFIG, context_db=Path('/nonexistent/d'), now=NOW, store=Store(None))))


if __name__ == '__main__':
    unittest.main()
