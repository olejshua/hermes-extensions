import unittest
from dataclasses import replace
from datetime import datetime
from confirmation import Binding,Acceptance,plan_acribic_invitation
class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.b=Binding('code','thread','person@example.invalid',datetime.fromisoformat('2026-10-12T15:00:00+02:00'),20,'proposal')
        self.a=Acceptance(self.b,'acceptance','person@example.invalid',('INBOX',),True)
    def plan(self,**kw):
        return plan_acribic_invitation(self.b,kw.pop('accepted',self.a),availability=kw.pop('availability','free'),own_hold_readback_verified=kw.pop('own_hold_readback_verified',True),**kw)
    def test_twenty_minutes_and_three_verified_participants(self):
        body=self.plan()['body']
        self.assertEqual(body['end']['dateTime'],'2026-10-12T15:20:00+02:00')
        self.assertEqual(len(body['attendees']),3)
        self.assertEqual(body['summary'],'Austausch zu öffentlichen Ausschreibungen')
    def test_other_thread_cannot_accept_hold(self):
        with self.assertRaises(ValueError):self.plan(accepted=replace(self.a,binding=replace(self.b,thread_id='different')))
    def test_wrong_sender_cannot_accept(self):
        with self.assertRaises(ValueError):self.plan(accepted=replace(self.a,verified_sender='other@example.invalid'))
    def test_unverified_acceptance_cannot_plan(self):
        with self.assertRaises(ValueError):self.plan(accepted=replace(self.a,verified_exact_agreement=False))
    def test_unknown_availability_blocks(self):
        with self.assertRaises(ValueError):self.plan(availability='unknown')
    def test_unread_hold_blocks(self):
        with self.assertRaises(ValueError):self.plan(own_hold_readback_verified=False)
    def test_existing_invitation_blocks_duplicate(self):
        with self.assertRaises(ValueError):self.plan(existing_invitation_id='event')
    def test_draft_cannot_be_acceptance(self):
        with self.assertRaises(ValueError):self.plan(accepted=replace(self.a,verified_incoming_labels=('DRAFT',)))
if __name__=='__main__':unittest.main()
