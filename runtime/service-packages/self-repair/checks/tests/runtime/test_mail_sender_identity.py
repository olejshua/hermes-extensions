"""The sender identity in draft signatures must agree across every producer.

The published source carries sanitized placeholder names; the private host
profile supplies real ones (deploy/source_release.py treats string literals as
profile-only differences). These tests derive the expected names from the
workflow prompt, not from what the formatter emits, so a drifted or
wrong-person signature in any one producer fails here. They cannot tell whether
the profile's real name is right; that stays a live-host check.
"""
import re
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[5] / 'hermes-home/scripts'
sys.path.insert(0, str(SCRIPTS))
import mail_policy  # noqa: E402
import mail_workflow  # noqa: E402

try:
    from types import ModuleType
    from unittest.mock import patch
    # email_watcher pulls Google client libraries; reply_signature does not use it.
    import importlib.util
    import tempfile
    stub = ModuleType('email_watcher')
    stub.HOME = Path(tempfile.gettempdir())
    with patch.dict(sys.modules, {'email_watcher': stub}):
        spec = importlib.util.spec_from_file_location('identity_email_realtime', SCRIPTS / 'email_realtime.py')
        email_realtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(email_realtime)
except Exception as error:  # pragma: no cover - environment specific
    email_realtime = None
    IMPORT_ERROR = error

PROMPT = next(v for v in vars(mail_workflow).values() if isinstance(v, str) and 'beim Duzen' in v)
MATCH = re.search(r'(\w+) beim Duzen, sonst ([\w ]+?)\.', PROMPT)
SHORT, FULL = MATCH.group(1), MATCH.group(2)
INFORMAL = [{'text': 'Hallo %s, passt dir Montag? Danke dir.' % SHORT}]
FORMAL = [{'text': 'Sehr geehrte Damen und Herren, wir bitten Sie um Rückmeldung.'}]


def closing(text):
    return text.rstrip().split('\n')[-1]


class Identity(unittest.TestCase):
    def test_prompt_declares_one_short_and_one_full_name(self):
        self.assertTrue(FULL.startswith(SHORT) and FULL != SHORT, (SHORT, FULL))

    def test_policy_format_body_signs_with_prompt_names(self):
        self.assertEqual(closing(mail_policy.format_body('Hallo, du kannst kommen.')), SHORT)
        self.assertEqual(closing(mail_policy.format_body('Sehr geehrte Frau X, Sie können kommen.')), FULL)

    @unittest.skipIf(email_realtime is None, 'email_realtime not importable here')
    def test_reply_signature_signs_with_prompt_names(self):
        self.assertEqual(closing(email_realtime.reply_signature(INFORMAL, 'Gerne.')), SHORT)
        self.assertEqual(closing(email_realtime.reply_signature(FORMAL, 'Gerne.')), FULL)

    @unittest.skipIf(email_realtime is None, 'email_realtime not importable here')
    def test_both_producers_agree_on_the_same_thread(self):
        for thread, body in ((INFORMAL, 'Gerne, du kannst kommen.'), (FORMAL, 'Gerne, Sie können kommen.')):
            self.assertEqual(closing(email_realtime.reply_signature(thread, body)),
                             closing(mail_policy.format_body(body)), thread)

    def test_foreign_closing_name_is_never_taken_as_ours(self):
        body = mail_policy.format_body('Gerne.\n\nBeste Grüße\nPeter Fremd')
        self.assertEqual(closing(body), FULL)
        self.assertEqual(body.count('Beste Grüße'), 2, 'foreign closing must stay visibly separate, not be overwritten')

    def test_own_closing_is_kept_once_not_doubled(self):
        for variant in (SHORT, FULL):
            body = mail_policy.format_body('Gerne, du kannst kommen.\n\nBeste Grüße\n' + variant)
            self.assertEqual(body.count('Beste Grüße'), 1, variant)
            self.assertEqual(closing(body), variant, 'an already-correct own closing is preserved as written')


if __name__ == '__main__':
    unittest.main()
