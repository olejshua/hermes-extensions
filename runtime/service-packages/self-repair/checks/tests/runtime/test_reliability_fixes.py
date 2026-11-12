"""Regression tests for the November 2026 reliability fixes.

Self-contained: every module under test is loaded from its repository path, so
the file runs both against this snapshot and on the host layout.
"""
import ast
import datetime as dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[6]
SCRIPTS = REPO / 'runtime/hermes-home/scripts'
GTM = REPO / 'runtime/service-packages/gtm'
for entry in (str(SCRIPTS), str(GTM)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from gtm import briefing_daemon as b  # noqa: E402
from gtm.calendar.routing import CODE  # noqa: E402
from mail_workflow import effort_for_phase  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fast_first = load('ole_fast_first_under_test', REPO / 'runtime/hermes-home/plugins/ole-dialogue/fast_first.py')

NOW = dt.datetime(2026, 10, 12, 10, 0, tzinfo=dt.timezone.utc)


def event(**change):
    result = dict(id='fictional-event', etag='v1', summary=b.ACRIBIC_TITLE, organizer={'email': b.CODE},
                  start={'dateTime': (NOW + dt.timedelta(minutes=15)).isoformat()},
                  attendees=[{'email': b.CODE}, {'email': b.PIET}, {'email': 'external@example.org'}])
    result.update(change)
    return result


def base_context(e):
    quote = 'Thomas Beispiel bei Example GmbH, Geschäftsführer. Sie: Interesse an Ausschreibungen.'
    return dict(calendar=e, messages=[dict(message_id='m1', internal_date='100', original=quote,
                                           labels=['SENT'], account=b.CODE, author=b.CODE,
                                           recipients=['external@example.org'],
                                           headers={'from': b.CODE, 'to': 'external@example.org'})])


def briefing_value(**change):
    reference = dict(message_id='m1', quote=base_context(event())['messages'][0]['original'])
    value = dict(abstain=False, person='Thomas Beispiel', person_email='external@example.org',
                 company='Example GmbH', role='Geschäftsführer',
                 company_context='In der Mail wird Example GmbH genannt.',
                 interest=None, conversation_context='Ausschreibungen',
                 questions=['Wie bearbeiten Sie Ausschreibungen?'], addressing='Sie', room=None,
                 evidence={k: reference for k in ['person', 'company', 'role', 'company_context',
                                                  'conversation_context', 'addressing']})
    value.update(change)
    return value


class PhaseEffortTests(unittest.TestCase):
    def test_defaults_keep_compose_and_review_thorough(self):
        self.assertEqual(effort_for_phase('understand'), 'medium')
        self.assertEqual(effort_for_phase('compose'), 'high')
        self.assertEqual(effort_for_phase('review'), 'high')

    def test_override_is_whitelisted(self):
        self.assertEqual(effort_for_phase('understand', 'low'), 'low')
        for bad in ('maximal', '', 3, True, ['low']):
            with self.subTest(override=bad), self.assertRaises(ValueError):
                effort_for_phase('compose', bad)

    def test_unknown_phase_rejected(self):
        with self.assertRaises(ValueError):
            effort_for_phase('guess')


class BriefingToleranceTests(unittest.TestCase):
    def test_unverifiable_interest_drops_to_evidenced_focus_with_caveat(self):
        ctx = base_context(event())
        value = briefing_value(interest='Interesse an Ausschreibungen')
        value['evidence']['interest'] = dict(message_id='m1', quote='Sie: Interesse an Ausschreibungen.')
        # The only "interest" evidence is our own SENT mail, never the
        # counterparty: previously the whole briefing died here.
        text = b.validate_output(json.dumps(value), ctx, starts_at=NOW)
        self.assertIn('Gesprächsfokus', text)
        self.assertNotIn('Konkretes Interesse:', text)
        self.assertIn('Das konkrete Interesse der Gegenpartei ist nicht belegt', text)
        self.assertIn('Thomas Beispiel', text)

    def test_interest_without_any_evidenced_focus_still_fails_closed(self):
        ctx = base_context(event())
        value = briefing_value(interest='Interesse an Ausschreibungen', conversation_context=None)
        value['evidence']['interest'] = dict(message_id='m1', quote='Sie: Interesse an Ausschreibungen.')
        with self.assertRaisesRegex(b.ReviewRequired, 'interest_requires_counterparty_original'):
            b.validate_output(json.dumps(value), ctx, starts_at=NOW)

    def test_counterparty_authored_interest_still_renders_as_before(self):
        ctx = base_context(event())
        ctx['messages'].append(dict(message_id='m2', internal_date='200',
                                    original='Ich habe konkretes Interesse an Ihrer Software.',
                                    labels=[], account=b.CODE, author='external@example.org',
                                    recipients=[b.CODE],
                                    headers={'from': 'external@example.org', 'to': b.CODE}))
        value = briefing_value(interest='Konkretes Interesse an der Software')
        value['evidence']['interest'] = dict(message_id='m2',
                                             quote='Ich habe konkretes Interesse an Ihrer Software.')
        text = b.validate_output(json.dumps(value), ctx, starts_at=NOW)
        self.assertIn('Konkretes Interesse:', text)
        self.assertNotIn('Gesprächsfokus', text)


class NoticeGapTests(unittest.TestCase):
    def due(self):
        return b.due_briefing(event(), calendar_id=CODE, category='acribic', now=NOW)

    def test_known_gap_is_named_in_notice(self):
        text = b.incomplete_notice(event(), self.due(), None,
                                   gap=b.notice_gap(b.ReviewRequired('briefing_abstained')))
        self.assertIn('Konkrete Lücke:', text)
        self.assertIn(b.NOTICE_GAPS['briefing_abstained'], text)

    def test_unknown_or_foreign_error_adds_no_detail(self):
        self.assertIsNone(b.notice_gap(b.ReviewRequired('some_other_code')))
        self.assertIsNone(b.notice_gap(ValueError('briefing_abstained')))
        self.assertIsNone(b.notice_gap(None))
        text = b.incomplete_notice(event(), self.due(), None, gap='raw exception text here')
        self.assertNotIn('raw exception text here', text)


class AbstainDeliveryTests(unittest.TestCase):
    """An abstaining model must still produce the enriched partial notice."""

    def test_abstain_delivers_named_gap_notice(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = b.Store(Path(tmp.name) / 'state')
        named = event(attendees=[{'email': b.CODE}, {'email': b.PIET},
                                 {'email': 'external@example.org', 'displayName': 'Thomas Beispiel'}])

        class Reads:
            def events(self, now):
                return [named]

            def current_event(self, id):
                return named

            def context(self, e, category):
                return base_context(e)

        class AbstainRuntime:
            def __init__(self):
                self.sent = []

            def model(self, ctx):
                return json.dumps({'abstain': True, 'reason': 'konkret fehlender Beleg'})

            def send(self, identity, message):
                self.sent.append(message)
                store.step(identity, 'delivered', receipt='fictional-receipt')
                return store.get(identity)

        runtime = AbstainRuntime()
        with patch.object(b.dt, 'datetime', wraps=dt.datetime) as clock:
            clock.now.return_value = NOW
            report = b.tick(Reads(), store, runtime, now=NOW, activate=True)
        self.assertEqual(report['incomplete_notices'], 1)
        self.assertEqual(len(runtime.sent), 1)
        self.assertIn('Thomas Beispiel', runtime.sent[0])
        self.assertIn('Konkrete Lücke:', runtime.sent[0])


class BriefingLeadTests(unittest.TestCase):
    def test_default_window_unchanged_and_longer_lead_opens_earlier(self):
        later = event(start={'dateTime': (NOW + dt.timedelta(minutes=30)).isoformat()})
        self.assertIsNone(b.due_briefing(later, calendar_id=CODE, category='acribic', now=NOW))
        due = b.due_briefing(later, calendar_id=CODE, category='acribic', now=NOW, lead_minutes=45)
        self.assertIsNotNone(due)
        # Identity stays bound to the real start: same key in both windows.
        at_15 = event()
        due_default = b.due_briefing(at_15, calendar_id=CODE, category='acribic', now=NOW)
        due_early = b.due_briefing(at_15, calendar_id=CODE, category='acribic', now=NOW, lead_minutes=45)
        self.assertEqual(due_default.identity, due_early.identity)

    def test_lead_validation(self):
        for bad in (0, 4, 241, True, '45'):
            with self.subTest(lead=bad), self.assertRaises(ValueError):
                b.due_briefing(event(), calendar_id=CODE, category='acribic', now=NOW, lead_minutes=bad)

    def test_tick_delivers_inside_extended_window_only_with_lead(self):
        later = event(start={'dateTime': (NOW + dt.timedelta(minutes=30)).isoformat()})

        class Reads:
            def events(self, now):
                return [later]

            def current_event(self, id):
                return later

            def context(self, e, category):
                return base_context(e)

        class Runtime:
            def __init__(self, store):
                self.store, self.sent = store, []

            def model(self, ctx):
                return json.dumps(dict(abstain=True, reason='kein Beleg'))

            def send(self, identity, message):
                self.sent.append(message)
                self.store.step(identity, 'delivered', receipt='fictional-receipt')
                return self.store.get(identity)

        for lead, expected in ((15, 0), (45, 1)):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            store = b.Store(Path(tmp.name) / 'state')
            runtime = Runtime(store)
            with patch.object(b.dt, 'datetime', wraps=dt.datetime) as clock:
                clock.now.return_value = NOW
                report = b.tick(Reads(), store, runtime, now=NOW, activate=True, lead_minutes=lead)
            self.assertEqual(len(runtime.sent), expected, lead)



class CasualSkipTests(unittest.TestCase):
    def test_short_casual_message_skips_preload(self):
        self.assertTrue(fast_first.casual_candidate('danke!'))
        self.assertTrue(fast_first.casual_candidate('Hi'))
        self.assertTrue(fast_first.casual_candidate('Guten Morgen'))
        self.assertTrue(fast_first.casual_candidate('ok 👍'))
        self.assertTrue(fast_first.casual_candidate('👍'))
        self.assertTrue(fast_first.casual_candidate('Alles klar, danke dir'))

    def test_project_keyword_long_multiline_or_empty_keep_full_preload(self):
        self.assertFalse(fast_first.casual_candidate('Block morgen 14-16 Uhr für Acribic'))
        self.assertFalse(fast_first.casual_candidate('Was läuft bei Stealth?'))
        self.assertFalse(fast_first.casual_candidate('x' * 161))
        self.assertFalse(fast_first.casual_candidate('Erste Zeile\nZweite Zeile'))
        self.assertFalse(fast_first.casual_candidate('   '))
        self.assertFalse(fast_first.casual_candidate(None))
        # Short but substantive: a question or instruction never skips the
        # preload, even without a project keyword.
        self.assertFalse(fast_first.casual_candidate('Hi, wie sieht morgen aus?'))
        self.assertFalse(fast_first.casual_candidate('verschieb den Termin auf 15 Uhr'))
        self.assertFalse(fast_first.casual_candidate('Danke, aber bitte kurz die Mail prüfen'))
        self.assertFalse(fast_first.casual_candidate('Hat er geantwortet?'))

    def test_keywords_match_casefolded_and_skip_defaults_on(self):
        self.assertFalse(fast_first.casual_candidate('ACRIBIC Update bitte'))
        instance = fast_first.FastFirst(object(), enabled=True)
        self.assertTrue(instance.casual_skip)
        disabled = fast_first.FastFirst(object(), enabled=True, casual_skip=False)
        self.assertFalse(disabled.casual_skip)


class DialogueStyleTests(unittest.TestCase):
    """The terse format is scoped to status/control replies, not substance."""

    def test_role_scopes_short_form_and_keeps_substance_complete(self):
        policy = load('ole_dialogue_policy_under_test',
                      REPO / 'runtime/hermes-home/plugins/ole-dialogue/policy.py')
        self.assertIn('Kurzform nur für Status-, Job- und Steuerungsantworten', policy.ROLE)
        self.assertIn('Inhaltliche Fragen vollständig und natürlich beantworten', policy.ROLE)
        # Safety constraints survive the style change.
        self.assertIn('konkreter Freigabe', policy.ROLE)
        self.assertIn('Keine Emojis', policy.ROLE)



if __name__ == '__main__':
    unittest.main()
