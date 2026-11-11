"""Actual dense recall per active resource plus original-quote verification, no model answer scoring."""
import json,re,subprocess,time,concurrent.futures
from pathlib import Path
from .manifest import load_manifest
from .state import State
from .ole_policy import atomic,verified_record,digest
from .ole_search import search

def version_checks(rows,packets):
 checks=[]
 for row in rows:
  packet=packets.get(row['source_root_id'],{})
  record=packet.get('records',{}).get(Path(row['relative_path']).stem)
  expected=record.get('export_sha256',record['sha256'])if record else None
  path=Path(row['path'])
  passed=bool(expected and row['sha256']==expected and path.is_file()and digest(path.read_bytes())==expected)
  checks.append({'source_id':row['source_id'],'passed':passed})
 return checks

def verify(manifest: Path, receipts: Path, output: Path):
 m=load_manifest(manifest);state=State(m.state_db);packet=json.loads(receipts.read_text());results=[];sources={s.id:s for s in m.sources};packets={s.id:json.loads((s.approval_receipts or receipts).read_text())for s in m.sources}
 with state.connect() as c:rows=c.execute("SELECT * FROM resources WHERE status='synced'").fetchall()
 versions=version_checks(rows,packets)
 selected={};expected={}
 for row in rows:
  source=sources.get(row['source_root_id'])
  if not source:continue
  rec=packets[source.id]['records'].get(Path(row['relative_path']).stem)
  if rec:selected.setdefault(rec['original_path'],row)
 for sid,pack in packets.items():
  for key,rec in pack['records'].items():expected[sid+'/'+key]=rec.get('export_sha256',rec['sha256'])
 def check(row):
  source=sources[row['source_root_id']];receipt_path=source.approval_receipts or receipts
  key=Path(row['relative_path']).stem;record=verified_record(receipt_path,key,accepted_statuses=source.accepted_statuses,packet=packets[source.id])
  if not record:return {'source_id':row['source_id'],'passed':False,'reason':'source receipt not fresh'}
  text=Path(row['path']).read_text();head=next((line.lstrip('# ').strip() for line in text.splitlines() if line.startswith('# ') and len(line)>10),' '.join(re.findall(r'\w{4,}',text)[:10]));q=head[:180]
  t=time.monotonic();p=subprocess.run([m.ov_binary,'find',q,'--uri',row['ov_resource_id'],'--limit','10','-L','2','-o','json'],text=True,capture_output=True,timeout=45)
  payload=json.loads(p.stdout) if p.returncode==0 else {};response=payload.get('result',{});hits=response.get('resources',[]) if isinstance(response,dict) else []
  matched=any(isinstance(h,dict) and (h.get('uri')==row['ov_resource_id'] or h.get('uri','').startswith(row['ov_resource_id'].rstrip('/')+'/')) for h in hits)
  return {'source_id':row['source_id'],'source_status':record['status'],'semantic_recalled':matched,'passed':matched,'seconds':time.monotonic()-t,'hash':record['sha256']}
 with concurrent.futures.ThreadPoolExecutor(max_workers=2)as pool:results=list(pool.map(check,selected.values()))
 # Real broad retrieval additionally proves quoted bytes match approved source version.
 citation_checks=[]
 for q in ('Wer ist Alex Beispiel?', 'Welche Kalender muss ich vor Terminvorschlägen prüfen?', 'Darf ich E-Mail-Entwürfe selbst versenden?', 'Wo liegt die aktuelle CRM-Quelle für Acribic?'):
  t=time.monotonic();r=search(q,manifest=manifest,receipts=receipts,require_current=True)
  checks=[]
  for hit in r['results']:
   rec=next(v for v in packet['records'].values() if v['original_path']==hit['original_path'])
   checks.append(hit['source_version']==rec['sha256'] and hit['quote'] in (receipts.parent/'documents'/rec['export_path']).read_text())
  citation_checks.append({'query':q,'returned':len(checks),'all_original_quotes_valid':bool(checks) and all(checks),'seconds':time.monotonic()-t})
 report={'checked_at':time.time(),'passed':len(rows)==len(expected) and all(x['passed']for x in versions) and all(x['passed'] for x in results) and all(x['all_original_quotes_valid'] for x in citation_checks),'source_hashes':expected,'original_document_count':len(selected),'indexed_chunk_count':len(rows),'resource_version_checks':versions,'semantic_canaries':results,'original_quote_checks':citation_checks,'claim_limit':'Retrieval/index/source plumbing verified; no personal-answer accuracy or approved held-out labels claimed.'}
 atomic(output,json.dumps(report,ensure_ascii=False,indent=2).encode());return report
if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--receipts',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();r=verify(a.manifest,a.receipts,a.output);print(json.dumps({'passed':r['passed'],'semantic_canaries':len(r['semantic_canaries']),'quote_checks':len(r['original_quote_checks'])}));raise SystemExit(0 if r['passed'] else 1)
