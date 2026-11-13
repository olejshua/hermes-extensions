import datetime as dt
import tempfile
import unittest
from zoneinfo import ZoneInfo
from gtm.commitments import CommitmentStore, commitment_tick, render_digest

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

    def tick(self, sender, now=NOW, activate=True):
        return commitment_tick(self.store, sender, now, ZoneInfo('Europe/Berlin'), activate=activate)

    def seed(self):
        self.store.add('Deck schicken', 'm1', '2026-11-13T17:00:00+01:00')

    def test_dry_run_renders_without_send(self):
        self.seed(); sender = FakeSender()
        report = self.tick(sender, activate=False)
        self.assertEqual((report['due'], report['attempted'], sender.calls), (1, 0, 0)); self.assertIn('Deck', report['message'])

    def test_delivered_marks_reminded_once(self):
        self.seed(); sender = FakeSender({'success': True, 'message_id': 'r1'})
        self.assertEqual(self.tick(sender)['delivered'], 1)
        self.assertEqual(self.tick(sender, NOW + dt.timedelta(hours=1))['attempted'], 0)
        self.assertEqual(sender.calls, 1)

    def test_unknown_receipt_is_not_replayed_and_gets_cooldown(self):
        self.seed(); sender = FakeSender(RuntimeError('timeout'))
        self.assertEqual(self.tick(sender)['unknown'], 1)
        self.assertEqual(self.tick(sender, NOW + dt.timedelta(minutes=30))['attempted'], 0)
        self.assertEqual(sender.calls, 1)
        self.assertEqual(self.tick(FakeSender({'success': True, 'message_id': 'r2'}), NOW + dt.timedelta(hours=25))['delivered'], 1)

    def test_rejected_retries_after_wait_and_is_bounded(self):
        self.seed(); sender = FakeSender(*[{'success': False}] * 5)
        self.assertEqual(self.tick(sender)['rejected'], 1)
        self.assertEqual(self.tick(sender, NOW + dt.timedelta(minutes=5))['attempted'], 0)
        self.assertEqual(self.tick(sender, NOW + dt.timedelta(minutes=11))['rejected'], 1)
        self.assertEqual(self.tick(sender, NOW + dt.timedelta(minutes=22))['rejected'], 1)
        self.assertEqual(self.tick(sender, NOW + dt.timedelta(minutes=33))['attempted'], 0)  # still a bounded stop

    def test_concurrent_ticks_send_once(self):
        import threading
        self.seed(); sender = FakeSender(*[{'success': True, 'message_id': 'r'}] * 4)
        threads = [threading.Thread(target=lambda: self.tick(sender)) for _ in range(4)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(sender.calls, 1)

    def test_crashed_send_becomes_unknown_after_staleness(self):
        from gtm.commitments import _claim_digest
        self.seed()
        self.assertIsNotNone(_claim_digest(self.store, ['m1'], 'x', NOW.timestamp()))
        sender = FakeSender({'success': True, 'message_id': 'r'})
        self.assertEqual(self.tick(sender, NOW + dt.timedelta(minutes=1))['attempted'], 0)  # in flight
        self.assertEqual(self.tick(sender, NOW + dt.timedelta(minutes=10))['attempted'], 0)  # stale -> unknown: never replayed
        self.assertEqual(sender.calls, 0)
        self.assertEqual(self.tick(sender, NOW + dt.timedelta(hours=25))['attempted'], 1)  # comes back next day


class FakeSender:
    def __init__(self, *results): self.results, self.calls = list(results), 0
    def send(self, identity, message):
        self.calls += 1; r = self.results.pop(0)
        if isinstance(r, Exception): raise r
        return r


if __name__ == '__main__':
    unittest.main()
