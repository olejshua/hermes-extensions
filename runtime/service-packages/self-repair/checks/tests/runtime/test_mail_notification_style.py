"""Item 4: exercise the real mail notification decision+wording code with
synthetic relevant/irrelevant fixtures (no private mail, no live sends).

Verifies the user's acceptance style: one to two natural German sentences, no
mail copies, emojis, full addresses or technical identifiers - and that
routine/irrelevant mails stay silent.
"""
import re
import sys
import types
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[5] / 'hermes-home/scripts'


def load_module(tmp_home):
    # Host-only neighbors are stubbed; only notification composition runs.
    watcher = types.ModuleType('email_watcher')
    watcher.HOME = tmp_home
    workflow = types.ModuleType('mail_workflow')
    workflow.MailWorkflowError = type('MailWorkflowError', (Exception,), {})
    workflow.prepare = None
    status = types.ModuleType('mail_model_status')
    status.failure_from_stderr = None
    saved = {name: sys.modules.get(name) for name in
             ('email_watcher', 'mail_workflow', 'mail_model_status')}
    sys.modules.update({'email_watcher': watcher, 'mail_workflow': workflow,
                        'mail_model_status': status})
    sys.path.insert(0, str(SCRIPTS))
    try:
        import email_realtime
        return email_realtime
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def message(sender_display):
    return {'account': 'code', 'id': 'synthetic-1', 'threadId': 't-1',
            'from': sender_display + ' <kontakt@example.org>'}


def notifications(module, state):
    return [item['text'] if isinstance(item, dict) else item
            for item in state.get('notifications', [])]


class NotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.mkdtemp()
        cls.er = load_module(Path(cls.tmp))

    def fresh(self):
        return {}

    def test_relevant_statuses_notify(self):
        er = self.er
        s = self.fresh()
        m = message('Maria Beispiel')
        base = {'notify': True, 'priority': 'high', 'company': 'Beispiel GmbH'}
        er.queue_draft_notification(s, m, {**base, 'status': 'important_information',
                                           'case_key': 'c1', 'notification_summary': 'hat Rückfragen zum Angebot'})
        er.queue_draft_notification(s, m, {**base, 'status': 'created', 'case_key': 'c2'})
        er.queue_draft_notification(s, m, {**base, 'status': 'needs_user_input',
                                           'case_key': 'c3', 'question': 'Passt dir Dienstag um 10 Uhr?'})
        er.queue_draft_notification(s, m, {**base, 'status': 'needs_manual_review_draft_state', 'case_key': 'c4'})
        er.queue_draft_notification(s, m, {**base, 'status': 'workflow_blocked', 'case_key': 'c5',
                                           'notification_summary': 'wartet auf die Unterlagen'})
        texts = notifications(er, s)
        self.assertEqual(len(texts), 5)
        self.assertEqual(texts[0], 'Maria Beispiel (Beispiel GmbH) hat Rückfragen zum Angebot.')
        self.assertEqual(texts[1], 'Der Antwortentwurf für Maria Beispiel (Beispiel GmbH) ist bereit.')
        self.assertEqual(texts[2], 'Maria Beispiel (Beispiel GmbH): Passt dir Dienstag um 10 Uhr?')
        self.assertEqual(texts[3], 'Ich konnte den gespeicherten Entwurf für Maria Beispiel (Beispiel GmbH) '
                                   'noch nicht bestätigen. Der Fall bleibt zur Prüfung erhalten.')
        self.assertEqual(texts[4], 'Maria Beispiel (Beispiel GmbH) wartet auf die Unterlagen. '
                                   'Der geprüfte Antwortentwurf fehlt noch.')

    def test_irrelevant_and_routine_stay_silent(self):
        er = self.er
        s = self.fresh()
        m = message('Luma')
        # Routine confirmations (Luma-style): no notify flag, normal priority.
        er.queue_draft_notification(s, m, {'status': 'important_information', 'notify': False,
                                           'priority': 'normal', 'case_key': 'r1'})
        er.queue_draft_notification(s, m, {'status': 'created', 'notify': False,
                                           'priority': 'normal', 'case_key': 'r2'})
        er.queue_draft_notification(s, m, {'status': 'workflow_blocked', 'notify': False,
                                           'priority': 'normal', 'case_key': 'r3'})
        er.queue_draft_notification(s, m, {'status': 'no_draft_needed', 'notify': True, 'case_key': 'r4'})
        er.queue_draft_notification(s, m, {'status': 'cc_information', 'notify': True, 'case_key': 'r5'})
        er.queue_draft_notification(s, m, {'status': 'not_worthy_or_already_handled', 'notify': True, 'case_key': 'r6'})
        self.assertEqual(notifications(er, s), [])

    def test_duplicate_case_notified_once(self):
        er = self.er
        s = self.fresh()
        m = message('Maria Beispiel')
        outcome = {'status': 'needs_manual_review_draft_state', 'notify': True, 'case_key': 'dup'}
        er.queue_draft_notification(s, m, outcome)
        er.queue_draft_notification(s, m, outcome)
        self.assertEqual(len(notifications(er, s)), 1)

    def test_style_invariants_no_addresses_emojis_or_identifiers(self):
        er = self.er
        s = self.fresh()
        m = {'account': 'code', 'id': 'synthetic-2', 'threadId': 't-2',
             'from': '"Beispiel, Max" <max.beispiel@example.org>'}
        dirty = ('Schreibt max.beispiel@example.org oder siehe https://example.org/x '
                 '🔥 *wichtig* ' + 'sehr lang ' * 40 + 'Ende')
        er.queue_draft_notification(s, m, {'status': 'important_information', 'notify': True,
                                           'case_key': 'style-1', 'notification_summary': dirty})
        er.queue_draft_notification(s, m, {'status': 'needs_user_input', 'notify': True,
                                           'case_key': 'style-2', 'question': dirty})
        for text in notifications(er, s):
            self.assertNotIn('@', text)
            self.assertNotIn('http', text)
            self.assertNotRegex(text, r'[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f]')
            self.assertNotRegex(text, r'[`*_<>]')
            self.assertNotIn('synthetic-2', text)
            self.assertNotIn('style-1', text)
            # Comma-form display name is flipped to natural order.
            self.assertIn('Max Beispiel', text)
            # One to two natural sentences, bounded length.
            self.assertLessEqual(len(re.findall(r'[.!?](?:\s|$)', text)), 2)
            self.assertLessEqual(len(text), 260)

    def test_unknown_sender_falls_back_to_neutral_party(self):
        er = self.er
        s = self.fresh()
        m = {'account': 'code', 'id': 'synthetic-3', 'threadId': 't-3', 'from': 'noreply@example.org'}
        er.queue_draft_notification(s, m, {'status': 'needs_manual_review_draft_state',
                                           'notify': True, 'case_key': 'fallback'})
        self.assertIn('Ein Kontakt', notifications(er, s)[0])


if __name__ == '__main__':
    unittest.main()
