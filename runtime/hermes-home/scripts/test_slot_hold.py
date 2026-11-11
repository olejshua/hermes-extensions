import unittest
from datetime import datetime
from unittest.mock import Mock
import slot_hold as h
class NotFound(Exception):
    resp=type('Response',(),{'status':404})()
class SlotHoldTests(unittest.TestCase):
    def fixture(self,busy=False):
        api,state=Mock(),{}
        def get(**kw):
            r=Mock()
            if state:r.execute.return_value=state['body']
            else:r.execute.side_effect=NotFound()
            return r
        def insert(**kw):state.update(kw);return Mock()
        api.events.return_value.get.side_effect=get
        api.events.return_value.insert.side_effect=insert
        api.calendarList.return_value.list.return_value.execute.return_value={'items':[{'id':h.PRIMARY},{'id':'fuxam','summary':'Fuxam Calendar'}]}
        api.freebusy.return_value.query.return_value.execute.return_value={'calendars':{h.PRIMARY:{'busy':[]},'fuxam':{'busy':[{'start':'busy'}] if busy else []}}}
        return api,state
    def test_private_busy_30min_no_invitation_idempotent_across_timezones(self):
        api,state=self.fixture()
        sent={'id':'m1','threadId':'t1','labelIds':['SENT']}
        start=datetime.fromisoformat('2026-10-12T13:00:00+02:00')
        result=h.hold(api,'code',sent,start,'Thomas')
        self.assertEqual(result['end'],'2026-10-12T13:30:00+02:00')
        self.assertEqual(state['body']['transparency'],'opaque')
        self.assertEqual(state['body']['visibility'],'private')
        self.assertEqual(state['sendUpdates'],'none')
        self.assertNotIn('attendees',state['body'])
        again=h.hold(api,'code',sent,datetime.fromisoformat('2026-10-12T11:00:00+00:00'),'Thomas')
        self.assertEqual(again['event_id'],result['event_id'])
        api.events.return_value.insert.assert_called_once()
    def test_draft_refused(self):
        api=Mock()
        with self.assertRaisesRegex(ValueError,'sent message'):
            h.hold(api,'code',{'id':'m1','threadId':'t1','labelIds':['DRAFT']},datetime.fromisoformat('2026-10-12T13:00:00+02:00'),'Thomas')
        api.events.assert_not_called()
    def test_conflict_refused(self):
        api,state=self.fixture(busy=True)
        with self.assertRaisesRegex(ValueError,'conflicts'):
            h.hold(api,'code',{'id':'m1','threadId':'t1','labelIds':['SENT']},datetime.fromisoformat('2026-10-12T13:00:00+02:00'),'Thomas')
        api.events.return_value.insert.assert_not_called()
