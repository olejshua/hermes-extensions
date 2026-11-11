"""Replace selected stale pending Markdown versions; no model or broad source reads."""
import fcntl,json,sqlite3,time
from contextlib import ExitStack
from pathlib import Path
from .deep_queue import current_source,exact_native,native_drained,native_recall
from .ole_policy import atomic,digest
from .vault_context_export import SECRET


def replace_versions(root,client,authorization,inputs,apply=False):
 root=Path(root);control=json.loads(Path(authorization).read_text());budget=Path(control['budget_control'])
 if not inputs or len({item['old_job_id']for item in inputs})!=len(inputs):raise RuntimeError('Empty or duplicate replacement selection')
 with ExitStack()as stack:
  for path in (root/'runner.lock',Path(control['state_db']).parent/'native-provider.lock',budget.parent/'budget.lock'):
   lock=stack.enter_context(path.open('a'));fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  value=json.loads(budget.read_text());measured=json.loads((budget.parent/'measured.json').read_text())
  if value.get('paused')is not True or value.get('active_job_id')or value.get('recovery_required')or measured.get('unsettled_reservations'):raise RuntimeError('Version repair requires recovered and paused budget')
  if not native_drained():raise RuntimeError('Native queue not proven drained')
  c=stack.enter_context(sqlite3.connect(root/'jobs.sqlite3'));c.row_factory=sqlite3.Row;prepared=[]
  for item in inputs:
   row=c.execute('SELECT * FROM jobs WHERE job_id=?',(item['old_job_id'],)).fetchone()
   if not row or row['status']not in ('pending','revoked'):raise RuntimeError('Only selected stale pending jobs may be replaced')
   if row['original_path']!=item['original_path']or row['source_version']!=item['old_source_version']:raise RuntimeError('Replacement identity changed')
   current=current_source(control,row['original_path'],allow_stale=False)
   if not current or current['sha256']!=item['new_source_version']or current['sha256']==row['source_version']:raise RuntimeError('Fresh changed source proof unavailable')
   if Path(row['original_path']).suffix.lower()!='.md':raise RuntimeError('Exact raw Markdown replacement only')
   raw=item['input_bytes'];text=raw.decode('utf-8')
   if digest(raw)!=current['sha256']or SECRET.search(text):raise RuntimeError('Replacement original SHA or secret check failed')
   job_id=digest((row['original_path']+'\0'+current['sha256']).encode());uri='viking://resources/ole_unconfirmed/deep/'+job_id
   existing=c.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
   if existing:
    if existing['original_path']!=row['original_path']or existing['source_version']!=current['sha256']or existing['input_sha']!=digest(raw):raise RuntimeError('Existing replacement conflicts')
    if existing['status']not in ('pending','indexed_verified'):raise RuntimeError('Existing replacement not safely reusable')
    if digest((root/existing['input_path']).read_bytes())!=digest(raw):raise RuntimeError('Existing replacement input changed')
   if digest(Path(item['input_path']).read_bytes())!=digest(raw):raise RuntimeError('Staged replacement input changed')
   state=existing['status']if existing else'pending'
   # Only exact native original version can avoid a future hierarchy dispatch.
   if exact_native(client,uri,Path(item['input_path'])):
    if not native_recall(client,uri,Path(item['input_path'])):raise RuntimeError('Existing native replacement recall failed')
    state='indexed_verified'
   elif state=='indexed_verified':raise RuntimeError('Verified replacement lost native original')
   prepared.append((row,current,job_id,uri,state,raw))
  for row,current,*_ in prepared:
   fresh=current_source(json.loads(Path(authorization).read_text()),row['original_path'],allow_stale=False)
   if not fresh or fresh['sha256']!=current['sha256']:raise RuntimeError('Source changed during version repair')
  if not native_drained():raise RuntimeError('Native queue changed during version repair')
  if apply:
   for row,current,job_id,uri,state,raw in prepared:atomic(root/'originals'/(job_id+'.md'),raw)
   with sqlite3.connect(control['state_db'])as registry:
    registry.execute('CREATE TABLE IF NOT EXISTS deep_resources(job_id TEXT PRIMARY KEY,original_path TEXT,source_version TEXT,native_uri TEXT,input_path TEXT,input_sha TEXT,namespace TEXT,status TEXT)')
    for row,current,job_id,uri,state,raw in prepared:
     if state=='indexed_verified':registry.execute('INSERT OR REPLACE INTO deep_resources VALUES(?,?,?,?,?,?,?,?)',(job_id,row['original_path'],current['sha256'],uri,str(root/'originals'/(job_id+'.md')),digest(raw),'ole_unconfirmed',state))
   c.execute('BEGIN IMMEDIATE')
   try:
    for row,current,job_id,uri,state,raw in prepared:
     fresh=current_source(json.loads(Path(authorization).read_text()),row['original_path'],allow_stale=False)
     if not fresh or fresh['sha256']!=current['sha256']:raise RuntimeError('Source changed before queue commit')
     live=c.execute('SELECT status,source_version FROM jobs WHERE job_id=?',(row['job_id'],)).fetchone()
     if not live or live['status']not in ('pending','revoked')or live['source_version']!=row['source_version']:raise RuntimeError('Old job changed before queue commit')
     c.execute("INSERT INTO jobs(job_id,original_path,source_version,status,source_status,input_path,native_uri,attempts,updated,input_sha)VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,native_uri=excluded.native_uri,updated=excluded.updated WHERE jobs.status='pending'",(job_id,row['original_path'],current['sha256'],state,current['status'],'originals/'+job_id+'.md',uri,0,time.time(),digest(raw)))
     c.execute("UPDATE jobs SET status='revoked',updated=? WHERE job_id=?",(time.time(),row['job_id']))
    c.commit()
   except Exception:c.rollback();raise
  result={'checked_at':time.time(),'applied':apply,'old_jobs_revoked':len(prepared)if apply else 0,'new_pending':sum(p[4]=='pending'for p in prepared),'native_deduplicated':sum(p[4]=='indexed_verified'for p in prepared),'provider_dispatches':0,'budget_changed':False,'jobs':[{'old_job_id':p[0]['job_id'],'new_job_id':p[2],'source_version':p[1]['sha256'],'input_sha':digest(p[5]),'state':p[4]}for p in prepared]}
  if apply:atomic(root/'version-repair-proof.json',json.dumps(result,indent=2).encode())
  return result
