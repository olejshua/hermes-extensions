"""Read the existing subscription status, without another scheduler or model."""
from __future__ import annotations
import importlib.util
import fcntl
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from self_repair.store import private_json,hash_value

PYTHON = Path('/home/hermes/.hermes/tools/python-3.14.7+20260901-linux-x64/bin/python3.14')
WORKER = Path('/home/hermes/.hermes/scripts/mail_model_status.py')
PROVIDER_CODES = {'mail_model_quota', 'mail_model_auth', 'mail_model_unavailable', 'codex_quota', 'codex_auth'}

class SubscriptionStatus:
    def __init__(self, *, worker=WORKER, python=PYTHON, runner=subprocess.run, clock=time.time, interval=300,
                 root='/home/hermes/.hermes/ole-repair'):
        self.worker=Path(worker);self.python=Path(python);self.runner=runner;self.clock=clock
        self.interval=interval;self.cache={};self.lock=threading.Lock()
        self._standard=None
        self.root=Path(root)

    @property
    def standard(self):
        if self._standard is None:
            spec=importlib.util.spec_from_file_location('trusted_mail_status',self.worker)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            self._standard=module
        return self._standard

    def _query(self,route,expected_account=None,*,restore=False):
        value=self.standard.unknown(route)
        env={key:os.environ[key]for key in ('HOME','PATH','LANG','TZ')if key in os.environ}
        env.update(PYTHONDONTWRITEBYTECODE='1',HERMES_DISABLE_LAZY_INSTALLS='1',HERMES_HOME='/home/hermes/.hermes')
        argv=[str(self.python),'-B',str(self.worker),'--route',route]
        if expected_account:argv+=['--account',expected_account]
        if restore:argv+=['--restore']
        try:
            run=self.runner(argv,text=True,capture_output=True,timeout=55 if restore else 20,env=env)
            if run.returncode==0 and len(run.stdout)<=16000:
                parsed=json.loads(run.stdout)
                if self.standard.valid(parsed,route=route):value=parsed
        except (OSError,ValueError,subprocess.TimeoutExpired):pass
        return value

    def restore(self,value):
        """Reserve before refresh; unchanged failed token is never replayed."""
        if (value.get('code')!='access_token_expired' or not value.get('refreshable')
                or not value.get('account_fingerprint')or not value.get('credential_epoch')):return value
        key=hash_value([value['route'],value['account_fingerprint'],value['credential_epoch']])
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        with(self.root/'subscription-recovery.lock').open('a')as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
            path=self.root/'subscription-recovery.json'
            if path.is_symlink():return value
            attempts=json.loads(path.read_bytes())if path.exists()else{}
            if key in attempts:return value
            # Only safe identity revisions and state, no credential or model
            # output. A crash also retains the reservation.
            attempts[key]={'started':self.clock(),'state':'reserved'}
            private_json(path,attempts)
            result=self._query(value['route'],value['account_fingerprint'],restore=True)
            attempts[key]['state']=result['state'];private_json(path,attempts)
            return result

    def read(self, route='mail', *, force=False, expected_account=None, restore=False):
        with self.lock:
            key=(route,expected_account)
            cached=self.cache.get(key)
            if cached and not force and self.clock()-cached[0]<self.interval:
                if restore:
                    try:
                        value=self.restore(cached[1]);self.cache[key]=(self.clock(),value);return value
                    except (OSError,ValueError):pass
                return cached[1]
            try:standard=self.standard
            except (OSError,ImportError):
                return {'state':'unknown','route':route,'code':'availability_unverified'}
            value=self._query(route,expected_account)
            if restore:
                try:value=self.restore(value)
                except (OSError,ValueError):pass
            self.cache[key]=(self.clock(),value)
            return value

    def advances(self, before, after, *, code=None):
        try:return self.standard.advances(before,after,code=code)
        except (OSError,ImportError):return False

    def key(self,value):
        try:return self.standard.evidence_key(value)
        except (OSError,ImportError):return None

def provider_route(code):
    return 'repair'if code in {'codex_quota','codex_auth'}else'mail'
