"""Original-document native hierarchy jobs. Explicit run authorization required; no auto-start."""
import argparse,json,sqlite3,time,subprocess
from pathlib import Path
from .ole_policy import atomic,digest
from .vault_context_export import text_of,SECRET

def prepare(receipt_paths,root):
 root.mkdir(parents=True,exist_ok=True,mode=0o700);db=root/'jobs.sqlite3'
 if not db.exists():atomic(db,b'')
 c=sqlite3.connect(db)
 c.execute('CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, original_path TEXT, source_version TEXT, status TEXT, source_status TEXT, input_path TEXT, native_uri TEXT, attempts INTEGER DEFAULT 0, updated REAL)')
 if 'input_sha'not in {r[1]for r in c.execute('PRAGMA table_info(jobs)')}:c.execute('ALTER TABLE jobs ADD COLUMN input_sha TEXT')
 docs={}
 for path in receipt_paths:
  for rec in json.loads(path.read_text())['records'].values():docs.setdefault(rec['original_path'],rec)
 total_bytes=total_tokens=sections=0
 from tokenizers import Tokenizer
 tokenizer=Tokenizer.from_file('/path/to/operator/Codex Generell/hermes-audit/brain-local-model/tokenizer.json');tokenizer.no_truncation()
 for original,record in docs.items():
  p=Path(original);raw=p.read_bytes()
  if digest(raw)!=record['sha256']:raise RuntimeError('Deep job source changed; refresh required')
  text=text_of(p,raw)
  if SECRET.search(text)or p.read_bytes()!=raw:raise RuntimeError('Deep job input rejected')
  job=digest((original+'\0'+record['sha256']).encode());input_path=root/'originals'/(job+'.md');atomic(input_path,text.encode())
  c.execute('INSERT OR IGNORE INTO jobs(job_id,original_path,source_version,status,source_status,input_path,updated)VALUES(?,?,?,?,?,?,?)',(job,original,record['sha256'],'pending',record['status'],'originals/'+job+'.md',time.time()))
  c.execute('UPDATE jobs SET input_sha=? WHERE job_id=?',(digest(text.encode()),job))
  total_bytes+=len(text.encode());total_tokens+=len(tokenizer.encode(text,add_special_tokens=False).ids);sections+=sum(line.startswith('#')for line in text.splitlines())
 active={digest((original+'\0'+record['sha256']).encode())for original,record in docs.items()}
 for(job,)in c.execute('SELECT job_id FROM jobs'):
  if job not in active:c.execute("UPDATE jobs SET status='revoked',updated=? WHERE job_id=?",(time.time(),job))
 c.commit();c.close()
 plan={'documents':len(docs),'parsed_bytes':total_bytes,'exact_tokenizer_input_tokens':total_tokens,'markdown_heading_count':sections,'processing_mode':'semantic_and_vectors','concurrency':1,'native_original_jobs':len(docs),'mass_model_start_authorized':False,'observed_15_source_physical_calls':80,'observed_15_source_prompt_tokens':76819,'observed_15_source_output_tokens':66710,'linear_estimate_calls':round(len(docs)*80/15),'linear_estimate_prompt_tokens':round(len(docs)*76819/15),'linear_estimate_output_tokens':round(len(docs)*66710/15),'estimate_limit':'Document structure and long transcripts differ strongly; projection is not a quota guarantee','existing_approved_native_documents':15,'pending_deep_documents':len(docs)-15,'remaining_linear_estimate_calls':round((len(docs)-15)*80/15),'requested_physical_call_cap':4000,'pause_on_quota_error':True,'source_instructions_authoritative':False}
 atomic(root/'plan.json',json.dumps(plan,indent=2).encode());return plan

def exact_partition(original,segments):
 # Native Markdown parser may split headings and omit separator whitespace.
 # Every preserved L2 segment must be an exact substring; no missing content allowed.
 covered=bytearray(len(original))
 for segment in segments:
  if not segment:return False
  start=original.find(segment)
  if start<0:return False
  while start>=0:
   covered[start:start+len(segment)]=b'\x01'*len(segment)
   start=original.find(segment,start+1)
 return bool(segments)and all(mark or char.isspace()for char,mark in zip(original,covered))

def exact_native(client,uri,input_path):
 listing=subprocess.run([client,'ls',uri,'--recursive','--all','--node-limit','20000','-o','json'],capture_output=True,text=True,timeout=45)
 if listing.returncode:return False
 entries=json.loads(listing.stdout).get('result',[])
 if not any(not item.get('isDir',True)for item in entries):return False
 # Native index must preserve the exact original input; generated summaries alone cannot satisfy this.
 original=input_path.read_text();segments=[]
 for item in entries:
  if item.get('isDir',True)or not item.get('uri','').endswith('.md')or item.get('uri','').rsplit('/',1)[-1].startswith('.'):continue
  content=subprocess.run([client,'read',item['uri'],'-o','json'],capture_output=True,text=True,timeout=45)
  if content.returncode:continue
  payload=json.loads(content.stdout).get('result',{})
  text=payload.get('content',payload.get('text',''))if isinstance(payload,dict)else payload
  if isinstance(text,str):
   if digest(text.encode())==digest(input_path.read_bytes()):return True
   segments.append(text)
 return exact_partition(original,segments)

def native_recall(client,uri,input_path):
 query=' '.join(__import__('re').findall(r'\w{4,}',input_path.read_text())[:12])
 if not query:return False
 probe=subprocess.run([client,'find',query,'--uri',uri,'-L','2','--limit','10','-o','json'],capture_output=True,text=True,timeout=45)
 hits=json.loads(probe.stdout).get('result',{}).get('resources',[])if probe.returncode==0 else[]
 return any(hit.get('uri','').startswith(uri+'/')or hit.get('uri')==uri for hit in hits)

def queue_drained(payload):
 # Observer renders a table; resolve columns by name, never count Processed as running.
 try:
  if payload.get('status')!='ok':return False
  result=payload['result']
  if result.get('is_healthy')is not True or result.get('has_errors')is not False:return False
  header=None;seen=False
  for line in result['status'].splitlines():
   values=[part.strip()for part in line.split('|')[1:-1]]
   if 'Queue'in values:
    header={name:values.index(name)for name in ('Queue','Pending','In Progress','Errors')}
    continue
   if not header or not values:continue
   if len(values)<=max(header.values()):return False
   numbers=[values[header[name]]for name in ('Pending','In Progress','Errors')]
   if not all(number.isdigit()for number in numbers):return False
   seen=True
   if any(int(number)for number in numbers):return False
  return seen
 except (KeyError,TypeError,ValueError,AttributeError):return False

def native_drained():
 import urllib.request
 try:
  with urllib.request.urlopen('http://127.0.0.1:1933/api/v1/observer/queue',timeout=15)as response:return queue_drained(json.load(response))
 except Exception:return False

def run(root,client,authorization):
 import fcntl
 root.mkdir(parents=True,exist_ok=True,mode=0o700)
 with (root/'runner.lock').open('a')as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:raise RuntimeError('Native deep runner already owned')
  return _run_locked(root,client,authorization)

def verify_adapter(stamp):
 import os
 if stamp.get('protocol')!='ole-openviking-0.4.16-v4-reserved':raise RuntimeError('Live native adapter protocol not budget-governed')
 os.kill(stamp['pid'],0)
 command=Path('/proc/'+str(stamp['pid'])+'/cmdline').read_bytes()
 if b'hermes_second_brain.ov_bootstrap'not in command:raise RuntimeError('Adapter stamp is not running native service')
 module=Path(__file__).parent
 if digest((module/'ov_bootstrap.py').read_bytes())!=stamp['adapter_sha256']or digest((module/'deep_budget.py').read_bytes())!=stamp['budget_sha256']:raise RuntimeError('Restart native service after adapter code change')

def current_source(control,original,allow_stale=True):
 from .manifest import load_manifest
 from .ole_policy import verified_record
 grants=json.loads(Path(control['source_grants']).read_text())
 active={g['id']:g for g in grants.get('grants',[])if g.get('active')is True}
 manifest=load_manifest(Path(control['manifest']))
 for source in manifest.sources:
  grant_id='remaining-vault-context-2026-10-08'if source.namespace=='ole_unconfirmed'else'approved-documents-2026-10-08'if source.namespace=='ole-approved'else''
  if grant_id not in active or not source.root.is_dir()or not source.approval_receipts:continue
  packet=json.loads(source.approval_receipts.read_text())
  for key,record in packet['records'].items():
   if record['original_path']==original:return verified_record(source.approval_receipts,key,allow_stale=allow_stale,accepted_statuses=source.accepted_statuses,packet=packet)
 return None

def _run_locked(root,client,authorization):
 grant=json.loads(authorization.read_text())
 if grant.get('mass_model_start_authorized')is not True:raise RuntimeError('Mass native VLM jobs require explicit approved budget grant')
 db=root/'jobs.sqlite3';c=sqlite3.connect(db);c.row_factory=sqlite3.Row
 stamp=json.loads(Path(grant['adapter_stamp']).read_text())
 verify_adapter(stamp)
 registry=sqlite3.connect(grant['state_db']);registry.execute('CREATE TABLE IF NOT EXISTS deep_resources(job_id TEXT PRIMARY KEY,original_path TEXT,source_version TEXT,native_uri TEXT,input_path TEXT,input_sha TEXT,namespace TEXT,status TEXT)')
 completed_this_run=0
 for row in c.execute("SELECT * FROM jobs WHERE status IN ('pending','dispatched') ORDER BY updated").fetchall():
  if completed_this_run>=grant.get('max_jobs',100000):break
  if grant.get('allowed_job_ids')and row['job_id']not in grant['allowed_job_ids']:continue
  control=json.loads(authorization.read_text());budget=Path(control['budget_control'])
  if control.get('paused')or not control.get('mass_model_start_authorized')or(budget.exists()and json.loads(budget.read_text()).get('paused')):break
  current=current_source(control,row['original_path'])
  if not current or current['sha256']!=row['source_version']:
   c.execute("UPDATE jobs SET status='revoked',updated=? WHERE job_id=?",(time.time(),row['job_id']));registry.execute("UPDATE deep_resources SET status='revoked' WHERE job_id=?",(row['job_id'],));c.commit();registry.commit();continue
  # Approved15 have already native hierarchy; only remaining653 original jobs need deep generation.
  if current['status']=='confirmed'and current.get('role')in('confirmed_note','explicit_policy','explicit_reference'):
   evidence=row['job_id']in control.get('existing_approved_job_ids',[])
   state='existing_approved_native'if evidence else'waiting_source_sync'
   c.execute('UPDATE jobs SET status=?,updated=?WHERE job_id=?',(state,time.time(),row['job_id']));c.commit();continue
  import fcntl
  from .deep_budget import set_active
  provider_lock=Path(control['state_db']).parent/'native-provider.lock'
  with provider_lock.open('a')as native_owner:
   fcntl.flock(native_owner,fcntl.LOCK_EX);set_active(row['job_id'])
   dispatch_started=False;quiescent=False
   try:
    uri=row['native_uri']or'viking://resources/ole_unconfirmed/deep/'+row['job_id'];input_path=root/row['input_path'];input_sha=digest(input_path.read_bytes())
    if input_sha!=row['input_sha']:
     c.execute("UPDATE jobs SET status='revoked' WHERE job_id=?",(row['job_id'],));c.commit();continue
    c.execute("UPDATE jobs SET status='dispatched',native_uri=?,attempts=attempts+1,updated=? WHERE job_id=?",(uri,time.time(),row['job_id']));c.commit()
    args=[client,'add-resource',str(input_path),'--to',uri,'--processing-mode','semantic_and_vectors','--wait','--no-progress','-o','json']if row['status']=='pending'else[client,'wait','--timeout','1800','-o','json']
    dispatch_started=True
    result=subprocess.run(args,text=True,capture_output=True,timeout=1850);state='paused_review_required';actual_uri=uri
    if result.returncode==0:
     payload=json.loads(result.stdout).get('result',{});actual_uri=payload.get('root_uri',uri)if isinstance(payload,dict)else uri
     if actual_uri!=uri and not actual_uri.startswith(uri+'/'):raise RuntimeError('Unexpected native deep target')
     if exact_native(client,actual_uri,input_path):
      if native_recall(client,actual_uri,input_path):state='indexed_verified'
    # Re-read current consent/version after the long native operation; never publish stale completion.
    fresh=current_source(json.loads(authorization.read_text()),row['original_path'])
    if not fresh or fresh['sha256']!=row['source_version']:state='revoked'
    if digest(input_path.read_bytes())!=input_sha:state='revoked'
    c.execute('UPDATE jobs SET status=?,native_uri=?,updated=? WHERE job_id=?',(state,actual_uri,time.time(),row['job_id']));c.commit()
    if state=='indexed_verified':registry.execute('INSERT OR REPLACE INTO deep_resources VALUES(?,?,?,?,?,?,?,?)',(row['job_id'],row['original_path'],row['source_version'],actual_uri,str(input_path),input_sha,'ole_unconfirmed','indexed_verified'));registry.commit()
    if state=='indexed_verified':completed_this_run+=1
    if state=='paused_review_required':break
   finally:
    quiescent=(not dispatch_started)or native_drained()
    if quiescent:set_active(None)
    else:
     from .deep_budget import require_recovery
     require_recovery('native operation not proven drained; retain active job binding')
 registry.close();c.close()
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--receipts',type=Path,action='append');p.add_argument('--run',action='store_true');p.add_argument('--authorization',type=Path);p.add_argument('--client');a=p.parse_args()
 if a.run:run(a.root,a.client,a.authorization)
 else:print(json.dumps(prepare(a.receipts,a.root)))
