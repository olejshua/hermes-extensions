import json, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'gtm'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gtm.commitment_proposals import ConsumedMessages, confirm_from_owner_event, ProposalStore, confirm_from_owner_message, confirmation_phrase, flags_for, propose_from_thread  # noqa: E402
from gtm.commitments import CommitmentStore  # noqa: E402
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

    HANDLE = '+490000000000'

    def confirm(self, msgs, row, body=None, sender=None, ref=None):
        rec = {'from': sender or self.HANDLE, 'body': body if body is not None else confirmation_phrase(row), 'kind': 'message'}
        return confirm_from_owner_message(self.props, self.commit, Svc(OWNER, msgs), OWNER, ref=ref or row['ref'],
                                          message_id='w1', lookup=lambda mid: rec, owner_handle=self.HANDLE)

    def test_confirmation_must_come_from_owner_with_the_bound_phrase(self):
        base = [msg('m1', ['SENT'], body='ich schicke das Angebot')]
        self.run_flow(base); row = self.props.pending()[0]
        for kw in (dict(sender='+491111111111'), dict(body='Bestätige alles'), dict(body=confirmation_phrase(row) + ' trotz Hinweisen')):
            with self.assertRaises(ValueError, msg=str(kw)):
                self.confirm(base, row, **kw)
        self.assertEqual((self.count(), len(self.props.pending())), (0, 1))
        self.confirm(base, row)
        self.assertEqual((self.count(), self.props.pending()), (1, []))

    def test_phrase_is_bound_to_version_so_an_old_confirmation_fails_after_reproposal_change(self):
        base = [msg('m1', ['SENT'], body='ich schicke das Angebot')]
        self.run_flow(base); row = self.props.pending()[0]
        other = dict(row, title='Anderes')
        with self.assertRaises(ValueError):
            self.confirm(base, row, body=confirmation_phrase(other))

    def test_newer_inbound_reply_makes_confirmation_stale(self):
        base = [msg('m1', ['SENT'], body='ich schicke das Angebot')]
        self.run_flow(base); row = self.props.pending()[0]
        grown = base + [msg('m2', ['INBOX'], body='Bitte doch nicht mehr', frm='anna@example.invalid', stamp='1794560001000')]
        with self.assertRaises(ValueError) as ctx:
            self.confirm(grown, row)
        self.assertIn('thread_changed', str(ctx.exception)); self.assertEqual(self.count(), 0)

    def test_label_change_or_unreadable_thread_stays_review(self):
        base = [msg('m1', ['SENT'], body='ich schicke das Angebot')]
        self.run_flow(base); row = self.props.pending()[0]
        with self.assertRaises(ValueError):
            self.confirm([msg('m1', ['SENT', 'TRASH'], body='ich schicke das Angebot')], row)
        with self.assertRaises(ValueError):
            self.confirm([msg('m1', ['SENT'], body='ich schicke das Angebot', stamp='x')], row)
        self.assertEqual(self.count(), 0)

    def test_flagged_needs_explicit_phrase_tail(self):
        msgs = [msg('m1', ['SENT'], body='ich schicke das Angebot'), msg('m2', ['SENT'], body='Das kann ich nicht mehr liefern.', stamp='1794560001000')]
        self.run_flow(msgs)
        row = self.props.pending()[0]
        self.assertIn('later_own_message_may_withdraw', row['flags'])
        self.assertTrue(confirmation_phrase(row).endswith('trotz Hinweisen'))
        with self.assertRaises(ValueError):
            self.confirm(msgs, row, body=confirmation_phrase(dict(row, flags='')))
        self.confirm(msgs, row)
        self.assertEqual(self.count(), 1)

    def native(self, **ov):
        import types
        src = types.SimpleNamespace(platform=types.SimpleNamespace(value='telegram'), user_id='42', chat_id='42', chat_type='private',
                                    profile=None, is_bot=False, profile_route_rejected=False)
        pol = types.SimpleNamespace(owner='telegram:42', bound=lambda s: (getattr(s, 'user_id', '') == '42' and s.chat_type == 'private'
                                                                          and not s.is_bot))
        gw = types.SimpleNamespace(_is_user_authorized_for_source=lambda s: ov.pop('authorized', True))
        ev = dict(source=src, internal=False, message_id='tg-1', text=None); ev.update(ov)
        return pol, gw, types.SimpleNamespace(**ev)

    def event_confirm(self, msgs, row, consumed, **ov):
        pol, gw, ev = self.native(text=confirmation_phrase(row), **ov)
        return confirm_from_owner_event(self.props, self.commit, Svc(OWNER, msgs), OWNER, ref=row['ref'], event=ev, gateway=gw,
                                        policy=pol, consumed=consumed)

    def test_native_event_provenance_is_required(self):
        base = [msg('m1', ['SENT'], body='ich schicke das Angebot')]
        self.run_flow(base); row = self.props.pending()[0]
        consumed = ConsumedMessages(Path(self.tmp.name) / 'u.db')
        import types
        for ov in (dict(internal=True), dict(authorized=False), dict(edited=True), dict(message_id='')):
            with self.assertRaises(ValueError, msg=str(ov)):
                self.event_confirm(base, row, consumed, **ov)
        pol, gw, ev = self.native(text=confirmation_phrase(row))
        ev.source.is_bot = True
        with self.assertRaises(ValueError):
            confirm_from_owner_event(self.props, self.commit, Svc(OWNER, base), OWNER, ref=row['ref'], event=ev, gateway=gw, policy=pol, consumed=consumed)
        ev.source.is_bot = False; ev.source.chat_type = 'group'
        with self.assertRaises(ValueError):
            confirm_from_owner_event(self.props, self.commit, Svc(OWNER, base), OWNER, ref=row['ref'], event=ev, gateway=gw, policy=pol, consumed=consumed)
        self.assertEqual(self.count(), 0)
        self.event_confirm(base, row, consumed)
        self.assertEqual(self.count(), 1)

    def test_replay_and_concurrent_confirmation_commit_once(self):
        import threading
        base = [msg('m1', ['SENT'], body='ich schicke das Angebot')]
        self.run_flow(base); row = self.props.pending()[0]
        consumed = ConsumedMessages(Path(self.tmp.name) / 'u.db')
        results = []
        def go():
            try:
                self.event_confirm(base, row, consumed); results.append('ok')
            except ValueError as exc:
                results.append(str(exc))
        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(results.count('ok'), 1); self.assertEqual(self.count(), 1)

    def test_message_used_for_a_stale_attempt_cannot_be_replayed(self):
        base = [msg('m1', ['SENT'], body='ich schicke das Angebot')]
        self.run_flow(base); row = self.props.pending()[0]
        consumed = ConsumedMessages(Path(self.tmp.name) / 'u.db')
        grown = base + [msg('m2', ['INBOX'], body='nein', frm='anna@example.invalid', stamp='1794560001000')]
        with self.assertRaises(ValueError):
            self.event_confirm(grown, row, consumed)
        with self.assertRaises(ValueError) as ctx:
            self.event_confirm(base, row, consumed)
        self.assertIn('already_used', str(ctx.exception)); self.assertEqual(self.count(), 0)

if __name__ == '__main__':
    unittest.main()
