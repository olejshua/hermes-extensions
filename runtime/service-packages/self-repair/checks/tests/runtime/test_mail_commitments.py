"""Mail-sourced commitments: owner's own sent mail only, exact quotes, no invented model step."""
import sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'gtm'))
from gtm.commitments import CommitmentStore  # noqa: E402
from gtm.mail_commitments import MailCommitment, MailSource, record_mail_commitments  # noqa: E402

OWNER = 'owner@example.invalid'
BODY = 'Hallo Anna,\nich schicke dir das Angebot bis 20.11.2026.\nIch melde mich am Montag.\nGruß'


def source(**kw):
    base = dict(message_id='m1', thread_id='t1', from_header=OWNER, labels=('SENT',), mailbox_account=OWNER, sent_at='2026-11-13T09:00:00+01:00',
                body=BODY, thread_complete=True)
    base.update(kw)
    return MailSource(**base)


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = CommitmentStore(Path(self.tmp.name) / 'c.db')

    def q(self, sql):
        with self.store.connect() as db:
            return [tuple(r) for r in db.execute(sql).fetchall()]

    def rec(self, src, items):
        return record_mail_commitments(self.store, OWNER, src, items)

    def test_dated_commitment_is_date_only(self):
        refs = self.rec(source(), [MailCommitment('offer', 'Angebot an Anna schicken', 'ich schicke dir das Angebot bis 20.11.2026', '2026-11-20', '20.11.2026')])
        self.assertEqual(len(refs), 1)
        row = self.q('select due_precision, due from commitment')[0]
        self.assertEqual(row[0], 'date')

    def test_undated_commitment_tracked_without_reminder_date(self):
        refs = self.rec(source(), [MailCommitment('call', 'Anna melden', 'Ich melde mich am Montag')])
        self.assertEqual(len(refs), 1)
        row = self.q('select due from commitment')[0]
        self.assertIsNone(row[0])

    def test_relative_day_never_becomes_a_date(self):
        with self.assertRaises(ValueError):
            self.rec(source(), [MailCommitment('call', 'x', 'Ich melde mich am Montag', '2026-11-16', 'Montag')])

    def test_foreign_sender_is_refused(self):
        with self.assertRaises(ValueError):
            self.rec(source(from_header='anna@example.invalid'), [MailCommitment('a', 'x', 'Ich melde mich am Montag')])

    def test_valid_display_name_owner_mail_is_accepted(self):
        for header in (f'Ole Beispiel <{OWNER}>', f'"Beispiel, Ole" <{OWNER.upper()}>'):
            self.assertEqual(len(self.rec(source(message_id=header[:5] + str(len(header))), [MailCommitment('a', 'x', 'Ich melde mich am Montag')])), 1)

    def test_ambiguous_or_spoofed_header_is_refused(self):
        for header in (f'Owner <x@example.invalid> {OWNER}', f'{OWNER}, anna@example.invalid',
                       f'"{OWNER}" <anna@example.invalid>', f'Ole <{OWNER}>\nBcc: a@b.invalid'):
            with self.assertRaises(ValueError, msg=header):
                self.rec(source(from_header=header), [MailCommitment('a', 'x', 'Ich melde mich am Montag')])

    def test_header_alone_is_not_provenance(self):
        item = [MailCommitment('a', 'x', 'Ich melde mich am Montag')]
        for kw in (dict(labels=()), dict(labels=('INBOX',)), dict(labels=('SENT', 'DRAFT')),
                   dict(mailbox_account='other@example.invalid'), dict(mailbox_account='')):
            with self.assertRaises(ValueError, msg=str(kw)):
                self.rec(source(**kw), item)

    def test_invented_quote_refused(self):
        with self.assertRaises(ValueError):
            self.rec(source(), [MailCommitment('a', 'x', 'ich zahle morgen')])

    def test_incomplete_thread_refused(self):
        with self.assertRaises(ValueError):
            self.rec(source(thread_complete=False), [MailCommitment('a', 'x', 'Ich melde mich am Montag')])

    def test_idempotent_by_message_and_key(self):
        item = [MailCommitment('a', 'x', 'Ich melde mich am Montag')]
        self.rec(source(), item); self.rec(source(), item)
        n = self.q('select count(*) from commitment')[0][0]
        self.assertEqual(n, 1)

    def test_duplicate_keys_and_empty_refused(self):
        item = MailCommitment('a', 'x', 'Ich melde mich am Montag')
        with self.assertRaises(ValueError):
            self.rec(source(), [item, item])
        with self.assertRaises(ValueError):
            self.rec(source(), [])

if __name__ == '__main__':
    unittest.main()
