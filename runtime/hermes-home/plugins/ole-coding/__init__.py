import importlib.util,json
from pathlib import Path
spec=importlib.util.spec_from_file_location('ole_coding_worker',Path(__file__).with_name('worker.py'));worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
ns=importlib.util.spec_from_file_location('ole_coding_native',Path(__file__).with_name('native_worker.py'));native=importlib.util.module_from_spec(ns);ns.loader.exec_module(native)
worker.NATIVE=native
def register(ctx):
    ctx.register_tool('ole_coding_file','ole-coding-files',{'name':'ole_coding_file','description':'Restricted owned coding-job repository read/list/search/write, no shell or other paths.','parameters':{'type':'object','properties':{'job_id':{'type':'string'},'operation':{'type':'string','enum':['list','read','write','search']},'path':{'type':'string'},'content':{'type':'string'},'needle':{'type':'string'}},'required':['job_id','operation']}},native.operation)
    def home():
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    def start(args,**_):
        return json.dumps(worker.launch(args['repo'],args['brief'],args['verification_argv'],home(),args.get('timeout_seconds',600),args.get('read_only',False),args.get('progress',True)))
    def status(args,**_):return json.dumps(worker.status(args['job_id'],args.get('wait_seconds',0)))
    def cancel(args,**_):return json.dumps(worker.cancel(args['job_id']))
    schema={'name':'ole_coding_start','description':'Start explicit isolated Sol6.1medium coding job; no auto commit/push/merge. Native stream remains private, status gives verified receipt. Requires existing authorised clean repo and concrete verification argv.','parameters':{'type':'object','properties':{'repo':{'type':'string'},'brief':{'type':'string'},'verification_argv':{'type':'array','items':{'type':'string'}},'timeout_seconds':{'type':'integer'},'read_only':{'type':'boolean'},'progress':{'type':'boolean'}},'required':['repo','brief','verification_argv']}}
    ctx.register_tool('ole_coding_start','ole-coding',schema,start)
    for name,handler in [('ole_coding_status',status),('ole_coding_cancel',cancel)]:
        ctx.register_tool(name,'ole-coding',{'name':name,'description':'Get bounded safe worker progress/final verified receipt or cancel exact job process.','parameters':{'type':'object','properties':{'job_id':{'type':'string'},'wait_seconds':{'type':'integer'}},'required':['job_id']}},handler)
    ctx.register_command('codingjob',lambda raw:json.dumps(worker.status(raw.strip())),description='Show coding job progress and verified receipt')
