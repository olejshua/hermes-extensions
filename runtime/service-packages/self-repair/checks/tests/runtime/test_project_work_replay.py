"""ProjectService job control surface: owner scope, idempotent requests, steering and queue limits."""
import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5] / 'hermes-home/ole-project-work/package'))
from ole_project_work.service import ProjectService  # noqa: E402

OWNER, SESSION = 'owner-1', 'session-1'



from ole_project_work.worker import Supervisor  # noqa: E402
from ole_project_work.transport import Rejected, Unknown  # noqa: E402


class FakeTransport:
    def __init__(self, behaviors=None, turns=None):
        self.calls, self.behaviors, self.turns = [], list(behaviors or []), turns or []

    def rpc(self, method, params, timeout=20):
        self.calls.append((method, params))
        behavior = self.behaviors.pop(0) if self.behaviors else None
        if isinstance(behavior, Exception):
            raise behavior
        if method == 'turn/start':
            return {'turn': {'id': 'turn-1'}}
        return behavior or {}

    def history(self, thread_id):
        return {'turns': self.turns}


class Replay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        src = base / 'notes.md'
        src.write_text('Stand')
        policy = dict(owner=OWNER, session_id=SESSION, workspace_root=str(base / 'ws'),
                      sources={'acribic': [dict(path=str(src), title='N', sha256=hashlib.sha256(b'Stand').hexdigest(), verified_at=time.time())]})
        self.svc = ProjectService(policy)
        self.sup = Supervisor(self.svc)
        self.job = self.svc.submit('acribic', 'Ziel', 'r1', OWNER, SESSION)['job_id']
        self.sup.update(self.job, thread_id='thread-1')

    def test_acked_turn_start_marks_job_working_and_counts_the_turn(self):
        t = FakeTransport()
        self.sup.start_turn(t, self.job)
        j = self.sup.load(self.job)
        self.assertEqual((j['state'], j['turn_id'], j['turns_used'], j['inflight']), ('working', 'turn-1', 1, None))
        self.assertEqual([c[0] for c in t.calls], ['turn/start'])

    def test_unknown_outcome_is_never_replayed_and_reconcile_adopts_the_proven_turn(self):
        t = FakeTransport([Unknown('timeout')])
        with self.assertRaises(Unknown):
            self.sup.start_turn(t, self.job)
        j = self.sup.load(self.job)
        marker = j['inflight']['marker']
        self.assertEqual((j['turns_used'], j['turn_id']), (0, None))
        t.turns = [{'id': 'turn-9', 'status': 'inProgress', 'items': [
            {'type': 'userMessage', 'clientId': marker, 'content': []}]}]
        self.sup.reconcile(t, self.job)
        j = self.sup.load(self.job)
        self.assertEqual((j['turn_id'], j['turns_used'], j['inflight']), ('turn-9', 1, None))
        self.assertEqual(len([c for c in t.calls if c[0] == 'turn/start']), 1, 'the unknown start must not be sent again')

    def test_unproven_operation_stays_blocked_not_replayed(self):
        t = FakeTransport([Unknown('timeout')])
        with self.assertRaises(Unknown):
            self.sup.start_turn(t, self.job)
        t.turns = []
        with self.assertRaises(Unknown):
            self.sup.reconcile(t, self.job)
        self.assertIsNotNone(self.sup.load(self.job)['inflight'])
        self.assertEqual(len(t.calls), 1)

    def test_rejected_request_clears_inflight_and_rejects_the_command(self):
        self.svc.steer(self.job, 'Korrektur', 0, 's1', OWNER, SESSION)
        cid, kind, *_ = self.sup.command_row(self.job)
        t = FakeTransport([Rejected('no')])
        with self.assertRaises(Rejected):
            self.sup.call(t, self.job, 'turn/steer', {'expectedTurnId': 'x', 'clientUserMessageId': 'm'}, cid)
        self.assertIsNone(self.sup.load(self.job)['inflight'])
        out = self.svc.steer(self.job, 'Korrektur', 0, 's1', OWNER, SESSION)
        self.assertEqual((out['ok'], out['command_state']), (False, 'rejected'))

    def test_time_budget_stops_the_job_before_more_model_work(self):
        self.svc.policy['max_run_seconds'] = 1
        self.sup.update(self.job, accounted_at=time.time() - 50, spent_seconds=0)
        from ole_project_work.safety import Fault
        with self.assertRaises(Fault) as ctx:
            self.sup.account(self.job)
        self.assertEqual(ctx.exception.code, 'model_time_budget_exhausted')

    def test_turn_budget_is_enforced_before_a_new_turn(self):
        from ole_project_work.safety import Fault
        self.svc.policy['max_turns'] = 1
        self.sup.update(self.job, turns_used=1)
        t = FakeTransport()
        with self.assertRaises(Fault) as ctx:
            self.sup.start_turn(t, self.job)
        self.assertEqual((ctx.exception.code, t.calls), ('turn_budget_exhausted', []))



if __name__ == '__main__':
    unittest.main()
