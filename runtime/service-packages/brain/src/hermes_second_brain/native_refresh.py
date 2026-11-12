"""Bounded exact-original vector refresh; never generate or rewrite source claims."""
import argparse,fcntl,json,sqlite3,subprocess,time
from pathlib import Path
from .deep_queue import exact_native,native_drained,native_recall
from .ids import source_id
from .manifest import load_manifest
from .ole_policy import atomic,digest,verified_record
from .ole_search import lexical_refresh,search
from .ole_verify import version_checks
from .state import _receipt

GRANTS={'ole-approved':'approved-documents-2026-10-08','ole_unconfirmed':'remaining-vault-context-2026-10-08'}


def grant_sources(manifest):
 # Sources outside the export-grant model (e.g. namespace 'dreams') carry
 # their own approval proof - publication receipts verified by dream_sync.
 # This refresh must neither fail on their missing export grants nor index
 # them here; their own verified pipeline owns them.
 return [source for source in manifest.sources if source.namespace in GRANTS]


def grant_source_ids(manifest):
 return {source.id for source in grant_sources(manifest)}


def failure_result(exc):
 # Only code-authored exact literals become diagnostic codes. Never render str(exc):
 # native TimeoutExpired/SDK messages can contain private query/request material.
 codes={'Source grant or export unavailable':'snapshot_grant_or_export_unavailable',
        'Source receipt unavailable or expired':'snapshot_receipt_not_current',
        'Export path outside source':'snapshot_export_path_outside_root'}
 reason=exc.args[0] if type(exc)is RuntimeError and len(exc.args)==1 and type(exc.args[0])is str else None
 return {'phase':'failed_review_required','error_type':type(exc).__name__,
         'error_code':codes.get(reason,'refresh_failure_unclassified'),
         'provider_dispatches':0,'model_dispatches':0}


def snapshot(manifest,grants):
 active={g['id']for g in json.loads(Path(grants).read_text()).get('grants',[])if g.get('active')is True}
 packets={};records={};expected={}
 for source in grant_sources(manifest):
  if GRANTS.get(source.namespace)not in active or not source.root.is_dir()or not source.approval_receipts:raise RuntimeError('Source grant or export unavailable')
  packet=json.loads(source.approval_receipts.read_text());age=time.time()-packet['generated_at']
  if packet.get('version')!=1 or not -60<=age<=1800:raise RuntimeError('Source receipt unavailable or expired')
  packets[source.id]=packet
  for key,record in packet['records'].items():
   raw_path=source.approval_receipts.parent/'documents'/record['export_path'];path=raw_path.resolve()
   if not path.is_relative_to(source.root.resolve())or raw_path.is_symlink():raise RuntimeError('Export path outside source')
   sid=source_id(source.namespace,path,source.root)
   records[sid]=(source,key,record,path);expected[source.id+'/'+key]=record.get('export_sha256',record['sha256'])
 return packets,records,expected


def snapshot_signature(packets):
 return {sid:{key:tuple(record.get(field)for field in ('original_path','sha256','export_sha256','status','role','export_path','char_start','char_end'))for key,record in packet['records'].items()}for sid,packet in packets.items()}


def current_record(source,key,record,path,grants):
 active={g['id']for g in json.loads(Path(grants).read_text()).get('grants',[])if g.get('active')is True}
 if GRANTS.get(source.namespace)not in active:raise RuntimeError('Source grant revoked during vector refresh')
 fresh=verified_record(source.approval_receipts,key,accepted_statuses=source.accepted_statuses)
 if not fresh or fresh['sha256']!=record['sha256']or fresh.get('export_sha256',fresh['sha256'])!=record.get('export_sha256',record['sha256']):raise RuntimeError('Source changed or revoked during vector refresh')
 if any(fresh.get(field)!=record.get(field)for field in ('original_path','status','role','export_path')):raise RuntimeError('Source identity or authority changed during vector refresh')
 if digest(path.read_bytes())!=record.get('export_sha256',record['sha256']):raise RuntimeError('Export bytes changed during vector refresh')
 return fresh


def native_exists(client,uri):
 result=subprocess.run([client,'stat',uri,'-o','json'],capture_output=True,text=True,timeout=45)
 if result.returncode==0:
  payload=json.loads(result.stdout)
  if payload.get('status')in(None,'ok')and isinstance(payload.get('result'),dict):return True
  raise RuntimeError('Native stat result unknown')
 # The pinned CLI emits missing-resource errors on stderr, not JSON.
 if 'not found'in result.stderr.lower():return False
 raise RuntimeError('Native target presence not proven')

def advance_readiness(manifest,manifest_path,grants,readiness,packets,records,expected,proofs,rows):
 if not readiness.exists():return False,'awaiting_baseline_recall'
 base=json.loads(readiness.read_text())
 if not base.get('passed'):return False,'awaiting_baseline_recall'
 versions=version_checks(rows,packets)
 if len(rows)!=len(expected)or not all(x['passed']for x in versions):return False,'pending_resource_versions'
 prior=base.get('source_hashes',{});changed={k for k,v in expected.items()if prior.get(k)!=v};removed=set(prior)-set(expected)
 by_key={source.id+'/'+key:(sid,source,key,record,path)for sid,(source,key,record,path)in records.items()}
 accepted=[]
 for key in changed:
  sid,source,_,record,_=by_key[key]
  proof=proofs.get(sid,{})
  if source.namespace!='ole_unconfirmed'and not(source.namespace=='ole-approved'and proof.get('index_mode')=='verified_original_vectors'and proof.get('original_path')==record['original_path']and proof.get('native_uri')==next(r['ov_resource_id']for r in rows if r['source_id']==sid)):
   return False,'blocked_approved_model_review'
  if proof.get('source_version')!=record['sha256']or proof.get('derived_sha')!=expected[key]or not proof.get('l2_complete')or not proof.get('recall_verified'):return False,'awaiting_targeted_recall'
  accepted.append(proof)
 # Removal is only inferred from fresh granted snapshots; approved changes need review.
 source_names={s.id:s.namespace for s in manifest.sources}
 if any(source_names.get(key.split('/',1)[0])!='ole_unconfirmed'for key in removed):return False,'blocked_approved_model_review'
 if not changed and not removed:return True,'verified_current'
 approved=next((s for s in manifest.sources if s.namespace=='ole-approved'),None)
 if not approved:return False,'approved_quote_source_missing'
 packet=packets[approved.id];quotes=[]
 for old in base.get('original_quote_checks',[]):
  result=search(old['query'],manifest=manifest_path,receipts=approved.approval_receipts,require_current=True)
  checks=[]
  for hit in result['results']:
   rec=next((r for r in packet['records'].values()if r['original_path']==hit['original_path']),None)
   checks.append(bool(rec and hit['source_version']==rec['sha256']and hit['quote']in(approved.approval_receipts.parent/'documents'/rec['export_path']).read_text()))
  quotes.append({'query':old['query'],'returned':len(checks),'all_original_quotes_valid':bool(checks)and all(checks)})
 if len(quotes)!=4 or not all(q['all_original_quotes_valid']for q in quotes):return False,'awaiting_original_quote_checks'
 current_packets,_,current_expected=snapshot(manifest,grants)
 if current_expected!=expected or snapshot_signature(current_packets)!=snapshot_signature(packets):return False,'source_changed_before_readiness'
 base.pop('incremental_proof',None)
 base.update(checked_at=time.time(),source_hashes=expected,resource_version_checks=versions,indexed_chunk_count=len(rows),original_document_count=len({r['original_path']for _,_,r,_ in records.values()}),original_quote_checks=quotes,reused_semantic_recall_checked_at=base['checked_at'],incremental_source_rechecks=accepted,incremental_removed_source_keys=sorted(removed),claim_limit='Prior recall reused only for unchanged sources; changed exact originals/derivatives individually L2/recall checked. Approved original vector updates do not generate semantic summaries or change rules. Current hashes and original quotes verified; personal answer quality is a separate check. No provider/model calls.')
 atomic(readiness,json.dumps(base,indent=2).encode());return True,'verified_current'


def refresh(manifest_path,grants,readiness,max_resources=40,max_seconds=480,allow_approved_originals=False):
 if not 1<=max_resources<=40 or not 1<=max_seconds<=480:raise ValueError('Native refresh bounds are 40 resources and 480 seconds')
 m=load_manifest(Path(manifest_path));packets,records,expected=snapshot(m,grants)
 # No DB/schema/native writes occur until a complete, fresh granted snapshot exists.
 root=m.state_db.parent;status_path=root/'native-refresh-status.json';proof_path=root/'incremental-vector-proofs.json'
 summary={'checked_at':time.time(),'updated':0,'approved_originals_updated':0,'deleted':0,'blocked_approved':0,'remaining_unconfirmed':0,'provider_dispatches':0,'model_dispatches':0,'readiness_published':False,'readiness_current':False,'phase':'deferred_deep_owner'}
 with(root/'native-provider.lock').open('a')as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:atomic(status_path,json.dumps(summary).encode());return summary
  budget=root/'deep-budget/control.json';measure=root/'deep-budget/measured.json'
  if budget.exists():
   value=json.loads(budget.read_text());measured=json.loads(measure.read_text())if measure.exists()else{}
   if value.get('active_job_id')or value.get('recovery_required')or measured.get('unsettled_reservations'):summary['phase']='deferred_physical_recovery';atomic(status_path,json.dumps(summary).encode());return summary
  if not native_drained():summary['phase']='blocked_native_queue_review';atomic(status_path,json.dumps(summary).encode());return summary
  c=sqlite3.connect('file:'+str(m.state_db)+'?mode=rw',uri=True);c.row_factory=sqlite3.Row
  try:
   old={r['source_id']:dict(r)for r in c.execute('SELECT * FROM resources')};proofs=json.loads(proof_path.read_text())if proof_path.exists()else{}
   base=json.loads(Path(readiness).read_text())if Path(readiness).exists()else{};prior=base.get('source_hashes',{})
   approved=[];changes=[]
   for sid,(source,key,record,path)in records.items():
    row=old.get(sid);sha=record.get('export_sha256',record['sha256'])
    proof=proofs.get(sid,{})
    eligible_approved=allow_approved_originals and source.namespace=='ole-approved'and record.get('status')=='confirmed'and record.get('role')in('explicit_policy','explicit_reference','confirmed_note')and sha==record['sha256']
    needs_proof=base.get('passed')is True and(source.namespace=='ole_unconfirmed'or eligible_approved)and prior.get(source.id+'/'+key)!=sha and(proof.get('source_version')!=record['sha256']or proof.get('derived_sha')!=sha or not proof.get('l2_complete')or not proof.get('recall_verified')or(eligible_approved and proof.get('index_mode')!='verified_original_vectors'))
    if row and row['sha256']==sha and row['status']=='synced'and not needs_proof:continue
    if source.namespace=='ole-approved'and not eligible_approved:approved.append(sid)
    else:changes.append(sid)
   summary['blocked_approved']=len(approved);started=time.monotonic();changed_count=0
   for sid in changes:
    if changed_count>=max_resources or time.monotonic()-started>=max_seconds:break
    source,key,record,path=records[sid];current_record(source,key,record,path,grants);sha=record.get('export_sha256',record['sha256'])
    uri='viking://resources/'+source.namespace+'/versions/'+digest(sid.encode())+'/'+sha
    # Approved policies must survive byte-for-byte. Native Markdown sectioning
    # inserts heading gaps and can omit the document title on long files. Use the
    # native verbatim layout, at a distinct immutable target; never waive L2 proof.
    parser_args=[]
    if source.namespace=='ole-approved':
     uri+='/original';parser_args=['--args','{"split_content":false}']
    if not exact_native(m.ov_binary,uri,path):
     if native_exists(m.ov_binary,uri):raise RuntimeError('Existing immutable vector target lacks exact original; inspect before retry')
     result=subprocess.run([m.ov_binary,'add-resource',str(path),'--to',uri,'--processing-mode','vectors_only',*parser_args,'--wait','--no-progress','-o','json'],capture_output=True,text=True,timeout=120)
     if result.returncode:raise RuntimeError('Local-vector native add failed')
     payload=json.loads(result.stdout).get('result',{});uri_actual=payload.get('root_uri',uri)if isinstance(payload,dict)else uri
     if not(uri_actual==uri or uri_actual.startswith(uri+'/')):raise RuntimeError('Unexpected vector target')
     uri=uri_actual
    if not exact_native(m.ov_binary,uri,path)or not native_recall(m.ov_binary,uri,path):raise RuntimeError('Native derivative exact/recall proof failed')
    current_record(source,key,record,path,grants);now=time.time();stat=path.stat()
    c.execute('BEGIN IMMEDIATE')
    try:
     current_record(source,key,record,path,grants)
     c.execute("INSERT INTO resources(source_id,source_root_id,namespace,path,relative_path,sha256,size_bytes,mtime_ns,status,ov_resource_id,attempts,first_seen,last_seen,last_synced,sync_receipt)VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET path=excluded.path,relative_path=excluded.relative_path,source_root_id=excluded.source_root_id,namespace=excluded.namespace,sha256=excluded.sha256,size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,status=excluded.status,ov_resource_id=excluded.ov_resource_id,error=NULL,lease_until=NULL,lease_owner=NULL,attempts=resources.attempts+1,last_seen=excluded.last_seen,last_synced=excluded.last_synced,sync_receipt=excluded.sync_receipt",(sid,source.id,source.namespace,str(path),path.relative_to(source.root).as_posix(),sha,stat.st_size,stat.st_mtime_ns,'synced',uri,1,now,now,now,_receipt(sid,sha,uri,now)))
     c.commit()
    except Exception:c.rollback();raise
    proofs[sid]={'source_id':sid,'source_version':record['sha256'],'derived_sha':sha,'native_uri':uri,'original_path':record['original_path'],'index_mode':'verified_original_vectors','l2_complete':True,'recall_verified':True,'checked_at':now};atomic(proof_path,json.dumps(proofs).encode());changed_count+=1;summary['updated']+=1
    if source.namespace=='ole-approved':summary['approved_originals_updated']+=1
   # Refresh absence proof before changing only our derivative registry statuses.
   fresh_packets,fresh_records,fresh_expected=snapshot(m,grants)
   if fresh_expected!=expected or snapshot_signature(fresh_packets)!=snapshot_signature(packets):raise RuntimeError('Source snapshot changed during native pass')
   for sid,row in old.items():
    if row['source_root_id']not in packets or sid in records or row['status']=='deleted':continue
    if changed_count>=max_resources or time.monotonic()-started>=max_seconds:break
    if row['namespace']!='ole_unconfirmed':approved.append(sid);summary['blocked_approved']+=1;continue
    c.execute("UPDATE resources SET status='deleted',error=NULL WHERE source_id=? AND sha256=?",(sid,row['sha256']));c.commit();changed_count+=1;summary['deleted']+=1
   lexical_refresh(m)
   rows=[r for r in c.execute("SELECT * FROM resources WHERE status='synced'").fetchall()
         if r['source_root_id'] in grant_source_ids(m)]
   current={r['source_id']:r for r in rows};summary['remaining_unconfirmed']=sum(source.namespace=='ole_unconfirmed'and(sid not in current or current[sid]['sha256']!=record.get('export_sha256',record['sha256']))for sid,(source,_,record,_)in records.items())
   readiness_path=Path(readiness);before=digest(readiness_path.read_bytes())if readiness_path.exists()else None
   summary['readiness_current'],summary['phase']=advance_readiness(m,Path(manifest_path),Path(grants),readiness_path,packets,records,expected,proofs,rows)
   summary['readiness_published']=summary['readiness_current']and readiness_path.exists()and digest(readiness_path.read_bytes())!=before
   if summary['blocked_approved']:summary['phase']='blocked_approved_model_review';summary['readiness_published']=False;summary['readiness_current']=False
   atomic(root/'approved-native-review-required.json',json.dumps({'checked_at':time.time(),'source_ids':approved,'model_dispatches':0,'action':'Review current approved native original/L0/L1 evidence; no automatic semantic update.'}).encode())
  except Exception as exc:
   summary.update(phase='failed_review_required',error_type=type(exc).__name__,error_code=failure_result(exc)['error_code']);atomic(status_path,json.dumps(summary).encode());raise
  finally:c.close()
 atomic(status_path,json.dumps(summary).encode());return summary

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--grants',type=Path,required=True);p.add_argument('--readiness',type=Path,required=True);p.add_argument('--max-resources',type=int,default=40);p.add_argument('--max-seconds',type=int,default=480);p.add_argument('--approved-originals',action='store_true',help='Index current confirmed original bytes with local vectors only; no generated summaries or rule changes.');a=p.parse_args()
 try:print(json.dumps(refresh(a.manifest,a.grants,a.readiness,a.max_resources,a.max_seconds,a.approved_originals)))
 except Exception as exc:
  # TimeoutExpired can contain a source-derived native query; never emit traceback/args.
  print(json.dumps(failure_result(exc)));raise SystemExit(1)
