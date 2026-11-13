import datetime as dt
import tempfile
import unittest
from zoneinfo import ZoneInfo
from gtm.commitments import CommitmentStore, render_digest

NOW = dt.datetime.fromisoformat('2026-11-13T09:00:00+01:00')


class CommitmentTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        self.store = CommitmentStore(d.name + '/c.sqlite3')

    def test_requires_source_ref_and_offset(self):
        for args in (('x', ' '), (' ', 'm1')):
            with self.assertRaises(ValueError): self.store.add(*args)
        with self.assertRaises(ValueError): self.store.add('x', 'm1', '2026-11-13T10:00:00')

    def test_due_soon_overdue_and_far_future(self):
        self.store.add('Deck schicken', 'm1', '2026-11-13T17:00:00+01:00')
        self.store.add('Alt', 'm2', '2026-11-10T17:00:00+01:00')
        self.store.add('Später', 'm3', '2026-11-20T17:00:00+01:00')
        self.store.add('Ohne Datum', 'm4')
        self.assertEqual([r['ref'] for r in self.store.due_for_reminder(NOW)], ['m2', 'm1'])

    def test_reminder_only_marked_after_receipt_and_not_repeated_within_day(self):
        self.store.add('Deck schicken', 'm1', '2026-11-13T17:00:00+01:00')
        self.assertEqual(len(self.store.due_for_reminder(NOW)), 1)
        self.assertEqual(len(self.store.due_for_reminder(NOW)), 1)  # failed delivery: still due
        with self.assertRaises(ValueError): self.store.mark_reminded(['m1'], '', NOW)
        self.store.mark_reminded(['m1'], 'r-1', NOW)
        self.assertEqual(self.store.due_for_reminder(NOW + dt.timedelta(hours=3)), [])
        self.assertEqual(len(self.store.due_for_reminder(NOW + dt.timedelta(hours=25))), 1)

    def test_done_stops_reminders_and_duplicate_ref_ignored(self):
        self.store.add('A', 'm1', '2026-11-13T17:00:00+01:00'); self.store.add('B', 'm1', '2026-11-13T18:00:00+01:00')
        self.assertEqual(len(self.store.due_for_reminder(NOW)), 1)
        self.store.close('m1'); self.assertEqual(self.store.due_for_reminder(NOW), [])

    def test_digest_text(self):
        self.store.add('Deck schicken', 'm1', '2026-11-13T17:00:00+01:00')
        text = render_digest(self.store.due_for_reminder(NOW), NOW, ZoneInfo('Europe/Berlin'))
        self.assertIn('Deck schicken (fällig 13.11. 17:00)', text); self.assertIn('Nichts davon wurde nach außen', text)
        self.assertIsNone(render_digest([], NOW, ZoneInfo('Europe/Berlin')))


if __name__ == '__main__':
    unittest.main()
