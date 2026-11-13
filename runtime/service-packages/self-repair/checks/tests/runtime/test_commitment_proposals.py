import json, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'gtm'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gtm.commitment_proposals import ProposalStore, confirm, flags_for, propose_from_thread, thread_hash  # noqa: E402
from gtm.commitments import CommitmentStore  # noqa: E402
from gtm.gmail_mail_source import thread_sources  # noqa: E402
from gtm.mail_commitments import MailCommitment, MailSource  # noqa: E402
from test_gmail_mail_source import Svc, msg, OWNER  # noqa: E402


def S(body):
    return MailSource('m', 't1', OWNER, '2026-11-13T09:00:00+00:00', body, True, ('SENT',), OWNER)


def item(q):
    return MailCommitment('a', 'x', q)


class Flags(unittest.TestCase):
    def test_plain_promise_has_no_flags(self):
        self.assertEqual(flags_for(S('ich schicke dir das Angebot'), item('ich schicke dir das Angebot'), []), [])

    def test_quoted_request_of_someone_else(self):
        body = 'Ok.\n\nAm 12.11.2026 schrieb Anna:\n> Schick mir bitte das Angebot'
        self.assertIn('quoted_text', flags_for(S(body), item('Schick mir bitte das Angebot'), []))

    def test_conditional_and_withdrawal_and_later_message(self):
        self.assertIn('conditional', flags_for(S('Falls du willst, schicke ich das Angebot'), item('schicke ich das Angebot'), []))
        self.assertIn('withdrawal_in_quote', flags_for(S('das Angebot schicke ich doch nicht'), item('das Angebot schicke ich doch nicht'), []))
        later = [S('Update: das Angebot kann ich nicht mehr bis Montag liefern.')]
        self.assertIn('later_own_message_may_withdraw', flags_for(S('ich schicke das Angebot'), item('ich schicke das Angebot'), later))

    def test_withdrawal_only_inside_quoted_history_of_later_message_is_ignored(self):
        later = [S('Danke.\n> ich kann das nicht mehr')]
        self.assertEqual(flags_for(S('ich schicke das Angebot'), item('ich schicke das Angebot'), later), [])


def model(payload):
    calls = []
    def complete(prompt): calls.append(prompt); return json.dumps(payload)
    complete.calls = calls
    return complete


ITEMS = {'items': [{'key': 'a', 'title': 'Angebot schicken', 'quote': 'ich schicke das Angebot', 'due_date': None, 'deadline_basis': None}]}


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.props = ProposalStore(Path(self.tmp.name) / 'p.db')
        self.commit = CommitmentStore(Path(self.tmp.name) / 'c.db')

    def run_flow(self, messages, mode='on', c=None, **kw):
        c = c or model(ITEMS)
        return c, propose_from_thread(Svc(OWNER, messages), OWNER, 't1', self.props, complete=c, mode=mode,
                                      transmission_approved=True, scope='o|p|c', **kw)

    def count(self):
        with self.commit.connect() as db:
            return db.execute('select count(*) from commitment').fetchone()[0]

    def test_proposals_never_enter_reminder_store_on_their_own(self):
        _, rep = self.run_flow([msg('m1', ['SENT'], body='ich schicke das Angebot')])
        self.assertEqual((rep['proposed'], len(self.props.pending()), self.count()), (1, 1, 0))

    def test_off_and_dry_run_store_nothing(self):
        c, rep = self.run_flow([msg('m1', ['SENT'], body='ich schicke das Angebot')], mode='off')
        self.assertEqual((rep['status'], c.calls, self.props.pending()), ('disabled', [], []))
        _, rep = self.run_flow([msg('m1', ['SENT'], body='ich schicke das Angebot')], mode='dry_run')
        self.assertEqual((rep['proposed'], self.props.pending()), (1, []))

    def test_incomplete_thread_stops_before_model(self):
        c, rep = self.run_flow([msg('m1', ['SENT'], body='ich schicke das Angebot'), msg('m2', ['INBOX'], stamp='x')])
        self.assertEqual((rep['status'], c.calls, self.props.pending()), ('incomplete_thread', [], []))

    def test_confirm_needs_unchanged_thread_and_ack_for_flags(self):
        base = [msg('m1', ['SENT'], body='ich schicke das Angebot')]
        _, rep = self.run_flow(base)
        ref = self.props.pending()[0]['ref']
        grown = thread_hash(thread_sources(Svc(OWNER, base + [msg('m2', ['SENT'], body='ich schicke es doch nicht', stamp='1794560001000')]), OWNER, 't1'))
        with self.assertRaises(ValueError):
            confirm(self.props, self.commit, ref, current_thread_hash=grown)
        confirm(self.props, self.commit, ref, current_thread_hash=rep['thread_hash'])
        self.assertEqual((self.count(), self.props.pending()), (1, []))

    def test_flagged_needs_acknowledgement(self):
        msgs = [msg('m1', ['SENT'], body='ich schicke das Angebot'), msg('m2', ['SENT'], body='Das kann ich nicht mehr liefern.', stamp='1794560001000')]
        _, rep = self.run_flow(msgs)
        row = self.props.pending()[0]
        self.assertIn('later_own_message_may_withdraw', row['flags'])
        with self.assertRaises(ValueError):
            confirm(self.props, self.commit, row['ref'], current_thread_hash=rep['thread_hash'])
        confirm(self.props, self.commit, row['ref'], current_thread_hash=rep['thread_hash'], acknowledge_flags=True)
        self.assertEqual(self.count(), 1)

if __name__ == '__main__':
    unittest.main()
