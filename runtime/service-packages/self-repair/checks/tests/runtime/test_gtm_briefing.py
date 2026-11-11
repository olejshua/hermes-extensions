from datetime import datetime, timezone
import unittest
from gtm.briefing import BriefingDue, due_briefing, render_briefing


class BriefingTests(unittest.TestCase):
    def setUp(self):
        self.event = {'id': 'event', 'start': {'dateTime': '2026-10-12T16:00:00+02:00'}, 'attendees': []}

    def due(self, stamp, **kwargs):
        return due_briefing(self.event, calendar_id='code', category=kwargs.pop('category', 'acribic'), now=datetime.fromisoformat(stamp), **kwargs)

    def test_exact_timing_and_offset(self):
        self.assertIsNone(self.due('2026-10-12T13:44:59+00:00'))
        result = self.due('2026-10-12T15:45:00+02:00')
        self.assertIsInstance(result, BriefingDue)
        self.assertEqual(result.starts_at, datetime(2026, 10, 12, 14, tzinfo=timezone.utc))
        self.assertEqual(result.late_seconds, 0)

    def test_rsvp_change_no_duplicate_but_reschedule_new_identity(self):
        key = self.due('2026-10-12T15:45:00+02:00').identity
        self.event['etag'] = 'changed'; self.event['sequence'] = 44
        self.assertEqual(key, self.due('2026-10-12T15:45:00+02:00').identity)
        self.event['start']['dateTime'] = '2026-10-12T17:00:00+02:00'
        self.assertNotEqual(key, self.due('2026-10-12T16:45:00+02:00').identity)

    def test_missed_deadline_and_private_unknown_calls_not_claimed_delivered(self):
        result = self.due('2026-10-12T15:55:00+02:00', max_delay_seconds=120)
        self.assertEqual(result['state'], 'missed_briefing_deadline'); self.assertFalse(result['auto_delivery'])
        self.assertIsNone(self.due('2026-10-12T15:45:00+02:00', category='personal'))
        self.assertIsNone(self.due('2026-10-12T15:45:00+02:00', category=None))
        self.assertIsNone(self.due('2026-10-12T16:00:00+02:00'))

    def test_restart_catches_up_until_actual_call_start(self):
        self.assertIsInstance(self.due('2026-10-12T15:55:00+02:00'), BriefingDue)
        self.assertIsNone(self.due('2026-10-12T16:00:00+02:00'))

    def test_cancelled_declined_and_all_day(self):
        self.event['status'] = 'cancelled'; self.assertIsNone(self.due('2026-10-12T15:45:00+02:00'))
        self.event.pop('status'); self.event['attendees'] = [{'self': True, 'responseStatus': 'declined'}]
        self.assertIsNone(self.due('2026-10-12T15:45:00+02:00'))
        self.event['attendees'] = []; self.event['start'] = {'date': '2026-10-12'}
        self.assertIsNone(self.due('2026-10-12T15:45:00+02:00'))

    def test_naive_time_refused_and_actual_briefing_contract(self):
        self.event['start']['dateTime'] = '2026-10-12T16:00:00'
        with self.assertRaises(ValueError): self.due('2026-10-12T15:45:00+02:00')
        text = render_briefing(time_label='16:00 Uhr', person='Kontakt', role='Leitung', company='Firma', company_context='Betreut Ausschreibungen.', interest='Möchte den Ablauf besprechen.', questions=['Wie läuft die Abgabe?'], addressing='Sie', room='B.12')
        self.assertEqual(text.count('Raum:'), 1)
        self.assertTrue(text.endswith('Sag mir Bescheid, wenn der Call durch ist.'))
        self.assertNotIn('Warum ihr sprecht', text)


if __name__ == '__main__': unittest.main()
