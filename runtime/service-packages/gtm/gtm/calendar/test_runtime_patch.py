"""Review snapshot tests; no OAuth, services, subprocess model, or network."""
import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).parent/'runtime'
def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/(name+'.py'))
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module
    spec.loader.exec_module(module)
    return module
class Request:
    def __init__(self,result):self.result=result
    def execute(self):return self.result
class Calendar:
    def __init__(self, busy=None, list_extra=None):
        self.busy=busy if busy is not None else {'code':{'busy':[]},'fuxam':{'busy':[]}}
        self.list_extra=list_extra or {}
    def calendarList(self):return self
    def list(self,**kwargs):return Request({'items':[{'id':p.PRIMARY,'primary':True},{'id':'fuxam','summary':'Fuxam Calendar'}],**self.list_extra})
    def freebusy(self):return self
    def query(self,body):return Request({'calendars':{i['id']: self.busy['code' if i['id']==p.PRIMARY else 'fuxam'] for i in body['items']}})

# Standalone module import with the existing watcher replaced entirely.
w=types.ModuleType('email_watcher');w.HOME=Path('/fixture/not-a-real-hermes-home')
with patch.dict(sys.modules, {'email_watcher':w}):
    p=load('mail_policy');r=load('email_realtime')

class RuntimePatchTests(unittest.TestCase):
    def setUp(self):
        self.modules=patch.dict(sys.modules, {'mail_policy':p})
        self.modules.start();self.addCleanup(self.modules.stop)
        self.slot=datetime.fromisoformat('2026-10-12T15:00:00+02:00')
        self.now=datetime.fromisoformat('2026-10-12T08:00:00+02:00')
        self.pending=patch.object(p,'pending_busy',return_value=[]);self.pending.start()
        self.addCleanup(self.pending.stop)
    def test_lucian_15_is_free_even_when_shortlist_omits_it(self):
        with patch.object(r,'calendar_service',return_value=Calendar()):
            shortlist=r.calendar_options(now=self.now)
            self.assertNotIn(self.slot.isoformat(),[s['start'] for s in shortlist])
            self.assertTrue(r.check_incoming_availability(self.slot))
    def test_missing_fuxam_busy_blocks_review(self):
        with patch.object(r,'calendar_service',return_value=Calendar({'code':{'busy':[]},'fuxam':{}})):
            with self.assertRaises(r.SchedulingReviewRequired):r.check_incoming_availability(self.slot)
    def test_no_own_proposal_before_twelve(self):
        with patch.object(r,'calendar_service',return_value=Calendar()):
            self.assertTrue(all(datetime.fromisoformat(s['start']).hour>=12 for s in r.calendar_options(self.now)))
    def test_external_morning_still_allowed(self):
        with patch.object(r,'calendar_service',return_value=Calendar()):
            self.assertTrue(r.check_incoming_availability(self.slot.replace(hour=10)))
    def test_live_overlap_in_either_calendar_is_not_free(self):
        for cid in ('code','fuxam'):
            busy={'code':{'busy':[]},'fuxam':{'busy':[]}}
            busy[cid]['busy']=[{'start':'2026-10-12T15:00:00+02:00','end':'2026-10-12T15:30:00+02:00'}]
            with patch.object(r,'calendar_service',return_value=Calendar(busy)):
                self.assertFalse(r.check_incoming_availability(self.slot))
    def test_own_hold_path_preserved_with_exact_owner(self):
        owner=('code','thread-one',['person@example.invalid'])
        with patch.object(r,'calendar_service',return_value=Calendar()),patch.object(p,'own_hold_available',return_value=True) as own:
            self.assertTrue(r.check_incoming_availability(self.slot,owner))
            own.assert_called_once_with(unittest.mock.ANY,*owner,self.slot)
    def test_foreign_pending_hold_not_usable(self):
        with patch.object(r,'calendar_service',return_value=Calendar()),patch.object(p,'own_hold_available',return_value=None),patch.object(p,'pending_busy',return_value=[(self.slot,self.slot.replace(minute=30))]):
            self.assertFalse(r.check_incoming_availability(self.slot,('code','other-thread',['other@example.invalid'])))
    def test_incomplete_calendar_identity_blocks(self):
        with patch.object(r,'calendar_service',return_value=Calendar(list_extra={'nextPageToken':'page-two'})):
            with self.assertRaises(ValueError):r.check_incoming_availability(self.slot)
    def test_complete_calendar_pages_are_traversed(self):
        calendar=Calendar()
        pages=[{'items':[{'id':p.PRIMARY,'primary':True}],'nextPageToken':'two'},
               {'items':[{'id':'fuxam','summary':'Fuxam Calendar'}]}]
        with patch.object(calendar,'list',side_effect=[Request(x) for x in pages]) as listing:
            self.assertEqual(p._calendars(calendar),[p.PRIMARY,'fuxam'])
            self.assertEqual(listing.call_args_list[1].kwargs,{'pageToken':'two'})
    def test_pagination_cap_fails_closed(self):
        calendar=Calendar()
        pages=[{'items':[],'nextPageToken':str(i)} for i in range(20)]
        with patch.object(calendar,'list',side_effect=[Request(x) for x in pages]):
            with self.assertRaises(ValueError):p._calendars(calendar)
    def test_duplicate_fuxam_blocks(self):
        calendars=[{'id':p.PRIMARY,'primary':True},{'id':'f1','summary':'Fuxam Calendar'},{'id':'f2','summary':'Fuxam Calendar'}]
        with patch.object(r,'calendar_service',return_value=Calendar(list_extra={'items':calendars})):
            with self.assertRaises(ValueError):r.check_incoming_availability(self.slot)
    def test_reservation_check_rejects_missing_busy(self):
        with self.assertRaises(ValueError):p._check(Calendar({'code':{'busy':[]},'fuxam':{}}),self.slot,[p.PRIMARY,'fuxam'])

if __name__=='__main__':unittest.main()
