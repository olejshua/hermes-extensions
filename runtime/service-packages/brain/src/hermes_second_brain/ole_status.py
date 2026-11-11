"""Separate service health, receipt freshness and actually proved retrieval readiness."""
import json,time
from pathlib import Path
from .manifest import load_manifest
from .state import State

def runner_lock_held(path):
 # Observe Linux flock metadata without briefly owning the runner lock ourselves.
 import os
 try:
  stat=path.stat();target=(os.major(stat.st_dev),os.minor(stat.st_dev),stat.st_ino)
  for line in Path('/proc/locks').read_text().splitlines():
   fields=line.split()
   if len(fields)<6 or fields[1]!='FLOCK':continue
   values=fields[5].split(':')
   if len(values)==3 and (int(values[0],16),int(values[1],16),int(values[2]))==target:return True
  return False
 except (OSError,ValueError):return None

def deep_liveness(root,control,states,pending_stale=0):
 held=runner_lock_held(root/'deep-queue/runner.lock')
 pending=states.get('pending',0)
 if control.get('recovery_required'):phase='recovery_required';action='verify_and_recover_bound_job'
 elif pending_stale:phase='stale_pending_versions';action='replace_selected_source_versions'
 elif control.get('paused')is True:phase='paused';action='explicit_resume_then_start_existing_service'if pending else'none_pending'
 elif held is True:phase='owner_active';action='observe_completion_or_pause'
 elif pending:phase='idle_pending';action='explicit_start_existing_service'
 else:phase='idle';action='none_pending'
 return {'phase':phase,'runner_lock_held':held,'next_action':action,'automatic_resume':False,'claim_limit':'Runner ownership is not proof of provider activity; paused/idle is not a running Deepservice.'}

def status(manifest,receipts,readiness):
 m=load_manifest(manifest);s=State(m.state_db)
 with s.connect()as c:counts=dict(c.execute('SELECT status,COUNT(*) FROM resources GROUP BY status'))
 packets={source.id:json.loads((source.approval_receipts or receipts).read_text())for source in m.sources};expected={};originals={};ages={}
 for sid,packet in packets.items():
  ages[sid]=max(0,time.time()-packet['generated_at'])
  for key,rec in packet['records'].items():expected[sid+'/'+key]=rec.get('export_sha256',rec['sha256']);originals[rec['original_path']]=rec['status']
 probe=json.loads(readiness.read_text())if readiness.is_file()else{'passed':False,'reason':'No actual full recall proof'}
 ready=bool(probe.get('passed')and probe.get('source_hashes')==expected and not counts.get('pending',0)and not counts.get('failed',0))
 root=m.state_db.parent.parent;deep={};budget={};control={};versions={}
 jobs=root/'deep-queue/jobs.sqlite3'
 if jobs.is_file():
  import sqlite3
  with sqlite3.connect('file:'+str(jobs)+'?mode=ro',uri=True)as c:
   deep=dict(c.execute('SELECT status,COUNT(*)FROM jobs GROUP BY status'))
   source_versions={rec['original_path']:rec['sha256']for packet in packets.values()for rec in packet['records'].values()}
   for original,version,state in c.execute('SELECT original_path,source_version,status FROM jobs'):
    key=state+(':'+ 'current'if source_versions.get(original)==version else':version_changed');versions[key]=versions.get(key,0)+1
 for file,key in [(root/'state/deep-budget/measured.json','measured'),(root/'state/deep-budget/control.json','control')]:
  if file.is_file():budget[key]=json.loads(file.read_text())
 native_path=root/'state/native-refresh-status.json';native=json.loads(native_path.read_text())if native_path.exists()else None
 return {'native_refresh':native,'deep_job_states':deep,'deep_current_version_counts':versions,'deep_liveness':deep_liveness(root,budget.get('control',{}),deep,versions.get('pending:version_changed',0)),'deep_budget':budget,'subscription_quota_available':budget.get('measured',{}).get('subscription_quota_available',False),'provider_measurement':'physical calls and explicit known usage; unavailable account quota is unknown','source_last_refresh':{sid:packet['generated_at']for sid,packet in packets.items()},'source_counts':counts,'original_documents':len(originals),'derivative_resources':len(expected),'status_document_counts':{status:list(originals.values()).count(status)for status in set(originals.values())},'source_age_seconds':ages,'fresh_for_current':all(age<=1800 for age in ages.values()),'index_recall_ready':ready,'dreaming_enabled':False,'mail_collection_enabled':False,'source_writes_enabled':False}
if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--receipts',type=Path,required=True);p.add_argument('--readiness',type=Path,required=True);a=p.parse_args();print(json.dumps(status(a.manifest,a.receipts,a.readiness)))
