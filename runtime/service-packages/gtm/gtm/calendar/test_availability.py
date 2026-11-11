import copy
import unittest
from datetime import datetime, timedelta
from availability import assess_exact_slot, Status

class ExactSlotTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime.fromisoformat('2026-10-12T15:00:00+02:00')
        self.now = datetime.fromisoformat('2026-10-12T10:00:00+02:00')
        self.response = {'timeMin':'2026-10-12T12:45:00Z', 'timeMax':'2026-10-12T13:30:00Z',
                         'calendars':{'code':{'busy':[]},'fuxam':{'busy':[]}}}
    def check(self, response=None, **kwargs):
        return assess_exact_slot(self.start, response or self.response, ('code','fuxam'),
                                 checked_at=kwargs.pop('checked_at',self.now), now=self.now, **kwargs).status
    def test_exact_offer_free_even_if_not_in_shortlist(self):
        self.assertEqual(self.check(),Status.FREE)
    def test_missing_busy_is_unknown(self):
        del self.response['calendars']['fuxam']['busy']
        self.assertEqual(self.check(),Status.UNKNOWN)
    def test_missing_calendar_is_unknown(self):
        del self.response['calendars']['fuxam']
        self.assertEqual(self.check(),Status.UNKNOWN)
    def test_provider_error_is_unknown(self):
        self.response['calendars']['code']['errors']=[{'reason':'notFound'}]
        self.assertEqual(self.check(),Status.UNKNOWN)
    def test_either_calendar_conflict_is_busy(self):
        for cid in ('code','fuxam'):
            r=copy.deepcopy(self.response)
            r['calendars'][cid]['busy']=[{'start':'2026-10-12T13:00:00Z','end':'2026-10-12T13:15:00Z'}]
            self.assertEqual(self.check(r),Status.BUSY)
    def test_adjacent_event_at_upper_boundary_is_free(self):
        self.response['calendars']['code']['busy']=[{'start':'2026-10-12T13:30:00Z','end':'2026-10-12T14:00:00Z'}]
        self.assertEqual(self.check(),Status.FREE)
    def test_partial_query_is_unknown(self):
        self.response['timeMin']='2026-10-12T13:00:00Z'
        self.assertEqual(self.check(),Status.UNKNOWN)
    def test_stale_query_is_unknown(self):
        self.assertEqual(self.check(checked_at=self.now-timedelta(minutes=2)),Status.UNKNOWN)
    def test_naive_datetime_is_unknown(self):
        self.start=self.start.replace(tzinfo=None)
        self.assertEqual(self.check(),Status.UNKNOWN)
    def test_foreign_pending_hold_is_busy(self):
        self.assertEqual(self.check(pending=[(self.start,self.start+timedelta(minutes=30))]),Status.BUSY)
    def test_invalid_interval_is_unknown(self):
        self.response['calendars']['fuxam']['busy']=[{'start':'2026-10-12T13:30:00Z','end':'2026-10-12T13:00:00Z'}]
        self.assertEqual(self.check(),Status.UNKNOWN)

if __name__ == '__main__': unittest.main()
