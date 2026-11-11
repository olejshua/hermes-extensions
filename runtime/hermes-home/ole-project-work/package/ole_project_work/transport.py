"""Private stdio app-server. No protocol, prompt, result or auth logging."""
import ctypes
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from .safety import Fault,private,checked,read_file,write_private

class Unknown(Fault):pass
class Rejected(Fault):pass

def toml(x):
    if isinstance(x,dict):return '{'+','.join(json.dumps(k)+'='+toml(v) for k,v in x.items())+'}'
    if isinstance(x,bool):return 'true' if x else 'false'
    return json.dumps(x)

def runtime(policy,jobroot):
    r=private(jobroot/'runtime');home=private(r/'codex');work=private(jobroot/'work');private(work/'.tmp')
    binary=checked(Path(policy['codex_binary']).resolve(strict=True))
    # Authentication stays on the host outside the tool sandbox. This link is
    # operator-selected, never supplied by Telegram or exposed to the model.
    auth=checked(policy['codex_auth_file'])
    if auth.stat().st_mode&0o077:raise Fault('private_auth_required')
    link=home/'auth.json'
    if link.is_symlink():
        if link.resolve()!=auth:raise Fault('unexpected_auth_link')
    elif link.exists():raise Fault('unexpected_auth_copy')
    else:link.symlink_to(auth)
    fs={':root':'deny',':minimal':'read',str(work):'write',str(jobroot/'sources'):'read',str(binary.resolve().parent):'read'}
    config='default_permissions="hermes-project"\napproval_policy="never"\nmodel="gpt-6.1-sol"\nmodel_reasoning_effort="medium"\nweb_search="live"\n[permissions.hermes-project]\nnetwork.enabled=false\nfilesystem='+toml(fs)+'\n[shell_environment_policy]\ninherit="none"\n[shell_environment_policy.set]\nPATH="/usr/bin:/bin"\nTMPDIR='+toml(str(work/'.tmp'))+'\nHOME='+toml(str(work/'.private-home'))+'\nHERMES_DISABLE_LAZY_INSTALLS="1"\nPYTHONDONTWRITEBYTECODE="1"\n[analytics]\nenabled=false\n[features]\nmulti_agent=false\napps=false\n'
    write_private(home/'config.toml',config)
    env={'PATH':'/usr/bin:/bin','HOME':str(r),'CODEX_HOME':str(home),'LANG':'C.UTF-8','TZ':'Europe/Berlin','TMPDIR':str(work/'.tmp'),'PYTHONDONTWRITEBYTECODE':'1','HERMES_DISABLE_LAZY_INSTALLS':'1'}
    return [str(binary),'app-server','--stdio'],env,work,home

def death_signal():
    if sys.platform=='linux':
        ctypes.CDLL(None).prctl(1,signal.SIGKILL)
        if os.getppid()==1:os._exit(125)

def process_identity(pid):
    if sys.platform!='linux':return None
    try:return Path('/proc/'+str(pid)+'/stat').read_text().split(') ',1)[1].split()[19]
    except OSError:return None

class AppServer:
    def __init__(self,policy,jobroot):
        args,env,work,_=runtime(policy,jobroot)
        self.p=subprocess.Popen(args,cwd=work,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True,preexec_fn=death_signal if sys.platform=='linux' else None)
        self.identity=process_identity(self.p.pid)
        self.events=queue.Queue(maxsize=512);self.responses={};self.cv=threading.Condition();self.write_lock=threading.Lock();self.sequence=0;self.dead=False
        self.reader=threading.Thread(target=self._reader,daemon=True);self.reader.start()
        try:
            self.rpc('initialize',{'clientInfo':{'name':'ole_project_work','version':'0.1.0'},'capabilities':{'experimentalApi':False}},timeout=20)
            self.send({'method':'initialized'})
        except BaseException:
            self.close();raise
    def send(self,obj):
        try:
            with self.write_lock:self.p.stdin.write(json.dumps(obj,separators=(',',':'),ensure_ascii=False).encode()+b'\n');self.p.stdin.flush()
        except (OSError,ValueError):raise Unknown('appserver_transport_unknown')
    def _reader(self):
        try:
            while True:
                b=self.p.stdout.readline(16_000_001)
                if not b:break
                if len(b)>16_000_000 or not b.endswith(b'\n'):break
                obj=json.loads(b)
                if 'id' in obj and 'method' not in obj:
                    with self.cv:self.responses[obj['id']]=obj;self.cv.notify_all()
                elif 'id' in obj:
                    # Never approve host commands, permissions, external tools or user auth.
                    if obj['method'] in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
                        self.send({'id':obj['id'],'result':{'decision':'decline'}})
                    else:self.send({'id':obj['id'],'error':{'code':-32601,'message':'Disabled by project policy'}})
                elif obj.get('method') in ('turn/completed','error'):
                    try:self.events.put_nowait(obj)
                    except queue.Full:break
        except (OSError,ValueError,Fault):pass
        finally:
            with self.cv:self.dead=True;self.cv.notify_all()
    def rpc(self,method,params,timeout=20):
        self.sequence+=1;rid=self.sequence
        self.send({'id':rid,'method':method,'params':params});deadline=time.monotonic()+timeout
        with self.cv:
            while rid not in self.responses:
                remaining=deadline-time.monotonic()
                if self.dead or remaining<=0:raise Unknown('appserver_transport_unknown')
                self.cv.wait(min(remaining,1))
            obj=self.responses.pop(rid)
        if 'error' in obj:raise Rejected('appserver_request_rejected')
        return obj['result']
    def history(self,thread_id):
        t=self.rpc('thread/read',{'threadId':thread_id,'includeTurns':True})['thread']
        if t.get('historyMode')=='paginated':
            turns=[];cursor=None
            for _ in range(20):
                p=self.rpc('thread/turns/list',{'threadId':thread_id,'limit':50,'itemsView':'full','sortDirection':'asc','cursor':cursor})
                turns+=p['data'];cursor=p.get('nextCursor')
                if not cursor:break
            else:raise Fault('history_bound_exceeded')
            t['turns']=turns
        for turn in t.get('turns',[]):
            if turn.get('itemsView','full')!='full':raise Fault('full_history_required')
        return t
    def close(self):
        try:os.killpg(self.p.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        if self.p.poll() is None:
            try:self.p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:os.killpg(self.p.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                self.p.wait(timeout=3)
        try:os.killpg(self.p.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        for stream in (self.p.stdin,self.p.stdout):
            try:stream.close()
            except OSError:pass
