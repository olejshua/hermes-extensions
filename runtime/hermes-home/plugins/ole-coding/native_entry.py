"""Private stdin job packet; only typed content-free events go to stdout."""
import contextlib,importlib.util,json,os,sys,time,threading
from pathlib import Path
out=sys.stdout
packet=json.load(sys.stdin)
job={**packet,'work':Path(packet['work']),'cancel':threading.Event()}
def emit(job,event):out.write(json.dumps({'type':'ole.native','event':event})+'\n');out.flush()
try:
 with open(os.devnull,'w') as null,contextlib.redirect_stdout(null),contextlib.redirect_stderr(null):
  runtime=packet.pop('runtime')
  spec=importlib.util.spec_from_file_location('native',Path(__file__).with_name('native_worker.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  sys.modules['ole_native_worker_runtime']=m
  plug=Path(packet['home'])/'plugins/ole-coding-files';plug.mkdir(parents=True,mode=0o700)
  (plug/'plugin.yaml').write_text('name: ole-coding-files\nversion: 0.1.0\ndescription: Private own coding file tools\n')
  schema={'name':'ole_coding_file','description':'Own isolated codingjob file list/read/write/literal search. No shell, external paths or git metadata.','parameters':{'type':'object','properties':{'job_id':{'type':'string'},'operation':{'type':'string','enum':['list','read','write','search']},'path':{'type':'string'},'content':{'type':'string'},'needle':{'type':'string'}},'required':['job_id','operation']}}
  (plug/'__init__.py').write_text("import ole_native_worker_runtime as m\ndef register(ctx):\n    ctx.register_tool('ole_coding_file','ole-coding-files',"+repr(schema)+",m.operation)\n")
  telemetry_spec=importlib.util.spec_from_file_location('ole_native_telemetry',Path(__file__).with_name('telemetry.py'));telemetry=importlib.util.module_from_spec(telemetry_spec);telemetry_spec.loader.exec_module(telemetry)
  sys.modules['ole_native_telemetry']=telemetry
  telemetry._packet_job=job;telemetry._emit=emit;telemetry._spool=packet['spool']
  tplug=Path(packet['home'])/'plugins/ole-native-telemetry';tplug.mkdir(mode=0o700)
  (tplug/'plugin.yaml').write_text('name: ole-native-telemetry\nversion: 0.1.0\ndescription: Native worker telemetry\n')
  (tplug/'__init__.py').write_text('import ole_native_telemetry as t\ndef register(ctx):\n    t.register_native(ctx,t._packet_job,t._emit,t._spool)\n')
  (Path(packet['home'])/'config.yaml').write_text('plugins:\n  enabled: [ole-coding-files, ole-native-telemetry]\nmodel:\n  default: gpt-6.1-sol\n  provider: openai-codex\nagent:\n  reasoning_effort: medium\ncompression:\n  enabled: false\n')
  from hermes_cli.plugins import discover_plugins,get_plugin_manager
  discover_plugins(force=True)
  m.run_native(job,emit,runtime)
 emit(job,{'type':'native.receipt','status':'completed','at':time.time()})
except BaseException as e:
 emit(job,{'type':'native.receipt','status':'failed','error_type':type(e).__name__,'missing_module':e.name if isinstance(e,ModuleNotFoundError) else None,'at':time.time()})
 sys.exit(1)
