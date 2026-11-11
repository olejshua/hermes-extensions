import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import email_realtime as rt
import email_watcher as w


class ReplySchedulerTests(unittest.TestCase):
    def setUp(self):
        self.item = {'account': 'stealth', 'id': 'incoming', 'threadId': 'thread-1'}
        self.thread = [{'id': 'incoming', 'labels': ['INBOX'], 'from': 'person@example.com',
                        'to': 'operator@secondary.example.invalid', 'cc': '', 'subject': 'Antwort',
                        'text': 'Sehr geehrter Herr Beispiel, vielen Dank für Ihre Nachricht.',
                        'truncated': False}]

    def _draft(self, body, thread=None, alias='stealth'):
        answer = {'body': body, 'priority': 'normal', 'incoming_summary': 'geantwortet',
                  'company': None, 'referral_recipients': []}
        with patch.object(rt, 'get_thread', return_value=(thread or self.thread, False)), \
             patch.object(rt, 'draft_worthy', return_value=True), \
             patch.object(rt, 'generate', return_value=answer), \
             patch.object(w, 'make_draft', return_value={'status': 'created', 'to': 'person@example.com'}) as make:
            rt.draft_one(dict(self.item, account=alias))
        return make.call_args.args[2]

    def test_formal_stealth_reply_adds_name_below_greeting(self):
        self.assertEqual(self._draft('Vielen Dank.\n\nBeste Grüße'),
                         'Vielen Dank.\n\nBeste Grüße\nOle Beispiel')

    def test_informal_reply_deduplicates_trailing_name(self):
        informal = [dict(self.thread[0], text='Hallo Alex, danke für deine Nachricht.')]
        self.assertEqual(self._draft('Ja, gern.\n\nLiebe Grüße\nOle Beispiel\n\nOle', informal),
                         'Ja, gern.\n\nBeste Grüße\nOle')

    def test_formal_sie_without_title_gets_full_name(self):
        formal = [dict(self.thread[0], text='Guten Tag, könnten Sie mir dazu etwas sagen?')]
        self.assertTrue(self._draft('Gerne.', formal, alias='code').endswith('Beste Grüße\nOle Beispiel'))

    def test_formal_addressee_overrides_informal_introducer(self):
        informal = [dict(self.thread[0], text='Hallo Alex, ich stelle dir Herrn Schenk vor.')]
        self.assertTrue(self._draft('Hallo Herr Schenk, gern.', informal).endswith('Beste Grüße\nOle Beispiel'))

    def test_calendar_options_exclude_both_calendar_conflicts(self):
        svc = MagicMock()
        svc.calendarList().list().execute.return_value = {'items': [
            {'id': 'operator@primary.example.invalid', 'primary': True, 'summary': 'Code'},
            {'id': 'imported-calendar', 'summary': 'Fuxam Calendar'}]}
        svc.freebusy().query().execute.return_value = {'calendars': {
            'operator@primary.example.invalid': {'busy': [
                {'start': '2026-10-07T13:30:00+02:00', 'end': '2026-10-07T13:50:00+02:00'}]},
            'imported-calendar': {'busy': [
                {'start': '2026-10-02T09:00:00+02:00', 'end': '2026-10-02T18:00:00+02:00'}]}}}
        with patch.object(rt, 'calendar_service', return_value=svc):
            slots = rt.calendar_options(now=datetime.fromisoformat('2026-09-29T12:00:00+02:00'))
        starts = {s['start'] for s in slots}
        self.assertFalse(any('2026-10-02' in start for start in starts))
        self.assertNotIn('2026-10-07T14:00:00+02:00', starts)
        self.assertIn('2026-10-09T10:00:00+02:00', starts)

    def test_schedule_requires_preverified_slot_and_exact_label(self):
        slot = {'start': '2026-10-09T10:00:00+02:00', 'label': 'Freitag, 09.10.2026, 10:00 Uhr'}
        answer = {'body': 'Hallo Lars,\n\nWürde dir {{SLOT}} passen?\n\nBeste Grüße',
                  'priority': 'normal', 'incoming_summary': 'hat einen Termin angeboten',
                  'company': None, 'question': None, 'proposed_slot': slot['start'],
                  'referral_recipients': []}
        def fake_run(args, **kwargs):
            prompt_text = Path(args[3]).read_text()
            self.assertIn(slot['label'], prompt_text)
            return SimpleNamespace(returncode=0, stdout=json.dumps(answer), stderr='')
        scheduling_thread = [dict(self.thread[0], text='Hallo Alex, Freitagvormittag würde passen.')]
        with patch.object(rt.subprocess, 'run', side_effect=fake_run), \
             patch.object(rt, 'calendar_options', return_value=[slot]):
            result = rt.generate('code', 'incoming', scheduling_thread, False)
        self.assertIn(slot['label'], result['body'])
        self.assertNotIn('{{SLOT}}', result['body'])

    def test_extracts_explicit_incoming_offer_before_quoted_history(self):
        text = ('12. Oktober 15:00 Uhr wäre passend.\n\n'
                'Von: Alex <operator@primary.example.invalid>\nMontag, 12. Oktober um 14:00 Uhr?')
        got = rt.extract_incoming_offer(text, datetime.fromisoformat('2026-10-05T19:00:00+02:00'))
        self.assertEqual(got.isoformat(), '2026-10-12T15:00:00+02:00')

    def test_quoted_gt_lines_are_not_treated_as_new_offer(self):
        text = 'Danke, ich kläre das.\n\n> 12. Oktober 15:00 Uhr wäre passend.'
        self.assertIsNone(rt.extract_incoming_offer(
            text, datetime.fromisoformat('2026-10-05T19:00:00+02:00')))

    def test_wrapped_gmail_reply_header_does_not_turn_quoted_timestamp_into_offer(self):
        text = ('Bitte nennen Sie mir zwei Termine.\r\r\n'
                '   Am\r\r\n   Mo, 5 Okt um  3:36 NACHMITTAGS\r\r\n'
                '   , Alex <operator@primary.example.invalid> schrieb:\r\r\n'
                '   12. Oktober 15:00 Uhr wäre passend.')
        self.assertIsNone(rt.extract_incoming_offer(
            text, datetime.fromisoformat('2026-10-06T17:00:00+02:00')))

    def test_accepts_exact_free_incoming_time_even_when_alternative_list_omits_it(self):
        offer = '2026-10-12T15:00:00+02:00'
        slots = [{'start': '2026-10-12T13:00:00+02:00', 'label': 'Montag, 12.10.2026, 13:00 Uhr'},
                 {'start': '2026-10-12T14:00:00+02:00', 'label': 'Montag, 12.10.2026, 14:00 Uhr'}]
        answer = {'body': 'Guten Tag Herr Muchowski,\n\n{{SLOT}} passt mir gut.',
                  'priority': 'normal', 'incoming_summary': 'hat einen Termin vorgeschlagen',
                  'company': None, 'question': None, 'proposed_slot': offer,
                  'referral_recipients': []}
        def fake_run(args, **kwargs):
            prompt = Path(args[3]).read_text()
            self.assertIn('Montag, 12.10.2026, 15:00 Uhr', prompt)
            self.assertIn('Vorauswahl', prompt)
            self.assertIn('frei', prompt)
            return SimpleNamespace(returncode=0, stdout=json.dumps(answer), stderr='')
        incoming = [dict(self.thread[0], text='12. Oktober 15:00 Uhr wäre passend.')]
        with patch.object(rt, 'calendar_options', return_value=slots), \
             patch.object(rt, 'check_incoming_availability', return_value=True, create=True), \
             patch.object(rt.subprocess, 'run', side_effect=fake_run):
            result = rt.generate('code', 'incoming', incoming, False,
                                 now=datetime.fromisoformat('2026-10-05T19:00:00+02:00'))
        self.assertIn('Montag, 12.10.2026, 15:00 Uhr passt mir gut', result['body'])

    def test_rejects_claim_of_conflict_for_verified_free_incoming_time(self):
        offer = '2026-10-12T15:00:00+02:00'
        answer = {'body': 'Guten Tag Herr Muchowski,\n\nDer Zeitpunkt {{SLOT}} ist für mich bereits belegt.',
                  'priority': 'normal', 'incoming_summary': 'hat einen Termin vorgeschlagen',
                  'company': None, 'question': None, 'proposed_slot': offer,
                  'referral_recipients': []}
        incoming = [dict(self.thread[0], text='12. Oktober 15:00 Uhr wäre passend.')]
        with patch.object(rt, 'calendar_options', return_value=[]), \
             patch.object(rt, 'check_incoming_availability', return_value=True, create=True), \
             patch.object(rt.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=0, stdout=json.dumps(answer), stderr='')):
            with self.assertRaises(rt.SchedulingReviewRequired):
                rt.generate('code', 'incoming', incoming, False,
                            now=datetime.fromisoformat('2026-10-05T19:00:00+02:00'))

    def test_exact_incoming_time_checks_both_calendars_with_buffer(self):
        svc = MagicMock()
        svc.calendarList().list().execute.return_value = {'items': [
            {'id': 'operator@primary.example.invalid', 'primary': True, 'summary': 'CODE'},
            {'id': 'imported-calendar', 'summary': 'Fuxam Calendar'}]}
        svc.freebusy().query().execute.return_value = {'calendars': {
            'operator@primary.example.invalid': {'busy': []},
            'imported-calendar': {'busy': [{'start': '2026-10-12T09:00:00+02:00',
                                            'end': '2026-10-12T10:45:00+02:00'}]}}}
        with patch.object(rt, 'calendar_service', return_value=svc):
            self.assertTrue(rt.check_incoming_availability(datetime.fromisoformat(
                '2026-10-12T15:00:00+02:00')))
        body = svc.freebusy().query.call_args.kwargs['body']
        self.assertEqual(body['timeMin'], '2026-10-12T14:45:00+02:00')
        self.assertEqual(body['timeMax'], '2026-10-12T15:30:00+02:00')
        self.assertEqual({i['id'] for i in body['items']},
                         {'operator@primary.example.invalid', 'imported-calendar'})

    def test_ambiguous_incoming_time_never_creates_draft(self):
        incoming = [dict(self.thread[0], text='Montag um 15 Uhr wäre passend.')]
        with patch.object(rt, 'get_thread', return_value=(incoming, False)), \
             patch.object(rt, 'draft_worthy', return_value=True), \
             patch.object(rt, 'calendar_options', return_value=[]), \
             patch.object(w, 'make_draft') as make, \
             patch.object(rt.subprocess, 'run') as model:
            result = rt.draft_one(dict(self.item, account='code'))
        self.assertEqual(result['status'], 'needs_manual_review_scheduling')
        make.assert_not_called()
        model.assert_not_called()

    def test_calendar_unavailable_requests_review_without_draft(self):
        incoming = [dict(self.thread[0], text='Freitagvormittag wäre möglich.')]
        with patch.object(rt, 'get_thread', return_value=(incoming, False)), \
             patch.object(rt, 'draft_worthy', return_value=True), \
             patch.object(rt, 'calendar_options', side_effect=RuntimeError('Google calendar unavailable')), \
             patch.object(w, 'make_draft') as make:
            result = rt.draft_one(dict(self.item, account='code'))
        self.assertEqual(result['status'], 'needs_manual_review_scheduling')
        make.assert_not_called()

    def test_free_incoming_offer_cannot_be_silently_dropped(self):
        answer = {'body': None, 'priority': 'normal', 'incoming_summary': 'hat einen Termin vorgeschlagen',
                  'company': None, 'question': None, 'proposed_slot': None, 'referral_recipients': []}
        incoming = [dict(self.thread[0], text='12. Oktober 15:00 Uhr wäre passend.')]
        with patch.object(rt, 'calendar_options', return_value=[]), \
             patch.object(rt, 'check_incoming_availability', return_value=True), \
             patch.object(rt.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=0, stdout=json.dumps(answer), stderr='')):
            with self.assertRaises(rt.SchedulingReviewRequired):
                rt.generate('code', 'incoming', incoming, False,
                            now=datetime.fromisoformat('2026-10-05T19:00:00+02:00'))

    def test_explicit_offer_is_reply_worthy_even_without_question_or_quote(self):
        incoming = dict(self.thread[0], subject='AW: Schule + Gebäudewirtschaft',
                        text='12. Oktober 15:00 Uhr wäre passend.')
        message = {'id': 'incoming', 'subject': incoming['subject']}
        self.assertTrue(rt.draft_worthy(message, [incoming]))

    def test_weekend_offer_is_not_automatically_accepted(self):
        offer = '2026-10-11T15:00:00+02:00'
        answer = {'body': '{{SLOT}} passt mir gut.', 'priority': 'normal',
                  'incoming_summary': 'hat einen Termin angeboten', 'company': None,
                  'question': None, 'proposed_slot': offer, 'referral_recipients': []}
        incoming = [dict(self.thread[0], text='11. Oktober 15:00 Uhr wäre passend.')]
        with patch.object(rt, 'calendar_options', return_value=[]), \
             patch.object(rt, 'check_incoming_availability', return_value=True), \
             patch.object(rt.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=0, stdout=json.dumps(answer), stderr='')):
            with self.assertRaises(rt.SchedulingReviewRequired):
                rt.generate('code', 'incoming', incoming, False,
                            now=datetime.fromisoformat('2026-10-05T19:00:00+02:00'))

    def test_offer_after_long_preamble_still_gets_exact_calendar_validation(self):
        offer = '2026-10-12T15:00:00+02:00'
        answer = {'body': '{{SLOT}} passt mir gut.', 'priority': 'normal',
                  'incoming_summary': 'hat einen Termin angeboten', 'company': None,
                  'question': None, 'proposed_slot': offer, 'referral_recipients': []}
        incoming = [dict(self.thread[0], text='Hintergrund. ' * 180 + '\n12. Oktober 15:00 Uhr wäre passend.')]
        with patch.object(rt, 'calendar_options', return_value=[]), \
             patch.object(rt, 'check_incoming_availability', return_value=True) as checked, \
             patch.object(rt.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=0, stdout=json.dumps(answer), stderr='')):
            result = rt.generate('code', 'incoming', incoming, False,
                                 now=datetime.fromisoformat('2026-10-05T19:00:00+02:00'))
        checked.assert_called_once()
        self.assertIn('12.10.2026, 15:00 Uhr', result['body'])

    def test_long_unquoted_offer_is_not_missed_by_reply_filter(self):
        incoming = dict(self.thread[0], subject='AW: Schule + Gebäudewirtschaft',
                        text='Hintergrund. ' * 180 + '\n12. Oktober 15:00 Uhr wäre passend.')
        self.assertTrue(rt.draft_worthy({'id': 'incoming', 'subject': incoming['subject']}, [incoming]))

    def test_acceptance_cannot_add_unverified_other_time(self):
        offer = '2026-10-12T15:00:00+02:00'
        answer = {'body': 'Ich bestätige {{SLOT}}; 16:00 Uhr ginge ebenfalls.',
                  'priority': 'normal', 'incoming_summary': 'hat einen Termin angeboten',
                  'company': None, 'question': None, 'proposed_slot': offer,
                  'referral_recipients': []}
        incoming = [dict(self.thread[0], text='12. Oktober 15:00 Uhr wäre passend.')]
        with patch.object(rt, 'calendar_options', return_value=[]), \
             patch.object(rt, 'check_incoming_availability', return_value=True), \
             patch.object(rt.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=0, stdout=json.dumps(answer), stderr='')):
            with self.assertRaises(rt.SchedulingReviewRequired):
                rt.generate('code', 'incoming', incoming, False,
                            now=datetime.fromisoformat('2026-10-05T19:00:00+02:00'))

    def test_unrecognized_clock_format_requires_review_instead_of_guessing(self):
        incoming = [dict(self.thread[0], text='October 12 at 3pm works for me.')]
        with patch.object(rt, 'calendar_options', return_value=[]), \
             patch.object(rt.subprocess, 'run') as model:
            with self.assertRaises(rt.SchedulingReviewRequired):
                rt.generate('code', 'incoming', incoming, False,
                            now=datetime.fromisoformat('2026-10-05T19:00:00+02:00'))
        model.assert_not_called()

    def test_unparsed_english_offer_still_reaches_manual_review(self):
        incoming = dict(self.thread[0], subject='AW: Schule + Gebäudewirtschaft',
                        text='October 12 at 3pm works for me.')
        self.assertTrue(rt.draft_worthy({'id': 'incoming', 'subject': incoming['subject']}, [incoming]))

    def test_shortlisted_slots_cannot_be_used_to_claim_unverified_unavailability(self):
        slot = {'start': '2026-10-13T16:00:00+02:00', 'label': 'Dienstag, 13.10.2026, 16:00 Uhr'}
        answer = {'body': 'Freitagvormittag passt mir leider nicht; Dienstag {{SLOT}} ginge.',
                  'priority': 'normal', 'incoming_summary': 'hat nach Freitag gefragt',
                  'company': None, 'question': None, 'proposed_slot': slot['start'],
                  'referral_recipients': []}
        incoming = [dict(self.thread[0], text='Freitagvormittag würde mir passen.')]
        with patch.object(rt, 'calendar_options', return_value=[slot]), \
             patch.object(rt.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=0, stdout=json.dumps(answer), stderr='')):
            with self.assertRaises(rt.SchedulingReviewRequired):
                rt.generate('code', 'incoming', incoming, False,
                            now=datetime.fromisoformat('2026-10-05T19:00:00+02:00'))

    def test_calendar_network_error_requests_review_without_draft(self):
        incoming = [dict(self.thread[0], text='Freitagvormittag wäre möglich.')]
        with patch.object(rt, 'get_thread', return_value=(incoming, False)), \
             patch.object(rt, 'draft_worthy', return_value=True), \
             patch.object(rt, 'calendar_options', side_effect=ConnectionError('offline')), \
             patch.object(rt.w, 'make_draft') as make_draft:
            result = rt.draft_one({'account': 'code', 'id': 'incoming', 'threadId': 'thread'})
        self.assertEqual(result['status'], 'needs_manual_review_scheduling')
        make_draft.assert_not_called()

    def test_busy_exact_offer_is_escalated_without_a_rejection_draft(self):
        incoming = [dict(self.thread[0], text='12. Oktober 15:00 Uhr wäre passend.')]
        with patch.object(rt, 'get_thread', return_value=(incoming, False)), \
             patch.object(rt, 'draft_worthy', return_value=True), \
             patch.object(rt, 'calendar_options', return_value=[]), \
             patch.object(rt, 'check_incoming_availability', return_value=False), \
             patch.object(rt.subprocess, 'run') as model, \
             patch.object(rt.w, 'make_draft') as make_draft:
            result = rt.draft_one({'account': 'code', 'id': 'incoming', 'threadId': 'thread'})
        self.assertEqual(result['status'], 'needs_manual_review_scheduling')
        model.assert_not_called()
        make_draft.assert_not_called()

    def test_rejects_unlisted_slot_and_question_asking_user_for_availability(self):
        slot = {'start': '2026-10-09T10:00:00+02:00', 'label': 'Freitag, 09.10.2026, 10:00 Uhr'}
        incoming = [dict(self.thread[0], text='Freitagvormittag würde mir passen.')]
        base = {'priority': 'normal', 'incoming_summary': 'hat Freitag angeboten',
                'company': None, 'referral_recipients': []}
        for answer in [dict(base, body='Würde dir {{SLOT}} passen?',
                            proposed_slot='2026-10-02T10:00:00+02:00'),
                       dict(base, body=None, question='Welcher Slot ist frei?')]:
            with self.subTest(answer=answer), \
                 patch.object(rt.subprocess, 'run', return_value=SimpleNamespace(
                     returncode=0, stdout=json.dumps(answer), stderr='')), \
                 patch.object(rt, 'calendar_options', return_value=[slot]):
                with self.assertRaises(ValueError):
                    rt.generate('code', 'incoming', incoming, False)


if __name__ == '__main__':
    unittest.main()
