import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "brain/src"))
from hermes_second_brain.dreaming.runner import SweepOutcome


class DreamOutcomeHealth(unittest.TestCase):
    def test_complete_without_any_round_is_not_reported_as_work_done(self):
        self.assertEqual(SweepOutcome(status='complete').as_dict()['health'], 'no_work')

    def test_profile_failures_make_a_complete_run_degraded(self):
        outcome = SweepOutcome(status='complete', rounds=1, sessions=3, profile_failures=('profile-a',))
        self.assertEqual(outcome.as_dict()['health'], 'degraded_profile')

    def test_profile_failure_wins_over_no_work(self):
        self.assertEqual(SweepOutcome(status='complete', profile_failures=('p',)).as_dict()['health'], 'degraded_profile')

    def test_real_work_without_failures_is_ok(self):
        self.assertEqual(SweepOutcome(status='complete', rounds=1, sessions=2).as_dict()['health'], 'ok')

    def test_non_complete_status_is_its_own_health(self):
        for status in ('failed', 'deadline_reached', 'publication_pending', 'already_running', 'disabled', 'dry_run'):
            self.assertEqual(SweepOutcome(status=status).as_dict()['health'], status)


if __name__ == '__main__':
    unittest.main()
