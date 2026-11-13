import sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'gtm'))
from gtm.prep_daemon_entry import build_drafter  # noqa: E402
from gtm.prep_adapters import evidence_drafter, ReviewRequired  # noqa: E402
from gtm.synthesis import ModelPrepDrafter  # noqa: E402


class T(unittest.TestCase):
    def test_default_and_off_are_the_echo(self):
        self.assertIs(build_drafter({}), evidence_drafter)
        self.assertIs(build_drafter({'synthesis': {'mode': 'off'}}), evidence_drafter)

    def test_mode_without_completion_stops(self):
        for mode in ('dry_run', 'on'):
            with self.assertRaises(ReviewRequired):
                build_drafter({'synthesis': {'mode': mode}})

    def test_config_without_approval_stops(self):
        with self.assertRaises(ReviewRequired):
            build_drafter({'synthesis': {'mode': 'on'}}, lambda p: (lambda x: '{}'))

    def test_approved_mode_without_ledger_stops(self):
        with self.assertRaises(ReviewRequired):
            build_drafter({'synthesis': {'mode': 'on', 'transmission_approved': True}}, lambda p: (lambda x: '{}'))

    def test_unknown_mode_stops(self):
        with self.assertRaises(ReviewRequired):
            build_drafter({'synthesis': {'mode': 'auto'}})

    def test_injected_factory_builds_model_drafter(self):
        d = build_drafter({'synthesis': {'mode': 'dry_run', 'transmission_approved': True}}, lambda p: (lambda prompt: '{}'), ledger=object())
        self.assertIsInstance(d, ModelPrepDrafter); self.assertEqual(d.mode, 'dry_run')

if __name__ == '__main__':
    unittest.main()


class Quiet(unittest.TestCase):
    def test_quiet_window_wraps_midnight(self):
        import datetime as dt
        from gtm.prep import PrepConfig
        from gtm.prep_daemon_entry import outside_quiet_hours
        c = PrepConfig()
        at = lambda h, m: dt.datetime(2026, 11, 13, h, m)
        self.assertFalse(outside_quiet_hours(c, at(22, 0)))
        self.assertFalse(outside_quiet_hours(c, at(7, 59)))
        self.assertTrue(outside_quiet_hours(c, at(8, 0)))
        self.assertTrue(outside_quiet_hours(c, at(21, 29)))
