"""mail_workflow.prepare: the model sees the whole thread, and drafts only use verified free slots."""
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[5] / 'hermes-home/scripts'))
import mail_workflow as mw

BERLIN = ZoneInfo('Europe/Berlin')
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=BERLIN)
SLOT = '2026-11-17T10:00:00+01:00'
AUTHORED = 'Gerne zeitnah. Wie wäre Dienstag, 17.11. um 10:00 Uhr?\n> Ihre Mail vom Montag, 09.11.'
THREAD = [{'id': 'a', 'internalDate': '1762675200000', 'text': 'Erste Mail mit Kontext und Preisfrage'},
          {'id': 'b', 'internalDate': '1762900000000', 'text': 'Unsere Antwort zu den Preisen'},
          {'id': 'c', 'internalDate': '1763000000000', 'text': AUTHORED}]


def understanding(**over):
    value = {'kind': 'personal_reply', 'reply_needed': True, 'notify': True, 'invitation_requested': False,
             'scheduling': 'offers', 'summary': 'Terminvorschlag Dienstag', 'company': 'Beispiel GmbH', 'requested_options': 1,
             'offers': [{'start': SLOT, 'end': None, 'quote': 'Dienstag, 17.11. um 10:00 Uhr'}]}
    value.update(over); return value


class Model:
    def __init__(self, first, second): self.first, self.second, self.contexts = first, second, []
    def __call__(self, phase, context):
        self.contexts.append((phase, context)); return self.first if phase == 'understand' else self.second


def prepare(model, exact=lambda start, **kw: True, options=lambda **kw: []):
    return mw.prepare('code', 'c', THREAD, now=NOW, authored=AUTHORED, model=model, exact=exact, options=options)


class FullThread(unittest.TestCase):
    def test_both_model_phases_receive_every_message_and_draft_uses_verified_slot(self):
        model = Model(understanding(), {'body': 'Gerne, {{SLOT}} passt gut.', 'question': None, 'proposed_slots': [SLOT]})
        result = prepare(model)
        for phase, context in model.contexts:
            self.assertEqual([m['id'] for m in context['thread']], ['a', 'b', 'c'], phase)
        self.assertIn('Dienstag, 17.11.2026, 10:00 Uhr', result['body']); self.assertNotIn('{{SLOT', result['body'])

    def test_unverified_slot_in_draft_is_rejected(self):
        model = Model(understanding(), {'body': 'Gerne, {{SLOT}}.', 'question': None, 'proposed_slots': ['2026-11-18T10:00:00+01:00']})
        with self.assertRaises(mw.MailWorkflowError): prepare(model)

    def test_occupied_offer_requires_new_window_question_not_a_time(self):
        model = Model(understanding(), {'body': 'Leider passt das nicht, aber am 19.11. um 11 Uhr.', 'question': None})
        with self.assertRaises(mw.MailWorkflowError) as caught: prepare(model, exact=lambda start, **kw: False)
        self.assertEqual(str(caught.exception), 'unavailable_offer_needs_new_window')

    def test_time_quoted_from_earlier_mail_is_not_a_current_offer(self):
        bad = understanding(offers=[{'start': '2026-11-09T10:00:00+01:00', 'end': None, 'quote': 'Montag, 09.11.'}])
        with self.assertRaises(mw.MailWorkflowError): prepare(Model(bad, {}))

    def test_invitation_sent_claim_is_rejected_without_a_receipt(self):
        model = Model(understanding(), {'body': 'Ich habe die Kalendereinladung gerade geschickt. {{SLOT}}', 'question': None, 'proposed_slots': [SLOT]})
        with self.assertRaises(mw.MailWorkflowError) as caught: prepare(model)
        self.assertEqual(str(caught.exception), 'invitation_send_claim_unverified')


if __name__ == '__main__':
    unittest.main()
