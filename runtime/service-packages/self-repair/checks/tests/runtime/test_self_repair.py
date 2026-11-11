import copy
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'second-brain/src'))
from self_repair.policy import check_source,changed_files,PolicyError,digest
from self_repair.store import Incidents,private_json
from self_repair.publish import install,reconcile,Busy

REL='gtm/briefing.py'
SOURCE='RULES="Business approvals stay"\ndef render_briefing(value):\n    return value.strip()\n'
FIXED=SOURCE.replace('value.strip()','value.strip()+"."')

class UnitControl:
    def __init__(self):self.state='active';self.events=[]
    def __call__(self,*args):
        self.events.append(args)
        if args[0]=='stop':self.state='inactive'
        if args[0]=='start':self.state='active'
        if args[0]=='show':return 'LoadState=loaded\nActiveState='+self.state+'\nMainPID='+('0' if self.state=='inactive' else '123')+'\nControlPID=0\nExecMainStartTimestampMonotonic=100'
        return self.state

class RepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
    def test_pure_scoped_change_is_accepted(self):
        self.assertEqual(check_source(REL,SOURCE,FIXED),['render_briefing'])
    def test_rules_api_wrappers_and_new_module_code_are_protected(self):
        for new in [FIXED.replace('Business approvals stay','No approvals'),
                    FIXED+'\nopen("elsewhere","w")\n',
                    FIXED.replace('render_briefing(value)','render_briefing(value=open("elsewhere"))')]:
            with self.assertRaises(PolicyError):check_source(REL,SOURCE,new)
    def test_pure_function_cannot_gain_shell_file_network_or_dunder_access(self):
        for expr in ['open("private")','os.system("anything")','__import__("os")','value.__class__',
                     'globals()','getattr(value,"run")']:
            with self.assertRaises(PolicyError):check_source(REL,SOURCE,SOURCE.replace('value.strip()',expr))
    def test_attribute_methods_and_indirect_calls_cannot_bypass_purity(self):
        for expr in ['value.write_text("private")','value.unlink()','value.read_bytes()',
                     'value["callable"]()','(lambda: value.strip())()']:
            with self.assertRaises(PolicyError):check_source(REL,SOURCE,SOURCE.replace('value.strip()',expr))
    def test_callables_cannot_be_aliased_to_privileged_global_traversal(self):
        for statement in ['zip=re.enum.sys.modules["builtins"].open; zip("private","w")',
                          'zip=value; zip("private","w")',
                          'for zip in [value]:\n        zip("private","w")']:
            new=SOURCE.replace('return value.strip()',statement+'\n    return value.strip()')
            with self.assertRaises(PolicyError):check_source(REL,SOURCE,new)
    def test_manifest_binds_originals_even_if_both_trees_are_identically_changed(self):
        from self_repair.bundle import immutable_bundle
        before=self.root/'before';work=self.root/'work'
        for root in (before,work):
            root.mkdir();(root/'test.py').write_text('assert False')
        private_json(self.root/'bundle-manifest.json',{'files':{'test.py':digest(before/'test.py')}})
        immutable_bundle(self.root,before,work)
        for root in (before,work):(root/'test.py').write_text('assert True')
        with self.assertRaises(PolicyError):immutable_bundle(self.root,before,work)
    def test_candidate_cannot_modify_tests_or_add_hidden_dependency(self):
        before=self.root/'before';after=self.root/'after'
        for folder in (before,after):
            (folder/REL).parent.mkdir(parents=True);(folder/REL).write_text(SOURCE)
        (after/REL).write_text(FIXED)
        self.assertEqual(list(changed_files(before,after)),[REL])
        (after/'injected.py').write_text('extra')
        with self.assertRaises(PolicyError):changed_files(before,after)
    def test_symlink_parent_is_not_a_candidate_source(self):
        before=self.root/'before';after=self.root/'after';before.mkdir();after.mkdir()
        (before/'original.py').write_text(SOURCE);(after/'leak').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(PolicyError):changed_files(before,after)
    def test_same_source_survives_restart_without_another_job(self):
        db=Incidents(self.root/'state')
        args=dict(source_key='source1',component='mail',code='requested_multiple_options_missing',versions={'file':'a'*64},snapshot={'original':'complete'})
        a=db.record(**args);b=Incidents(self.root/'state').record(**args)
        self.assertEqual(a['job']['job_id'],b['job']['job_id']);self.assertEqual(len(db.pending()),1)
        self.assertEqual(json.loads(Path(a['sources'][0]['snapshot_path']).read_text()),{'original':'complete'})
    def test_multiple_cases_of_same_code_version_share_one_repair(self):
        db=Incidents(self.root/'state')
        args=dict(component='mail',code='validation',versions={'file':'a'*64},snapshot={'original':'complete'})
        a=db.record(source_key='source1',**args);b=db.record(source_key='source2',**args)
        self.assertEqual(a['signature'],b['signature']);self.assertEqual(len(b['sources']),2)
        self.assertEqual(len(db.pending()),1)
    def test_time_reservations_are_durable_and_idempotent(self):
        db=Incidents(self.root/'state')
        self.assertTrue(db.dispatch('job1','build:1')['allowed']);self.assertTrue(db.dispatch('job1','build:1')['allowed'])
        self.assertTrue(db.dispatch('job2','build:1')['allowed'])
        self.assertFalse(Incidents(self.root/'state').dispatch('job3','build:1')['allowed'])
    def test_native_lease_prevents_two_workers_for_one_reservation(self):
        db=Incidents(self.root/'state')
        a=db.record(source_key='x',component='mail',code='x',versions={},snapshot={})
        jid=a['job']['job_id'];db.lifecycle.start_job(jid,owner='first',lease_seconds=120)
        with self.assertRaises(ValueError):db.lifecycle.start_job(jid,owner='second',lease_seconds=120)
    def setup_publish(self):
        before=self.root/'before';work=self.root/'work';target=self.root/'live/code.py'
        for folder in (before,work):
            (folder/REL).parent.mkdir(parents=True);(folder/REL).write_text(SOURCE)
        (work/REL).write_text(FIXED);target.parent.mkdir();target.write_text(SOURCE)
        state=target.parent/'business-state';state.write_text('current business state')
        return before,work,target,state
    def test_busy_runtime_is_left_untouched(self):
        before,work,target,state=self.setup_publish()
        with self.assertRaises(Busy):install(work,before,self.root/'tx',targets={REL:target},is_idle=lambda _:False)
        self.assertEqual(target.read_text(),SOURCE)
    def test_foreign_source_change_is_not_overwritten(self):
        before,work,target,state=self.setup_publish();target.write_text('foreign newer work')
        with self.assertRaises(PolicyError):install(work,before,self.root/'tx',targets={REL:target},is_idle=lambda _:True)
        self.assertEqual(target.read_text(),'foreign newer work')
    def test_actual_publisher_saves_code_but_retains_business_state(self):
        before,work,target,state=self.setup_publish();ctl=UnitControl()
        receipt=install(work,before,self.root/'tx',targets={REL:target},control=ctl,is_idle=lambda _:True)
        self.assertEqual(receipt['phase'],'active');self.assertEqual(target.read_text(),FIXED)
        self.assertEqual(state.read_text(),'current business state')
        receipt2=install(work,before,self.root/'tx',targets={REL:target},control=ctl,is_idle=lambda _:True)
        self.assertEqual(receipt2['phase'],'active')
        self.assertEqual(sum(e[0]=='stop' for e in ctl.events),1)
    def test_failed_start_rolls_back_only_exact_own_code(self):
        before,work,target,state=self.setup_publish();units=UnitControl()
        def ctl(*args):
            result=units(*args)
            if args[0]=='stop':state.write_text('new data retained')
            if args[0]=='start' and sum(x[0]=='start' for x in units.events)==1:
                units.state='inactive';raise RuntimeError('confirmed failed start')
            return result
        with self.assertRaises(RuntimeError):install(work,before,self.root/'tx',targets={REL:target},control=ctl,is_idle=lambda _:True)
        self.assertEqual(target.read_text(),SOURCE);self.assertEqual(state.read_text(),'new data retained')
        self.assertEqual(sum(x[0]=='stop' for x in units.events),2)
    def test_positive_process_readback_reconciles_lost_start_ack_once(self):
        before,work,target,state=self.setup_publish();units=UnitControl()
        def ctl(*args):
            result=units(*args)
            if args[0]=='start':raise TimeoutError('ack lost after actual start')
            return result
        result=install(work,before,self.root/'tx',targets={REL:target},control=ctl,is_idle=lambda _:True)
        self.assertEqual(result['phase'],'active');self.assertTrue(result['start_ack_reconciled'])
        self.assertEqual(target.read_text(),FIXED)
        self.assertEqual(sum(x[0]=='start' for x in units.events),1)
    def test_unknown_restart_and_unproven_stop_never_roll_back_running_code(self):
        before,work,target,state=self.setup_publish();units=UnitControl()
        def ctl(*args):
            if args[0]=='stop' and any(e[0]=='start' for e in units.events):raise TimeoutError('stop unknown')
            if args[0]=='show' and any(e[0]=='start' for e in units.events):raise TimeoutError('readback unavailable')
            result=units(*args)
            if args[0]=='start':raise TimeoutError('start ack unknown')
            return result
        with self.assertRaises(PolicyError):install(work,before,self.root/'tx',targets={REL:target},control=ctl,is_idle=lambda _:True)
        self.assertEqual(target.read_text(),FIXED)
        self.assertEqual(json.loads((self.root/'tx/publication.json').read_text())['phase'],'runtime_state_unknown')
    def test_unproven_initial_stop_does_not_change_live_files(self):
        before,work,target,state=self.setup_publish();units=UnitControl()
        def ctl(*args):
            if args[0]=='stop':raise TimeoutError('still active')
            return units(*args)
        with self.assertRaises(PolicyError):install(work,before,self.root/'tx',targets={REL:target},control=ctl,is_idle=lambda _:True)
        self.assertEqual(target.read_text(),SOURCE)
    def test_interrupted_publication_is_reconciled_without_data_restore(self):
        before,work,target,state=self.setup_publish();backup=self.root/'backup.py';backup.write_text(SOURCE)
        target.write_text(FIXED);journal=self.root/'publication.json'
        private_json(journal,{'phase':'applying','units':['briefing.service'],'files':[{
            'target':str(target),'backup':str(backup),'before':digest(backup),'after':digest(target)}]})
        reconcile(journal,control=UnitControl())
        self.assertEqual(target.read_text(),SOURCE);self.assertEqual(state.read_text(),'current business state')
    def test_crash_after_actual_start_reconciles_by_readback_without_rollback(self):
        before,work,target,state=self.setup_publish();backup=self.root/'backup.py';backup.write_text(SOURCE)
        target.write_text(FIXED);journal=self.root/'publication.json';units=UnitControl()
        private_json(journal,{'phase':'starting','units':['briefing.service'],'files':[{
            'target':str(target),'backup':str(backup),'before':digest(backup),'after':digest(target)}]})
        result=reconcile(journal,control=units)
        self.assertEqual(result['phase'],'active');self.assertEqual(target.read_text(),FIXED)
        self.assertFalse(any(x[0]=='stop' for x in units.events))
    def test_failed_native_job_resumes_using_durable_owner_fence(self):
        from self_repair.service import Engine
        engine=Engine.__new__(Engine);engine.root=self.root;engine.store=Incidents(self.root)
        a=engine.store.record(source_key='x',component='mail',code='x',versions={},snapshot={})
        jid=a['job']['job_id'];started=engine.store.lifecycle.start_job(jid,owner='first',lease_seconds=120)
        engine.store.lifecycle.fail_job(jid,error_type='independent_regression_failed',owner='first',lease_generation=started['lease_generation'])
        engine.store.result(a['signature'],{'retry_at':0,'code':'independent_regression_failed'})
        private_json(self.root/'jobs'/jid/'gate-failure.json',{'details':'actual failing calendar countercase'})
        engine.reconcile()
        self.assertEqual(engine.store.lifecycle.get_job(jid)['state'],'queued')
        self.assertEqual(engine.store.lease_fence(jid)['owner'],'')
    def test_published_job_resume_does_not_build_or_install_again(self):
        from self_repair.service import Engine
        engine=Engine.__new__(Engine);engine.root=self.root;engine.store=Incidents(self.root)
        engine.recovery_enabled=True;engine.publish_enabled=True
        engine.codex=SimpleNamespace();engine.recover=lambda *a:{'state':'restored'}
        a=engine.store.record(source_key='x',component='mail',code='x',versions={},snapshot={})
        directory=self.root/'jobs'/a['job']['job_id'];journal=directory/'publication-1/publication.json'
        private_json(journal,{'phase':'active'})
        with patch('self_repair.service.active_record',return_value={'phase':'active'}),patch('self_repair.service.build_bundle',side_effect=AssertionError('rebuilt')):
            result=engine.work(a)
        self.assertEqual(result['state'],'restored');self.assertTrue(result['resumed'])
        self.assertEqual(engine.store.lifecycle.get_job(a['job']['job_id'])['state'],'completed')

if __name__=='__main__':unittest.main()
