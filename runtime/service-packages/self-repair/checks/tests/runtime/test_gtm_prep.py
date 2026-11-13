from datetime import datetime, timezone
import unittest
from gtm.prep import PrepConfig, PrepDue, due_preparation, render_prep, qualifies

OWN = ('me@example.invalid',)


def event(start='2026-11-13T14:00:00+01:00', end='2026-11-13T14:30:00+01:00', **extra):
    base = {'id': 'e1', 'summary': 'Fundraising Session', 'start': {'dateTime': start}, 'end': {'dateTime': end},
            'organizer': {'email': 'me@example.invalid'},
            'attendees': [{'email': 'me@example.invalid', 'self': True}, {'email': 'x@outside.invalid'}]}
    base.update(extra)
    return base


def due(stamp, ev=None, **kw):
    return due_preparation(ev or event(), calendar_id='cal', own_addresses=OWN,
                           now=datetime.fromisoformat(stamp), **kw)


class PrepTests(unittest.TestCase):
    def test_window_opens_lead_hours_before_and_closes_before_start(self):
        self.assertIsNone(due('2026-11-12T19:59:00+01:00'))  # 18h lead = 20:00 previous day, but quiet hours
        result = due('2026-11-13T08:00:00+01:00')
        self.assertIsInstance(result, PrepDue)
        self.assertIsNone(due('2026-11-13T13:30:00+01:00'))

    def test_never_due_in_quiet_hours(self):
        ev = event('2026-11-13T09:00:00+01:00', '2026-11-13T09:30:00+01:00')
        self.assertIsNone(due('2026-11-12T23:00:00+01:00', ev))
        self.assertIsInstance(due('2026-11-12T16:00:00+01:00', ev), PrepDue)

    def test_morning_event_gets_evening_before_when_window_tight(self):
        ev = event('2026-11-13T08:30:00+01:00', '2026-11-13T09:00:00+01:00')
        self.assertIsInstance(due("2026-11-12T20:00:00+01:00", ev, config=PrepConfig(lead_hours=6)), PrepDue)
        self.assertIsNone(due('2026-11-12T19:00:00+01:00', ev, config=PrepConfig(lead_hours=6)))

    def test_identity_ignores_rsvp_but_not_reschedule(self):
        key = due('2026-11-13T09:00:00+01:00').identity
        ev = event(etag='new', sequence=5)
        self.assertEqual(key, due('2026-11-13T09:00:00+01:00', ev).identity)
        moved = event('2026-11-13T15:00:00+01:00', '2026-11-13T15:30:00+01:00')
        self.assertNotEqual(key, due('2026-11-13T09:00:00+01:00', moved).identity)

    def test_routine_cancelled_declined_and_allday_not_prepared(self):
        for extra in ({'summary': 'Luma Event'}, {'summary': 'Blocker: Vorschlag'}, {'status': 'cancelled'},
                      {'summary': 'ANRUFEN: Firma'}):
            self.assertIsNone(qualifies(event(**extra), OWN), extra)
        declined = event(attendees=[{'email': 'me@example.invalid', 'self': True, 'responseStatus': 'declined'}])
        self.assertIsNone(qualifies(declined, OWN))
        self.assertIsNone(qualifies({'id': 'a', 'summary': 'Fundraising', 'start': {'date': '2026-11-13'}, 'end': {'date': '2026-11-14'}}, OWN))
        self.assertIsNone(qualifies(event(end='2026-11-13T14:05:00+01:00'), OWN))

    def test_reasons(self):
        self.assertIsNone(qualifies(event(summary='Austausch'), OWN))  # default: outside attendees alone do not qualify
        self.assertEqual(qualifies(event(summary='Austausch'), OWN, PrepConfig(outside_attendees_qualify=True)), 'outside_attendees')
        self.assertEqual(qualifies(event(summary='Gym', attendees=[]), OWN, PrepConfig(important_terms=('gym',))), 'owner_named_topic')
        self.assertIsNone(qualifies(event(summary='Austausch', attendees=[]), OWN))
        self.assertEqual(qualifies(event(summary='Pre-Seed Session'), OWN), 'important_title')

    def test_owner_named_event_bypasses_defaults(self):
        short = event(summary='Blocker Vorschlag', end='2026-11-13T14:05:00+01:00')
        self.assertIsNone(qualifies(short, OWN))
        self.assertEqual(qualifies(short, OWN, PrepConfig(important_event_ids=('e1',))), 'owner_named_event')
        self.assertEqual(qualifies(short, OWN, PrepConfig(important_terms=('vorschlag',))), 'owner_named_topic')
        cancelled = dict(short, status='cancelled')
        self.assertIsNone(qualifies(cancelled, OWN, PrepConfig(important_event_ids=('e1',))))

    def test_defaults_are_distinguishable_from_configured_settings(self):
        cfg = PrepConfig(lead_hours=24, configured=frozenset({'lead_hours'}))
        self.assertEqual((cfg.source('lead_hours'), cfg.source('quiet_start')), ('configured', 'default'))

    def test_stranger_invite_with_important_title_does_not_qualify(self):
        stranger = event(organizer={'email': 'x@outside.invalid'},
                         attendees=[{'email': 'me@example.invalid', 'self': True, 'responseStatus': 'needsAction'}])
        self.assertIsNone(qualifies(stranger, OWN))
        stranger['attendees'][0]['responseStatus'] = 'accepted'
        self.assertEqual(qualifies(stranger, OWN), 'important_title')

    def test_missed_window_is_explicit_not_delivered(self):
        result = due('2026-11-13T13:00:00+01:00', max_delay_seconds=600)
        self.assertEqual(result['state'], 'missed_prep_window'); self.assertFalse(result['auto_delivery'])

    def test_naive_clock_rejected(self):
        with self.assertRaises(ValueError):
            due_preparation(event(), calendar_id='cal', own_addresses=OWN, now=datetime(2026, 11, 13, 9))

    def test_outside_attendees_exclude_self_and_resources(self):
        ev = event(attendees=[{'email': 'me@example.invalid'}, {'email': 'room@r.invalid', 'resource': True},
                              {'email': 'D@outside.invalid'}])
        self.assertEqual(due('2026-11-13T09:00:00+01:00', ev).outside_attendees, ('d@outside.invalid',))

    def test_render_requires_evidence_and_stays_private(self):
        text = render_prep(time_label='Fr 14:00', title='Fundraising Session', people=['David'],
                           known=['Letzte Mail zum Pre-Seed vom 12.11.'], open_questions=['Welche Summe nennst du?'],
                           gaps=['Keine Notiz zum letzten Gespräch.'])
        self.assertIn('Nichts davon ist nach außen gegangen', text)
        self.assertNotIn('\u2014', text)
        for bad in (dict(known=[]), dict(open_questions=[]), dict(open_questions=['']), dict(time_label=' ')):
            args = dict(time_label='Fr 14:00', title='T', people=[], known=['a'], open_questions=['q']); args.update(bad)
            with self.assertRaises(ValueError):
                render_prep(**args)


if __name__ == '__main__':
    unittest.main()
