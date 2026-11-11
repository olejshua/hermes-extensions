"""Recover an existing native result without add-resource, wait, or model dispatch."""
import argparse,fcntl,json,sqlite3,time
from contextlib import ExitStack
from pathlib import Path
from .deep_queue import current_source,exact_native,native_drained,native_recall
from .ole_policy import atomic,digest


def recover(root,client,authorization,job_id,apply=False):
 root=Path(root);control=json.loads(Path(authorization).read_text())
 budget=Path(control['budget_control']);registry_path=Path(control['state_db'])
 # Same lock order as the runner; no recovery may race a dispatch or source sync.
 with ExitStack()as stack:
  for path in (root/'runner.lock',registry_path.parent/'native-provider.lock',budget.parent/'budget.lock'):
   lock=stack.enter_context(path.open('a'))
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   except BlockingIOError:raise RuntimeError('Native recovery owner busy')
  value=json.loads(budget.read_text());measured=json.loads((budget.parent/'measured.json').read_text())
  if value.get('paused')is not True:raise RuntimeError('Recovery requires paused provider budget')
  if value.get('active_job_id')!=job_id:raise RuntimeError('Recovery must retain exact active job binding')
  if measured.get('unsettled_reservations'):raise RuntimeError('Physical reservations remain unsettled')
  if not native_drained():raise RuntimeError('Native queue is not proven healthy and drained')
  c=stack.enter_context(sqlite3.connect('file:'+str(root/'jobs.sqlite3')+'?mode=ro',uri=True));c.row_factory=sqlite3.Row
  row=c.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
  if not row or row['status']not in ('dispatched','paused_review_required','indexed_verified'):raise RuntimeError('Job is not recoverable')
  current=current_source(control,row['original_path'],allow_stale=False)
  if not current or current['sha256']!=row['source_version']:raise RuntimeError('Fresh source grant/version proof unavailable')
  input_path=(root/row['input_path']).resolve()
  if not input_path.is_relative_to((root/'originals').resolve()):raise RuntimeError('Recovery input escapes originals')
  if digest(input_path.read_bytes())!=row['input_sha']:raise RuntimeError('Recovery input SHA changed')
  uri=row['native_uri'];base='viking://resources/ole_unconfirmed/deep/'+job_id
  if not uri or not(uri==base or uri.startswith(base+'/')):raise RuntimeError('Recovery native target does not match job')
  if not exact_native(client,uri,input_path):raise RuntimeError('Native L2 does not cover exact original')
  if not native_recall(client,uri,input_path):raise RuntimeError('Native source-filtered L2 recall failed')
  fresh=current_source(json.loads(Path(authorization).read_text()),row['original_path'],allow_stale=False)
  if not fresh or fresh['sha256']!=row['source_version'] or digest(input_path.read_bytes())!=row['input_sha']:raise RuntimeError('Source changed during recovery proof')
  if not native_drained():raise RuntimeError('Native queue changed during recovery proof')
  proof={'job_id':job_id,'source_version':row['source_version'],'input_sha':row['input_sha'],'native_uri':uri,'source_current':True,'grant_verified':True,'l2_complete':True,'recall_verified':True,'native_drained':True,'checked_at':time.time(),'applied':apply,'paused':True,'provider_dispatches':0}
  if apply:
   # Registry first, then job, then binding. A crash leaves a bound, paused job that
   # can be rechecked; never clears recovery ahead of durable publication.
   with sqlite3.connect(registry_path)as registry:
    registry.execute('CREATE TABLE IF NOT EXISTS deep_resources(job_id TEXT PRIMARY KEY,original_path TEXT,source_version TEXT,native_uri TEXT,input_path TEXT,input_sha TEXT,namespace TEXT,status TEXT)')
    registry.execute('INSERT OR REPLACE INTO deep_resources VALUES(?,?,?,?,?,?,?,?)',(job_id,row['original_path'],row['source_version'],uri,str(input_path),row['input_sha'],'ole_unconfirmed','indexed_verified'))
   with sqlite3.connect(root/'jobs.sqlite3')as jobs:jobs.execute("UPDATE jobs SET status='indexed_verified',updated=? WHERE job_id=?",(time.time(),job_id))
   atomic(root/'recovery-proof.json',json.dumps(proof).encode())
   value.update(active_job_id=None,recovery_required=False,pause_reason='recovered existing native result; operator pause retained')
   atomic(budget,json.dumps(value).encode())
  return proof

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--client',required=True);p.add_argument('--authorization',type=Path,required=True);p.add_argument('--job-id',required=True);p.add_argument('--apply',action='store_true');a=p.parse_args();print(json.dumps(recover(a.root,a.client,a.authorization,a.job_id,a.apply)))
