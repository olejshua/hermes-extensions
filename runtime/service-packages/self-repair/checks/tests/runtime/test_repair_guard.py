import json
import sqlite3
import sys
import tempfile
import unittest
import subprocess
from unittest.mock import patch
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'second-brain/src'))
from self_repair.store import Incidents,private_json
from self_repair.guard import MODEL_TIME_SECONDS,COOLDOWN_SECONDS,WINDOW_SECONDS
from self_repair.service import Engine,retry_evidence,semantic_fingerprint
from self_repair.bundle import original_probe
from self_repair.policy import PolicyError
from self_repair.sources import TARGETS

class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Incidents(self.root)
    def incident(self,key,code=None,versions=None):
        return self.store.record(source_key=key,component='mail',code=code or key,
                                 versions=versions or {'version':'original'},snapshot={'kind':'mail'})
    def test_more_than_two_different_short_repairs_keep_running(self):
        for n in range(8):
            a=self.incident(str(n));jid=a['job']['job_id']
            self.assertTrue(self.store.dispatch(jid,'build:1',seconds=2460)['allowed'])
            self.store.guard.spent(jid,'build:1',20)
            self.store.guard.finish(jid,'build:1','published')
        self.assertEqual(len(self.store.pending()),8)  # Guard never forges native completion.
        with self.store.lifecycle.connect() as c:
            self.assertEqual(c.execute('SELECT sum(spent_seconds) FROM repair_dispatches').fetchone()[0],160)
    def test_business_text_fix_cannot_change_identity_or_calendar_wrappers(self):
        from self_repair.policy import check_source
        rel='gtm/calendar/runtime/calendar_routing.py'
        source=(ROOT/rel).read_text()
        fixed=source.replace("return bool(research or own_demo)","return bool(own_demo or research)")
        self.assertEqual(check_source(rel,source,fixed),['owned_business_source'])
        for forbidden in [source.replace("sender == CODE","sender != CODE"),
                          source.replace("return ACRIBIC if project == 'acribic' else CODE","return ACRIBIC")]:
            with self.assertRaises(PolicyError):check_source(rel,source,forbidden)
    def test_unresolved_cause_cannot_reset_budget_via_unrelated_code_version(self):
        a=self.incident('first','same',{'mail':'old','briefing':'old'});jid=a['job']['job_id']
        self.store.dispatch(jid,'build:1');self.store.guard.finish(jid,'build:1','needs_review')
        b=self.incident('second','same',{'mail':'old','briefing':'changed'})
        decision=self.store.dispatch(b['job']['job_id'],'build:1')
        self.assertFalse(decision['allowed']);self.assertEqual(decision['code'],'same_cause_paused')
        c=self.incident('independent','other')
        self.assertTrue(self.store.dispatch(c['job']['job_id'],'build:1')['allowed'])
    def test_same_feedback_cannot_buy_another_attempt(self):
        a=self.incident('case');jid=a['job']['job_id']
        self.store.dispatch(jid,'build:1');self.store.guard.finish(jid,'build:1','needs_review')
        self.assertTrue(self.store.dispatch(jid,'build:2',retry_key='new-countercase')['allowed'])
        self.store.guard.finish(jid,'build:2','needs_review')
        d=self.store.dispatch(jid,'build:3',retry_key='new-countercase')
        self.assertFalse(d['allowed']);self.assertEqual(d['code'],'same_feedback_already_tried')
        # The fixed two-attempt cause cap is gone: genuinely new evidence buys
        # another attempt. The lifecycle max_attempts budget (20) plus circuit
        # and model-time backstops bound the total instead.
        d=self.store.dispatch(jid,'build:3',retry_key='third-new-description')
        self.assertTrue(d['allowed'])
    def test_three_failures_pause_repair_service_then_allow_one_half_open_trial(self):
        now=100000
        for n in range(3):
            a=self.incident(str(n));jid=a['job']['job_id']
            self.assertTrue(self.store.dispatch(jid,'build:1',seconds=100,now=now+n)['allowed'])
            self.store.guard.spent(jid,'build:1',10)
            self.store.guard.finish(jid,'build:1','needs_review',now=now+n+1)
        fourth=self.incident('fourth')['job']['job_id']
        d=self.store.dispatch(fourth,'build:1',seconds=100,now=now+5)
        self.assertEqual(d['code'],'repair_circuit_cooldown')
        self.assertTrue(self.store.dispatch(fourth,'build:1',seconds=100,now=now+COOLDOWN_SECONDS+5)['allowed'])
    def test_crash_keeps_worst_case_reservation_after_restart(self):
        a=self.incident('a')['job']['job_id'];b=self.incident('b')['job']['job_id']
        self.assertTrue(self.store.dispatch(a,'build:1',seconds=MODEL_TIME_SECONDS-10)['allowed'])
        reboot=Incidents(self.root)
        self.assertFalse(reboot.dispatch(b,'build:1',seconds=60)['allowed'])
        self.assertTrue(reboot.dispatch(a,'build:1',seconds=MODEL_TIME_SECONDS-10)['allowed'])
    def test_code_build_cooldown_does_not_block_independent_mail_recovery_or_verified_readback(self):
        now=100000
        for n in range(3):
            a=self.incident('broken-build-'+str(n));jid=a['job']['job_id']
            self.assertTrue(self.store.dispatch(jid,'build:1',seconds=60,now=now+n)['allowed'])
            self.store.guard.spent(jid,'build:1',1)
            self.store.guard.finish(jid,'build:1','needs_review',now=now+n+1)
        other=self.incident('another-conversation')['job']['job_id']
        self.assertTrue(self.store.dispatch(other,'recovery:1',seconds=60,now=now+5)['allowed'])
        proof=self.incident('verified-parent-fix')['job']['job_id']
        self.assertTrue(self.store.dispatch(proof,'parent-verified:1',seconds=60,now=now+5)['allowed'])
        self.assertEqual(self.store.dispatch(proof,'build:1',seconds=60,now=now+5)['code'],'repair_circuit_cooldown')
    def test_time_budget_deferral_does_not_consume_native_attempt(self):
        a=self.incident('a')['job']['job_id'];b=self.incident('b')
        self.store.dispatch(a,'build:1',seconds=MODEL_TIME_SECONDS)
        engine=Engine.__new__(Engine);engine.root=self.root;engine.store=self.store
        engine.recovery_enabled=False
        result=engine.work(b)
        self.assertEqual(result['state'],'deferred');self.assertEqual(result['code'],'repair_time_budget_wait')
        self.assertEqual(self.store.lifecycle.get_job(b['job']['job_id'])['attempts'],0)
    def test_concurrent_admission_cannot_overspend(self):
        ids=[self.incident(str(n))['job']['job_id'] for n in range(2)]
        with ThreadPoolExecutor(2) as pool:
            out=list(pool.map(lambda j:self.store.dispatch(j,'build:1',seconds=MODEL_TIME_SECONDS),ids))
        self.assertEqual(sum(x['allowed'] for x in out),1)
    def test_unknown_legacy_cost_is_retained_until_closed_then_expires(self):
        with self.store.lifecycle.connect() as c:
            c.execute("INSERT INTO repair_dispatches(job_id,phase,started) VALUES('old','build:1',100000)")
        a=self.incident('new')['job']['job_id']
        d=self.store.dispatch(a,'build:1',seconds=MODEL_TIME_SECONDS,now=100001)
        self.assertEqual(d['code'],'repair_time_budget_wait')
        self.assertFalse(self.store.dispatch(a,'build:1',seconds=MODEL_TIME_SECONDS,now=100001+WINDOW_SECONDS)['allowed'])
        with self.store.lifecycle.connect() as c:c.execute("UPDATE repair_dispatches SET finished=100010 WHERE job_id='old'")
        self.assertFalse(self.store.dispatch(a,'build:1',seconds=MODEL_TIME_SECONDS,now=100001+WINDOW_SECONDS)['allowed'])
        self.assertTrue(self.store.dispatch(a,'build:1',seconds=MODEL_TIME_SECONDS,now=100011+WINDOW_SECONDS)['allowed'])
    def test_late_recovery_time_expires_from_finish_not_old_dispatch_start(self):
        a=self.incident('old')['job']['job_id'];b=self.incident('new')['job']['job_id']
        self.store.dispatch(a,'build:1',seconds=MODEL_TIME_SECONDS,now=100000)
        self.store.guard.spent(a,'build:1',2400)
        self.store.guard.finish(a,'build:1','published',now=102400)
        self.assertFalse(self.store.dispatch(b,'build:1',seconds=MODEL_TIME_SECONDS,now=186401)['allowed'])
        self.assertTrue(self.store.dispatch(b,'build:1',seconds=MODEL_TIME_SECONDS,now=188801)['allowed'])
    def test_abandoned_reservation_settles_conservatively_without_native_job_mutation(self):
        a=self.incident('old');jid=a['job']['job_id']
        self.store.dispatch(jid,'build:1',seconds=MODEL_TIME_SECONDS,now=100000)
        j=self.store.lifecycle.start_job(jid,owner='x',lease_seconds=120)
        self.store.guard.spent(jid,'build:1',20)
        self.store.lifecycle.fail_job(jid,error_type='private_interruption',owner='x',lease_generation=j['lease_generation'])
        original=self.store.lifecycle.get_job(jid)
        self.store.guard.settle_abandoned(now=200000)
        self.assertEqual(self.store.lifecycle.get_job(jid),original)
        with self.store.lifecycle.connect() as c:r=c.execute('SELECT * FROM repair_dispatches WHERE job_id=?',(jid,)).fetchone()
        self.assertEqual(r['spent_seconds'],MODEL_TIME_SECONDS);self.assertEqual(r['finished'],200000)
    def test_concurrent_first_schema_migration_is_idempotent(self):
        with self.store.lifecycle.connect() as c:
            c.execute('DROP TABLE repair_dispatches')
            c.execute("CREATE TABLE repair_dispatches(job_id TEXT,phase TEXT,started REAL,thread_id TEXT DEFAULT '',result TEXT DEFAULT '',PRIMARY KEY(job_id,phase))")
        with ThreadPoolExecutor(2) as pool:list(pool.map(lambda _:Incidents(self.root),range(2)))
        with self.store.lifecycle.connect() as c:columns={r['name'] for r in c.execute('PRAGMA table_info(repair_dispatches)')}
        self.assertTrue({'spent_seconds','reserved_seconds','finished','cause','retry_key'}<=columns)
    def test_failed_attempt_without_concrete_feedback_is_not_requeued(self):
        a=self.incident('x');jid=a['job']['job_id'];j=self.store.lifecycle.start_job(jid,owner='x',lease_seconds=120)
        self.store.lifecycle.fail_job(jid,error_type='independent_regression_failed',owner='x',lease_generation=j['lease_generation'])
        self.store.result(a['signature'],{'code':'independent_regression_failed','retry_at':0})
        engine=Engine.__new__(Engine);engine.root=self.root;engine.store=self.store
        engine.reconcile()
        self.assertEqual(self.store.lifecycle.get_job(jid)['state'],'failed')
        private_json(self.root/'jobs'/jid/'gate-failure.json',{'details':'new actual failed countercase'})
        engine.reconcile()
        self.assertEqual(self.store.lifecycle.get_job(jid)['state'],'queued')
    def test_cosmetic_candidate_change_is_not_a_new_approach(self):
        for rel in TARGETS:
            p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('def f():\n    return 1\n')
        before=semantic_fingerprint(self.root)
        p=self.root/next(iter(TARGETS));p.write_text('# another description\n'+p.read_text())
        self.assertEqual(semantic_fingerprint(self.root),before)
        p.write_text(p.read_text().replace('return 1','return 2'))
        self.assertNotEqual(semantic_fingerprint(self.root),before)
    def test_timeout_is_metered_and_process_group_stopped(self):
        from self_repair.codex import Codex
        work=self.root/'work';work.mkdir();calls=[]
        codex=Codex('/usr/bin/true',timeout=1200);codex.meter=calls.append
        class Process:
            pid=99999
            def communicate(self,*a,**k):raise subprocess.TimeoutExpired('private',1200)
            def wait(self,**k):return 0
        with patch('self_repair.codex.subprocess.Popen',return_value=Process()),patch('self_repair.codex.os.killpg') as stop,patch('self_repair.codex.time.monotonic',side_effect=[10,16]):
            with self.assertRaises(subprocess.TimeoutExpired):codex.run(work,'private',self.root/'build.json')
        self.assertEqual(calls,[6]);stop.assert_called_once()

    def probe_case(self,code):
        a=self.incident('proof',code);jid=a['job']['job_id'];directory=self.root/'jobs'/jid
        for folder in ['work','before']:
            for rel in TARGETS:
                p=directory/folder/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('def value():\n    return 1\n')
        (directory/'work/repro.py').write_text('assert False')
        self.store.dispatch(jid,'build:1')
        started=self.store.lifecycle.start_job(jid,owner='probe',lease_seconds=120)
        return a,directory,{'owner':'probe','lease_generation':started['lease_generation']}
    def assert_one_retry_after_real_feedback(self,a,directory,fence,code):
        jid=a['job']['job_id'];self.store.lifecycle.fail_job(jid,error_type=code,**fence)
        self.store.result(a['signature'],{'code':code,'retry_at':0})
        self.store.guard.finish(jid,'build:1','needs_review')
        engine=Engine.__new__(Engine);engine.root=self.root;engine.store=self.store
        engine.reconcile();current=self.store.get(a['signature'])
        self.assertEqual(current['job']['state'],'queued')
        key=retry_evidence(current,directory);self.assertTrue(key)
        self.assertTrue(self.store.dispatch(jid,'build:2',retry_key=key)['allowed'])
        self.store.guard.finish(jid,'build:2','needs_review')
        self.assertFalse(self.store.dispatch(jid,'build:3',retry_key=key)['allowed'])
    def test_failed_original_probe_produces_actual_feedback_for_one_retry(self):
        from types import SimpleNamespace
        a,directory,fence=self.probe_case('original_not_proven_red_to_green')
        class Commands:
            def command(self,*args,**kw):return SimpleNamespace(returncode=1,stdout='actual failed countercase',stderr='')
        with self.assertRaises(PolicyError):original_probe(Commands(),directory/'work',directory/'before')
        feedback=json.loads((directory/'gate-failure.json').read_text())
        self.assertEqual(feedback['details']['candidate_exit'],1)
        self.assert_one_retry_after_real_feedback(a,directory,fence,'original_not_proven_red_to_green')
    def test_failed_native_probe_produces_validation_feedback_without_sdk_stderr(self):
        from types import SimpleNamespace
        a,directory,fence=self.probe_case('native_briefing_quality_failed')
        engine=Engine.__new__(Engine)
        outputs=[SimpleNamespace(returncode=0,stdout='{"baseline_failed":true}',stderr=''),
                 SimpleNamespace(returncode=1,stdout='{"validation_code":"briefing_question_not_grounded"}',stderr='PRIVATE_AUTH_HEADER')]
        with patch('self_repair.service.subprocess.run',side_effect=outputs):
            with self.assertRaises(PolicyError):engine.native_briefing_probe(a,directory/'work',directory/'before',directory)
        feedback=(directory/'gate-failure.json').read_text()
        self.assertIn('briefing_question_not_grounded',feedback);self.assertNotIn('PRIVATE_AUTH_HEADER',feedback)
        self.assert_one_retry_after_real_feedback(a,directory,fence,'native_briefing_quality_failed')

if __name__=='__main__':unittest.main()
