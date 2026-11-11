"""Native directory vector import; durable Jasper registry stays the authorization join."""
import json,subprocess,time
from pathlib import Path
from .manifest import load_manifest
from .scanner import scan_source
from .state import State
from .ole_policy import atomic
from .ole_search import lexical_refresh

def bulk(manifest: Path):
 m=load_manifest(manifest);source=next(s for s in m.sources if s.namespace=='ole_unconfirmed');state=State(m.state_db)
 scanned=scan_source(source);state.upsert_scan(scanned,source.id)
 report_path=m.state_db.parent/'context-bulk-job.json';root='viking://resources/ole_unconfirmed/context'
 existing=json.loads(report_path.read_text())if report_path.exists()else{}
 job=existing if existing else{'source_root_id':source.id,'namespace':source.namespace,'processing_mode':'vectors_only','documents':len(scanned),'started_at':time.time(),'status':'importing'}
 if not existing:
  atomic(report_path,json.dumps(job).encode())
  p=subprocess.run([m.ov_binary,'add-resource',str(source.root),'--to',root,'--processing-mode','vectors_only','--no-progress','-o','json'],capture_output=True,text=True,timeout=240)
  if p.returncode:job.update(status='failed',error='native directory import rejected');atomic(report_path,json.dumps(job).encode());raise RuntimeError(job['error'])
  accepted=json.loads(p.stdout).get('result',{});job['operation']=accepted.get('meta');job['root_uri']=accepted.get('root_uri',root);atomic(report_path,json.dumps(job).encode())
 p=subprocess.run([m.ov_binary,'wait','--timeout','1800','-o','json'],capture_output=True,text=True,timeout=1850)
 if p.returncode:job.update(status='waiting_or_failed',error='native completion not verified');atomic(report_path,json.dumps(job).encode());raise RuntimeError(job['error'])
 # Actual native tree proves every derivative file exists; health is never index readiness.
 listing=subprocess.run([m.ov_binary,'ls',root,'--recursive','--all','--node-limit','40000','-o','json'],capture_output=True,text=True,timeout=240)
 if listing.returncode:raise RuntimeError('Native resource tree not verified')
 entries=json.loads(listing.stdout).get('result',[])
 present={entry.get('uri')for entry in entries if not entry.get('isDir',True)}
 # OV Markdown parser may rename a frontmatter-title file. Resolve exact native bytes, never assume names.
 native_root=m.state_db.parent.parent/'ov-data/viking/default/resources/ole_unconfirmed/context'
 from .ole_policy import digest
 native_by_hash={}
 for f in native_root.rglob('*.md'):
  if not f.name.startswith('.'):
   native_by_hash.setdefault(digest(f.read_bytes()),[]).append('viking://resources/ole_unconfirmed/context/'+f.parent.name)
 mapping={};missing=[]
 for res in scanned:
  expected=root+'/'+Path(res.relative_path).stem
  if expected+'/'+Path(res.relative_path).name in present:mapping[res.source_id]=expected
  else:
   options=native_by_hash.get(res.sha256,[])
   if len(options)==1:mapping[res.source_id]=options[0]
   else:missing.append(res.source_id)
 if missing:
  job.update(status='incomplete',missing_resource_count=len(missing));atomic(report_path,json.dumps(job).encode());raise RuntimeError('Native resource tree incomplete')
 # Every native original must match the exact derivative version, including frontmatter rename cases.
 for res in scanned:
  if mapping[res.source_id]not in native_by_hash.get(res.sha256,[]):raise RuntimeError('Native source bytes mismatch')
 job['native_original_files_verified']=len(scanned);job['native_frontmatter_renames']=sum(mapping[r.source_id]!=root+'/'+Path(r.relative_path).stem for r in scanned)
 with state.connect()as c:
  for res in scanned:c.execute("UPDATE resources SET status='synced',ov_resource_id=?,last_synced=? WHERE source_id=? AND sha256=?",(mapping[res.source_id],time.time(),res.source_id,res.sha256))
 lexical_refresh(m);job.update(status='indexed_pending_recall_probe',finished_at=time.time());atomic(report_path,json.dumps(job).encode());return job
if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);a=p.parse_args();r=bulk(a.manifest);print(json.dumps({k:v for k,v in r.items()if k!='operation'}))
