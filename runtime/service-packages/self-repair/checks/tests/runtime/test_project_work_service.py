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


class Service(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.src = self.base / 'notes.md'
        self.src.write_text('Projektstand: Beispiel')
        self.make()

    def policy(self, **extra):
        sha = hashlib.sha256(self.src.read_bytes()).hexdigest()
        return dict(owner=OWNER, session_id=SESSION, workspace_root=str(self.base / 'ws'), max_pending=2,
                    sources={'acribic': [dict(path=str(self.src), title='Notizen', sha256=sha, verified_at=time.time())]}, **extra)

    def make(self, **extra):
        self.svc = ProjectService(self.policy(**extra))

    def submit(self, rid='r1', goal='Pruefe den Stand', **kw):
        return self.svc.submit('acribic', goal, rid, kw.get('owner', OWNER), kw.get('session', SESSION))

    def test_submit_is_idempotent_and_conflicting_reuse_is_rejected(self):
        a = self.submit()
        self.assertTrue(a['ok'] and a['state'] == 'queued')
        self.assertEqual(self.submit()['job_id'], a['job_id'])
        self.assertEqual(self.submit(goal='anderes Ziel'), {'ok': False, 'error': 'request_id_conflict'})
        self.assertEqual(len(self.svc.list_owned(OWNER, SESSION)['jobs']), 1)

    def test_foreign_owner_or_session_cannot_see_or_command_a_job(self):
        job = self.submit()['job_id']
        self.assertEqual(self.svc.status(job, 'other', SESSION)['error'], 'owner_session_mismatch')
        self.assertEqual(self.svc.status(job, OWNER, 'other')['error'], 'owner_session_mismatch')
        self.assertEqual(self.svc.interrupt(job, 'i1', 'other', SESSION)['error'], 'owner_session_mismatch')
        self.assertEqual(self.svc.status('0' * 32, OWNER, SESSION)['error'], 'not_found')

    def test_unknown_project_and_missing_sources_fail_closed(self):
        self.assertEqual(self.svc.submit('other', 'x', 'r', OWNER, SESSION)['error'], 'project_not_allowed')
        self.assertEqual(self.svc.submit('monolith', 'x', 'r', OWNER, SESSION)['error'], 'monolith_project_context_required')

    def test_changed_or_stale_source_blocks_the_start(self):
        self.src.write_text('verändert nach der Prüfung')
        self.assertEqual(self.submit('r2')['error'], 'source_changed_refresh_required')
        self.src.write_text('Projektstand: Beispiel')
        stale = self.policy()
        stale['sources']['acribic'][0]['verified_at'] = time.time() - 10 * 86400
        self.assertEqual(ProjectService(stale).submit('acribic', 'x', 'r3', OWNER, SESSION)['error'], 'source_stale')

    def test_sensitive_source_names_are_refused(self):
        secret = self.base / 'token.txt'
        secret.write_text('x')
        policy = self.policy()
        policy['sources']['acribic'][0].update(path=str(secret), sha256=hashlib.sha256(b'x').hexdigest())
        self.assertEqual(ProjectService(policy).submit('acribic', 'x', 'r', OWNER, SESSION)['error'], 'sensitive_source_forbidden')

    def test_queue_limit(self):
        self.submit('a')
        self.submit('b', goal='zwei')
        self.assertEqual(self.submit('c', goal='drei')['error'], 'queue_full')

    def test_steer_needs_current_revision_and_bumps_it_once(self):
        job = self.submit()['job_id']
        stale = self.svc.steer(job, 'Konzentriere dich auf Preise', 5, 's1', OWNER, SESSION)
        self.assertEqual(stale['error'], 'revision_conflict')
        ok = self.svc.steer(job, 'Konzentriere dich auf Preise', 0, 's2', OWNER, SESSION)
        self.assertEqual((ok['ok'], ok['revision'], ok['command_state']), (True, 1, 'pending'))
        again = self.svc.steer(job, 'Konzentriere dich auf Preise', 0, 's2', OWNER, SESSION)
        self.assertEqual(again['revision'], 1, 'same request id must not bump the revision twice')
        pending = self.svc.steer(job, 'Noch etwas', 1, 's3', OWNER, SESSION)
        self.assertEqual(pending['error'], 'control_pending')

    def test_interrupt_and_resume_state_rules(self):
        job = self.submit()['job_id']
        self.assertEqual(self.svc.resume(job, 'u1', OWNER, SESSION)['error'], 'not_resumable')
        self.assertEqual(self.svc.interrupt(job, 'i1', OWNER, SESSION)['command_state'], 'pending')
        self.assertEqual(self.svc.interrupt(job, 'i2', OWNER, SESSION)['error'], 'control_pending')

    def test_result_is_not_available_before_completion(self):
        job = self.submit()['job_id']
        self.assertEqual(self.svc.result(job, OWNER, SESSION)['error'], 'result_not_ready')
        self.assertEqual(self.svc.artifact_read('nope', OWNER, SESSION)['error'], 'artifact_not_found')

    def test_policy_requires_bounds_and_trusted_identity(self):
        bad = self.policy()
        bad['max_turns'] = 99
        with self.assertRaises(Exception):
            ProjectService(bad)
        missing = self.policy()
        del missing['owner']
        with self.assertRaises(Exception):
            ProjectService(missing)


if __name__ == '__main__':
    unittest.main()
