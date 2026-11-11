"""Source-bound constraints reach the native reader before its result cap."""
import copy
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from types import SimpleNamespace
import sys
import pytest
from mail_fixtures import runtime, message, interpretation, models
from mail_workflow import validate_understanding, calendar_facts, MailWorkflowError

NOW = datetime.fromisoformat('2026-11-01T16:00:00+01:00')
QUOTE = 'Zeitlich ist das allerdings erst ab dem 10. November wieder möglich.'
BOUND = {'not_before': '2026-11-10T00:00:00+01:00', 'not_after': None, 'quote': QUOTE, 'excluded': []}


def native(r):
    requests = []
    class Calendar:
        def freebusy(self): return self
        def query(self, body): requests.append(body); return self
        def execute(self): return {'calendars': {'code': {'busy': []}, 'fuxam': {'busy': []}}}
    r.calendar_service = lambda: Calendar()
    policy = SimpleNamespace(_calendars=lambda _: ('code', 'fuxam'), pending_busy=lambda: [], project_from_thread=lambda alias, thread: 'stealth')
    return requests, policy


def test_original_lower_bound_survives_full_preparation_and_native_calendar(tmp_path):
    r = runtime(tmp_path)
    text = QUOTE + ' Sollte das für Sie noch passen, melden Sie sich gerne hinsichtlich eines Termins.'
    item, thread = message(text, 'stealth')
    thread[-1]['internalDate'] = str(int(NOW.timestamp()*1000))
    intent = interpretation(mode='request'); intent.update(availability=BOUND, requested_options=0)
    slots = ['2026-11-10T12:00:00+01:00', '2026-11-11T12:00:00+01:00']
    models(r, intent, dict(body='Gerne. Passt Ihnen {{SLOT_1}} oder {{SLOT_2}}?', question=None,
                         proposed_slots=slots, referral_recipients=[]))
    requests, policy = native(r); r.calendar_options = r.live_calendar_options
    journal = {}
    with patch.dict(sys.modules, {'mail_policy': policy}):
        result = r.generate('stealth', item['id'], thread, False, now=NOW, journal=lambda **v: journal.update(v))
    assert result['standard_verified'] and result['proposed_slots'] == slots
    assert requests and all({x['id'] for x in q['items']} == {'code', 'fuxam'} for q in requests)
    assert datetime.fromisoformat(requests[0]['timeMin']) >= datetime.fromisoformat(BOUND['not_before'])-timedelta(minutes=15)
    assert all(datetime.fromisoformat(x['start']) >= datetime.fromisoformat(BOUND['not_before']) for x in journal['calendar']['choices'])
    assert journal['understanding']['requested_options'] == 2
    assert len(journal['calendar']['choices']) == 12
    r.w.make_draft.assert_not_called()


def test_excluded_interval_is_filtered_before_twelve_choice_cap(tmp_path):
    r = runtime(tmp_path); requests, policy = native(r)
    bounds = {**BOUND, 'not_after': '2026-11-18T00:00:00+01:00', 'excluded': [
        {'start': '2026-11-10T00:00:00+01:00', 'end': '2026-11-13T00:00:00+01:00', 'quote': 'aber nicht vom 10. bis zum 12.'}]}
    with patch.dict(sys.modules, {'mail_policy': policy}):
        choices = r.live_calendar_options(now=NOW, availability=bounds)
    assert choices and all(datetime.fromisoformat(x['start']).day >= 13 for x in choices)
    assert all(datetime.fromisoformat(x['start'])+timedelta(minutes=30) <= datetime.fromisoformat(bounds['not_after']) for x in choices)
    assert all(datetime.fromisoformat(x['start']).weekday() < 5 for x in choices)


def test_bounds_apply_to_exact_and_window_offers_and_injected_owned_reader():
    text = 'Am 10. November von 12-16 Uhr, aber nicht 12-14 Uhr.'
    bounds = {**BOUND, 'quote': text, 'excluded': [
        {'start':'2026-11-10T12:00:00+01:00','end':'2026-11-10T14:00:00+01:00','quote':'nicht 12-14 Uhr'}]}
    intent = interpretation(mode='offers', offers=[{'start':'2026-11-10T12:00:00+01:00','end':'2026-11-10T16:00:00+01:00','quote':'10. November von 12-16 Uhr'}])
    intent.update(availability=bounds)
    valid = validate_understanding(intent, text, NOW)
    options = Mock(return_value=[{'start':s,'label':s} for s in ['2026-11-10T12:00:00+01:00','2026-11-10T14:00:00+01:00']])
    facts = calendar_facts(valid, now=NOW, owner=None, exact=Mock(return_value=True), options=options)
    assert [x['start'] for x in facts['choices']] == ['2026-11-10T14:00:00+01:00']
    assert options.call_args.kwargs['availability'] == bounds


@pytest.mark.parametrize('change', ['unquoted', 'naive', 'empty', 'backwards', 'bad_exclusion'])
def test_unverified_constraints_stop_before_any_calendar(change):
    intent = interpretation(mode='request'); intent['availability'] = copy.deepcopy(BOUND)
    if change == 'unquoted': intent['availability']['quote'] = 'erfunden'
    if change == 'naive': intent['availability']['not_before'] = '2026-11-10T00:00:00'
    if change == 'empty': intent['availability']['not_before'] = None
    if change == 'backwards': intent['availability']['not_after'] = '2026-11-09T00:00:00+01:00'
    if change == 'bad_exclusion': intent['availability']['excluded'] = [{'start':BOUND['not_before'],'end':'2026-11-11T00:00:00+01:00','quote':'nicht belegt'}]
    with pytest.raises(MailWorkflowError): validate_understanding(intent, QUOTE, NOW)


def test_past_upper_bound_does_not_query_calendar(tmp_path):
    r = runtime(tmp_path); r.calendar_service = Mock(side_effect=AssertionError('unnecessary lookup'))
    assert r.live_calendar_options(now=NOW, availability={**BOUND,'not_before':None,'not_after':'2026-10-31T00:00:00+01:00'}) == []
    r.calendar_service.assert_not_called()
