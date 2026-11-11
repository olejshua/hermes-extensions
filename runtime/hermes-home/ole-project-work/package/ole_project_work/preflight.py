"""No-model protocol and actual tool-sandbox acceptance; no host mutation outside own staging."""
import json
import os
import subprocess
from .safety import Fault,private,read_file,write_private
from .transport import runtime,AppServer

def preflight(policy):
    root=private(policy['workspace_root']);r=private(root/'preflight');private(r/'sources');work=private(r/'work')
    sentinel=r/'operator-sentinel';write_private(sentinel,'operator-only')
    source_sentinel=r/'sources'/'source-sentinel';write_private(source_sentinel,'source-only')
    args,env,work,home=runtime(policy,r)
    code='''from pathlib import Path
import socket
Path("sandbox-writable-probe").write_text("ok")
for p in (OPERATOR,AUTH):
 try:Path(p).read_text()
 except OSError:pass
 else:raise AssertionError("private host read allowed")
try:Path(OPERATOR).write_text("bad")
except OSError:pass
else:pass # Hidden host paths may be writable ephemeral mounts; host readback below is authoritative.
try:Path(SOURCE).write_text("bad")
except OSError:pass
else:raise AssertionError("protected source write allowed")
try:
 s=socket.socket();s.settimeout(2);s.connect(("1.1.1.1",443))
except OSError:pass
else:raise AssertionError("tool network allowed")
'''.replace('OPERATOR',repr(str(sentinel))).replace('AUTH',repr(policy['codex_auth_file'])).replace('SOURCE',repr(str(source_sentinel)))
    probe=subprocess.run([args[0],'sandbox','-P','hermes-project','/usr/bin/python3','-c',code],cwd=work,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=35)
    if probe.returncode or read_file(sentinel)!=b'operator-only' or read_file(source_sentinel)!=b'source-only':raise Fault('sandbox_preflight_failed')
    t=AppServer(policy,r)
    try:
        t.rpc('thread/list',{'limit':1})
        native=t.rpc('command/exec',{'command':['/usr/bin/python3','-c',code],'cwd':str(work),'timeoutMs':30000,'outputBytesCap':4000},timeout=35)
        if native['exitCode'] or read_file(sentinel)!=b'operator-only' or read_file(source_sentinel)!=b'source-only':raise Fault('native_appserver_sandbox_failed')
        # Starting a thread does not start a turn or invoke a model.
        result=t.rpc('thread/start',{'cwd':str(work),'model':'gpt-6.1-sol','approvalPolicy':'never','ephemeral':True})
        if not result.get('thread',{}).get('id'):raise Fault('protocol_preflight_failed')
    finally:t.close()
    proof={'ok':True,'model_calls':0,'private_stdio':True,'initialize_ack':True,'thread_start_ack':True,'native_command_exec_sandbox':True,'workspace_write':True,'host_read_denied':True,'host_write_isolated':True,'source_write_denied':True,'direct_network_denied':True}
    write_private(r/'proof.json',json.dumps(proof,indent=2));return proof
