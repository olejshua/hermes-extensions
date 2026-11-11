import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import email_realtime as realtime


class ManualReviewAlertTests(unittest.TestCase):
    def test_truncated_human_reply_is_not_silently_discarded(self):
        incoming = {
            'account': 'stealth', 'id': 'incoming', 'threadId': 'thread-1',
            'from': 'Moritz von Hesberg <moritz@example.com>',
            'subject': 'RE: Stealth Campus × DLA Piper',
            'snippet': 'Super, das passt. Schicken Sie Einwahldaten?',
        }
        thread = [{
            'id': 'incoming', 'labels': ['INBOX'], 'from': incoming['from'],
            'text': 'Super, das passt. Schicken Sie Einwahldaten?\n' + 'quoted history ' * 400,
            'truncated': True, 'cc': None,
        }]
        with patch.object(realtime, 'get_thread', return_value=(thread, False)):
            result = realtime.draft_one(incoming)
        self.assertEqual(result['status'], 'needs_manual_review_long_thread')
        state = {'notifications': []}
        realtime.queue_draft_notification(state, incoming, result)
        self.assertEqual(len(state['notifications']), 1)
        alert = state['notifications'][0]
        self.assertIn('Stealth', alert)
        self.assertIn('Moritz von Hesberg', alert)
        self.assertIn('Schicken Sie Einwahldaten?', alert)
        self.assertIn('manuell', alert.lower())
        self.assertIn('kein Entwurf', alert)


    def test_unverifiable_meeting_offer_triggers_manual_review_notification(self):
        incoming = {'account': 'code', 'id': 'meeting', 'from': 'Lucian Muchowski <contact-6@partner.example.invalid>',
                    'snippet': '12. Oktober 15:00 Uhr wäre passend.'}
        state = {'notifications': []}
        realtime.queue_draft_notification(state, incoming,
                                           {'status': 'needs_manual_review_scheduling'})
        self.assertEqual(len(state['notifications']), 1)
        alert = state['notifications'][0]
        self.assertIn('Lucian Muchowski', alert)
        self.assertIn('Termin', alert)
        self.assertIn('manuell', alert.lower())
        self.assertIn('kein Entwurf', alert)

if __name__ == '__main__':
    unittest.main()
