import json, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'gtm')); sys.path.insert(0, str(Path(__file__).resolve().parent))
from gtm.commitment_proposals import ProposalStore  # noqa: E402
from gtm.commitments import CommitmentStore  # noqa: E402
from gtm.gmail_mail_source import recent_sent_thread_ids  # noqa: E402
from gtm.prep_adapters import CODE, ReviewRequired  # noqa: E402
from gtm.prep_daemon_entry import proposal_section  # noqa: E402
from gtm.synthesis import ModelLedger  # noqa: E402
from test_gmail_mail_source import msg  # noqa: E402

ITEMS = {'items': [{'key': 'a', 'title': 'Angebot schicken', 'quote': 'ich schicke das Angebot', 'due_date': None, 'deadline_basis': None}]}


class Svc:
    def __init__(self, listing):
        self.listing = listing; self.calls = []
    def users(self): return self
    def getProfile(self, userId): return type('R', (), {'execute': lambda s: {'emailAddress': CODE}})()
    def messages(self): return self
    def list(self, **kw):
        self.calls.append(kw); return type('R', (), {'execute': lambda s: self.listing})()
    def threads(self): return self
    def get(self, userId, id, format):
        m = msg('m1', ['SENT'], body='ich schicke das Angebot', frm=CODE)
        return type('R', (), {'execute': lambda s: {'id': 't1', 'messages': [m]}})()


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.props, self.commit, self.led = ProposalStore(d / 'p.db'), CommitmentStore(d / 'c.db'), ModelLedger(d / 'l.db')
        self.cfg = {'owner': 'o', 'synthesis': {'mode': 'on', 'model': 'm1', 'transmission_approved': True}}

    def test_candidates_are_bounded_unique_and_read_only(self):
        svc = Svc({'messages': [{'threadId': 't1'}, {'threadId': 't1'}, {'threadId': 't2'}]})
        self.assertEqual(recent_sent_thread_ids(svc, CODE, days=2, limit=10), ['t1', 't2'])
        self.assertEqual(svc.calls[0]['labelIds'], ['SENT'])
        with self.assertRaises(ValueError):
            recent_sent_thread_ids(svc, CODE, days=0)

    def test_render_only_without_synthesis_or_factories(self):
        text, rep = proposal_section({}, ('mail_commitments',), self.props, self.commit, activate=False)
        self.assertEqual((text, rep['status'], rep['reconciled']), ('', 'render_only', None))

    def test_dry_run_collects_nothing_stored_and_on_needs_activation(self):
        svc = Svc({'messages': [{'threadId': 't1'}]})
        kw = dict(service_factory=lambda: svc, complete_factory=lambda p: (lambda prompt: json.dumps(ITEMS)), ledger=self.led)
        self.cfg['synthesis']['mode'] = 'dry_run'
        text, rep = proposal_section(self.cfg, ('mail_commitments',), self.props, self.commit, activate=False, **kw)
        self.assertEqual((text, rep['proposed']), ('', 1))
        self.cfg['synthesis']['mode'] = 'on'
        with self.assertRaises(ReviewRequired):
            proposal_section(self.cfg, ('mail_commitments',), self.props, self.commit, activate=False, **kw)

    def test_activated_on_stores_review_only_proposals_and_shows_them(self):
        svc = Svc({'messages': [{'threadId': 't1'}]})
        kw = dict(service_factory=lambda: svc, complete_factory=lambda p: (lambda prompt: json.dumps(ITEMS)), ledger=self.led)
        text, rep = proposal_section(self.cfg, ('mail_commitments',), self.props, self.commit, activate=True, **kw)
        self.assertIn('noch nichts übernommen', text); self.assertEqual(rep['reconciled'], {'confirmed': 0, 'unknown': 0})
        with self.commit.connect() as db:
            self.assertEqual(db.execute('select count(*) from commitment').fetchone()[0], 0)

    def test_source_not_allowed_or_unapproved_reads_nothing(self):
        svc = Svc({'messages': []})
        kw = dict(service_factory=lambda: svc, complete_factory=lambda p: (lambda prompt: '{}'), ledger=self.led)
        proposal_section(self.cfg, ('mail',), self.props, self.commit, activate=True, **kw)
        self.assertEqual(svc.calls, [])
        self.cfg['synthesis']['transmission_approved'] = False
        with self.assertRaises(ReviewRequired):
            proposal_section(self.cfg, ('mail_commitments',), self.props, self.commit, activate=True, **kw)
        self.assertEqual(svc.calls, [])

if __name__ == '__main__':
    unittest.main()


class Factory(unittest.TestCase):
    def test_identity_checked_service_and_nothing_read_at_build(self):
        import types
        from gtm.prep_adapters import code_gmail_service
        svc = Svc({'messages': []})
        reads = types.SimpleNamespace(mail_module=types.SimpleNamespace(service=lambda alias: svc if alias == 'code' else None))
        self.assertIs(code_gmail_service(reads), svc); self.assertEqual(svc.calls, [])
        other = types.SimpleNamespace(mail_module=types.SimpleNamespace(service=lambda a: svc, verified_profile=lambda s: 'x@example.invalid'))
        with self.assertRaises(ReviewRequired):
            code_gmail_service(other)

    def test_default_main_wiring_without_permission_reads_and_calls_nothing(self):
        import types, tempfile
        from gtm.codex_completion import mail_factory
        built = []
        d = Path(tempfile.mkdtemp())
        props, commit = ProposalStore(d / 'p.db'), CommitmentStore(d / 'c.db')
        text, rep = proposal_section({}, ('mail_commitments',), props, commit, activate=False,
                                     service_factory=lambda: built.append('svc'), complete_factory=mail_factory,
                                     ledger=ModelLedger(d / 'l.db'))
        self.assertEqual((built, rep['status'], text), ([], 'render_only', ''))
