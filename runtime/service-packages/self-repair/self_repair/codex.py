"""Official Codex CLI, explicit filesystem/network policy, bounded subprocesses."""
from __future__ import annotations
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

MODEL='gpt-6.1-sol'
EFFORT='medium'
SCHEMA={'type':'object','properties':{
    'status':{'type':'string','enum':['patched','approved','rejected','needs_input','unresolved']},
    'summary':{'type':'string'},'changed_files':{'type':'array','items':{'type':'string'}},
    'evidence':{'type':'string'}},'required':['status','summary','changed_files','evidence'],
    'additionalProperties':False}

class Codex:
    def __init__(self,binary,*,read_roots=(),timeout=1200):
        self.binary=Path(binary).resolve(strict=True)
        self.read_roots=[str(self.binary.parent.parent),*[str(Path(x).resolve()) for x in read_roots]]
        self.timeout=timeout
        self.meter=None
    def settings(self,*,readonly=False):
        fs={':minimal':'read',':workspace_roots':{'.':'read','.tmp':'write'} if readonly else {'.':'write'}}
        fs.update({p:'read' for p in self.read_roots})
        def toml(value):
            if isinstance(value,str):return json.dumps(value)
            return '{'+', '.join(json.dumps(k)+'='+toml(v) for k,v in value.items())+'}'
        # One inline table preserves dots in literal filesystem paths. The CLI's
        # dotted override parser does not preserve those quoted path segments.
        args=['-c','default_permissions="hermes-repair"',
              '-c','permissions.hermes-repair.network.enabled=false',
              '-c','permissions.hermes-repair.filesystem='+toml(fs)]
        return args
    def env(self,work):
        return {'HOME':str(Path.home()),'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','TZ':'Europe/Berlin',
                'CODEX_HOME':str(Path.home()/'.codex'),'TMPDIR':str(Path(work)/'.tmp'),
                'PYTHONDONTWRITEBYTECODE':'1','HERMES_DISABLE_LAZY_INSTALLS':'1',
                'HERMES_HOME':str(Path(work)/'.private-home')}
    def temporary(self,work):
        temp=Path(work)/'.tmp'
        if temp.is_symlink():raise RuntimeError('sandbox_temporary_symlink')
        temp.mkdir(exist_ok=True,mode=0o700)
    def command(self,work,argv,*,readonly=False,timeout=120):
        work=Path(work);self.temporary(work)
        cmd=[str(self.binary),'sandbox',*self.settings(readonly=readonly),'-P','hermes-repair',*map(str,argv)]
        return subprocess.run(cmd,cwd=work,env=self.env(work),capture_output=True,text=True,timeout=timeout)
    def preflight(self,work):
        # No model request. Only a harmless sentinel in the owned parent folder.
        work=Path(work);outside=work.parent/'outside-read-probe'
        outside.write_text('private-sentinel');outside.chmod(0o600)
        code=('from pathlib import Path; import socket\n'
              'Path(".sandbox-check").write_text("ok")\n'
              'denied=False\n'
              'try:Path('+repr(str(outside))+').read_text()\n'
              'except OSError:denied=True\n'
              'assert denied,"outside read allowed"\n'
              'denied=False\n'
              'try:Path('+repr(str(outside))+').write_text("bad")\n'
              'except OSError:denied=True\n'
              # Bubblewrap may expose writable *ephemeral* parent scaffolding.
              # The host sentinel is therefore checked by the parent below.
              'denied=False\n'
              'try:\n s=socket.socket();s.settimeout(2);s.connect(("1.1.1.1",443))\n'
              'except OSError as e:denied=e.errno in (1,13,101)\n'
              'assert denied,"network was not proven denied"\n')
        r=self.command(work,['/usr/bin/python3','-c',code])
        if outside.read_text()!='private-sentinel':raise RuntimeError('sandbox_host_write_leaked')
        if r.returncode:
            diagnostic=work.parent/'sandbox-preflight.json'
            diagnostic.write_text(json.dumps({'returncode':r.returncode,'stderr':r.stderr[-2000:]}));diagnostic.chmod(0o600)
            raise RuntimeError('sandbox_preflight_failed')
        (work/'.sandbox-check').unlink()
    def run(self,work,prompt,output,*,readonly=False):
        work,output=Path(work),Path(output)
        self.temporary(work)
        schema=output.with_suffix('.schema.json');schema.write_text(json.dumps(SCHEMA));schema.chmod(0o600)
        argv=[str(self.binary),'exec','--ignore-user-config',*self.settings(readonly=readonly),
              '-c','approval_policy="never"','-c','model_reasoning_effort="'+EFFORT+'"',
              '-c','shell_environment_policy.inherit="none"',
              '-c','shell_environment_policy.set.PATH="/usr/bin:/bin"',
              '-c','shell_environment_policy.set.PYTHONDONTWRITEBYTECODE="1"',
              '-c','shell_environment_policy.set.TMPDIR='+json.dumps(str(work/'.tmp')),
              '-c','shell_environment_policy.set.HERMES_HOME='+json.dumps(str(work/'.private-home')),
              '-c','shell_environment_policy.set.PYTEST_ADDOPTS="--basetemp=.tmp/pytest-agent -o cache_dir=.tmp/pytest-cache-agent"',
              '-m',MODEL,'--color','never','--skip-git-repo-check',
              '--output-schema',str(schema),'--output-last-message',str(output),'-']
        # The full stdout/stderr stream can include model reasoning or private
        # tool output. It is never printed or added to observability.
        with tempfile.TemporaryFile() as stdout,tempfile.TemporaryFile() as stderr:
            p=subprocess.Popen(argv,cwd=work,env=self.env(work),stdin=subprocess.PIPE,
                               stdout=stdout,stderr=stderr,text=True,start_new_session=True)
            started=time.monotonic()
            try:p.communicate(prompt,timeout=self.timeout)
            except BaseException:
                os.killpg(p.pid,signal.SIGTERM)
                try:p.wait(timeout=5)
                except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
                raise
            finally:
                if self.meter:self.meter(time.monotonic()-started)
            if p.returncode or not output.is_file():
                stderr.seek(0,os.SEEK_END);size=stderr.tell();stderr.seek(max(0,size-12000))
                tail=stderr.read().decode('utf-8','replace').lower()
                code=next((code for needle,code in (
                    ('usage limit','codex_quota'),('rate_limit','codex_quota'),
                    ('not logged in','codex_auth'),('unauthorized','codex_auth'),
                    ('not supported','codex_model_unavailable'),('model_not_found','codex_model_unavailable'),
                    ('bubblewrap','codex_sandbox'),('bwrap:','codex_sandbox')) if needle in tail),'codex_run_failed')
                raise RuntimeError(code)
            stderr.seek(0)
            header=stderr.read(8000).decode('utf-8','replace')
            match=re.search(r'session id:\s*([a-f0-9-]{36})',header,re.I)
            self.last_thread=match.group(1) if match else ''
        output.chmod(0o600)
        raw=output.read_bytes()
        if len(raw)>25000:raise RuntimeError('codex_result_unbounded')
        value=json.loads(raw)
        if (set(value)!=set(SCHEMA['required']) or value.get('status') not in SCHEMA['properties']['status']['enum']
                or not isinstance(value.get('changed_files'),list)):
            raise RuntimeError('codex_result_invalid')
        return value
