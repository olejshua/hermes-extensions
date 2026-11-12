"""Persistent singleton scheduler. Main acceptance never waits for completion."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from .contracts import checked_policy,ContractError
from .store import Store
from .slots import try_slot
from .processes import birth,group_stop,parent_death

class Supervisor:
    def __init__(self,root,policy,*,fixture=False):
        # Restart must persist a readable pause rather than crash-loop on a changed
        # reviewed implementation. Each native spawn below rechecks current hashes.
        from .service import BackgroundService
        self.policy=checked_policy(policy,fixture=fixture,verify_runtime=False);self.service=BackgroundService(root,self.policy,fixture=fixture)
        self.store=self.service.store;self.fixture=fixture;self.children={};self.stop=False
        self._draft_runtime=None;self.recovery_children=set();self.reconcile_children=set()
    def draft_runtime(self):
        if self._draft_runtime is None:
            from .draft_runtime import DraftRuntime
            self._draft_runtime=DraftRuntime(self.store,self.policy)
        return self._draft_runtime
    def worker_command(self,job_id,item_id,slot):
        entry=Path(__file__).with_name('worker_entry.py').resolve()
        args=[str(self.store.root),self.policy['policy_path'],job_id,item_id,'--slot-fd='+str(slot.fd),'--slot-index='+str(slot.index)]
        if (job_id,item_id) in self.recovery_children:args.append('--recovery-only')
        if (job_id,item_id) in self.reconcile_children:args.append('--reconcile-only')
        if self.store.row(job_id)['profile']in {'outreach-acquire-staging-v1','outreach-drafts-staging-v1'}:
            if self.policy.get('outreach_package')or not self.fixture:
                package=Path(self.policy.get('outreach_package',''))
                if not package.is_absolute()or package.is_symlink()or not package.is_dir():
                    raise ContractError('native_outreach_package_required')
                if str(package)not in sys.path:sys.path.insert(0,str(package))
                from outreach_night.host_runtime import RELEASE,registered_paths,background_command
                if RELEASE.exists()and 'outreach'in registered_paths()[0]['writers']['supported_routes']:
                    if self.fixture:args.append('--fixture')
                    return background_command(self.store,self.policy,entry,args)
        if self.fixture:
            args.append('--fixture')
            if not self.policy.get('native_fixture_bootstrap'):return [sys.executable,'-I',str(entry),*args]
        from .native import command
        return command(self.policy,entry,args)
    def recover_children(self):
        with self.store.lifecycle.connect() as c:rows=[dict(r) for r in c.execute('SELECT * FROM background_worker_births')]
        for r in rows:
            group_stop(r['pid'],r['birth'])
            self.store.release_dead_worker_leases(r['pid'],r['birth'])
        with self.store.lifecycle.connect() as c:
            c.execute('DELETE FROM background_worker_births')
            c.execute("UPDATE background_items SET state='pending' WHERE state='running'")
        self.store.lifecycle.detect_stale_jobs()
        if 'outreach-acquire-staging-v1' in self.policy.get('profiles',[]):
            from .draft_intake import DraftIntake
            DraftIntake(self.store,self.policy).recover_acceptances()
    def pending_items(self,row,items):
        """Native profile specialization point; default order stays unchanged."""
        if row['profile']=='outreach-drafts-staging-v1':
            return self.draft_runtime().pending_items(row,items)
        if row['profile']=='outreach-acquire-staging-v1' and json.loads(row['contract']).get('pool_continuation_ref'):
            if all(i['state']=='verified' for i in items):
                return [{**items[0], 'state':'pending', 'reconcile_only':True}]
        return items
    def tick(self):
        for slot,(p,job_id,item_id,owned_birth) in list(self.children.items()):
            cancel=self.store.row(job_id)['cancel_requested']
            if cancel and (job_id,item_id) not in self.recovery_children and p.poll() is None:group_stop(p.pid,owned_birth)
            if p.poll() is not None:
                p.wait()
                self.store.release_dead_worker_leases(p.pid,owned_birth)
                with self.store.lifecycle.connect() as c:c.execute('DELETE FROM background_worker_births WHERE slot=? AND worker_id=?',(slot,job_id+':'+item_id))
                item=next(i for i in self.store.items(job_id) if i['item_id']==item_id)
                if item['state']=='running':self.store.set_item(job_id,item_id,'pending')
                del self.children[slot]
                self.recovery_children.discard((job_id,item_id))
                self.reconcile_children.discard((job_id,item_id))
        self.service.reconcile_revisions()
        with self.store.lifecycle.connect() as c:
            rows=[dict(r) for r in c.execute("SELECT * FROM background_jobs WHERE state IN ('queued','running','cancelling') ORDER BY created")]
        for row in rows:
            job_id=row['job_id'];items=self.store.items(job_id)
            if row['cancel_requested'] and row['profile']!='outreach-drafts-staging-v1':
                if not any(v[1]==job_id for v in self.children.values()):self.store.update_job(job_id,state='cancelled')
                continue
            if row['profile']!='outreach-drafts-staging-v1':
                continuation=(row['profile']=='outreach-acquire-staging-v1' and json.loads(row['contract']).get('pool_continuation_ref'))
                if not continuation or not all(i['state']=='verified' for i in items):
                    self.store.completion(job_id)
                    if all(i['state'] in {'verified','blocked'} for i in items):continue
            pending=self.pending_items(row,items)
            if row['cancel_requested'] and not pending and not any(v[1]==job_id for v in self.children.values()):
                self.store.update_job(job_id,state='cancelled')
            for item in pending:
                if len(self.children)>=self.policy['max_workers']:return
                if item['state']!='pending' or any(v[1:3]==(job_id,item['item_id']) for v in self.children.values()):continue
                # Don't spin expired-lease recovery into an item blocker after a crash.
                with self.store.lifecycle.connect() as c:
                    live=c.execute("SELECT 1 FROM background_phases p JOIN lifecycle_jobs l ON p.native_job_id=l.job_id WHERE p.job_id=? AND p.item_id=? AND l.state IN ('running','checkpointed','verifying') AND l.lease_until>? LIMIT 1",(job_id,item['item_id'],time.time())).fetchone()
                if live:continue
                if not self.fixture:
                    try:checked_policy(self.policy,fixture=False)
                    except ContractError:
                        self.store.update_job(job_id,state='paused',runtime_blocker='native_runtime_review_required')
                        break # No respawn loop; existing workers/scopes remain intact.
                slot=try_slot(self.policy['slot_root'],background=True)
                if slot is None:return
                if slot.index in self.children:
                    slot.close();return # A just-exited process is reaped next tick before its slot is reused.
                env={k:os.environ[k] for k in ('PATH','LANG','LC_ALL','TZ','HOME') if k in os.environ}
                env['HERMES_HOME']=self.policy.get('hermes_home','/home/hermes/.hermes');env['HERMES_DISABLE_LAZY_INSTALLS']='1'
                if not self.fixture:env['HERMES_CHILD_SLOT_ROOT']=self.policy['slot_root']
                try:
                    if self.store.row(job_id)['cancel_requested'] and not item.get('recovery_only'):continue
                    if item.get('recovery_only'):self.recovery_children.add((job_id,item['item_id']))
                    if item.get('reconcile_only'):self.reconcile_children.add((job_id,item['item_id']))
                    else:self.store.set_item(job_id,item['item_id'],'running')
                    p=subprocess.Popen(self.worker_command(job_id,item['item_id'],slot),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                        env=env,start_new_session=True,close_fds=True,pass_fds=(slot.fd,),preexec_fn=parent_death)
                    b=birth(p.pid)
                    if b is None:p.wait(timeout=3);continue
                    self.children[slot.index]=(p,job_id,item['item_id'],b)
                    with self.store.lifecycle.connect() as c:
                        c.execute('INSERT OR REPLACE INTO background_worker_births VALUES(?,?,?,?)',(slot.index,job_id+':'+item['item_id'],p.pid,b))
                    self.store.update_job(job_id,state='cancelling' if row['cancel_requested'] else 'running')
                finally:slot.close()
    def run(self,*,idle_exit=None):
        fd=os.open(self.store.root/'supervisor.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return False
            with self.store.lifecycle.connect() as c:c.execute('INSERT OR REPLACE INTO background_controller VALUES(1,?,?,?)',(os.getpid(),birth(os.getpid()),time.time()))
            self.recover_children()
            def stop(*_):self.stop=True
            signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
            idle=time.monotonic();controller_heartbeat=time.monotonic()
            while not self.stop:
                if time.monotonic()-controller_heartbeat>=30:
                    with self.store.lifecycle.connect() as c:c.execute('UPDATE background_controller SET heartbeat=? WHERE pid=?',(time.time(),os.getpid()))
                    controller_heartbeat=time.monotonic()
                self.tick()
                if self.children:idle=time.monotonic()
                elif idle_exit is not None and time.monotonic()-idle>=idle_exit:break
                time.sleep(.1 if self.children else 1)
            return True
        finally:
            for p,j,i,b in list(self.children.values()):
                group_stop(p.pid,b);p.wait(timeout=4)
                self.store.release_dead_worker_leases(p.pid,b)
            with self.store.lifecycle.connect() as c:c.execute('DELETE FROM background_controller WHERE pid=?',(os.getpid(),))
            os.close(fd)
