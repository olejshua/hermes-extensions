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


class StoredRunHealth(unittest.TestCase):
    def public(self, **row):
        from hermes_second_brain.dreaming.runner import _public_run
        return _public_run(dict(run_id='r', status='complete', rounds=0, **row))

    def test_stored_zero_round_run_is_no_work_and_keeps_old_fields(self):
        result = self.public(summary={'sessions': 0, 'profile_failures': 0})
        self.assertEqual((result['health'], result['status'], result['rounds']), ('no_work', 'complete', 0))

    def test_stored_profile_failures_degrade(self):
        self.assertEqual(self.public(summary={'sessions': 2, 'profile_failures': 1}, )['health'], 'degraded_profile')

    def test_stored_work_run_ok_and_missing_summary_safe(self):
        from hermes_second_brain.dreaming.runner import _public_run
        self.assertEqual(_public_run(dict(run_id='r', status='complete', rounds=2, summary={'sessions': 4}))['health'], 'ok')
        self.assertEqual(_public_run(dict(run_id='r', status='failed', rounds=1))['health'], 'failed')
        self.assertEqual(_public_run(None), None)
