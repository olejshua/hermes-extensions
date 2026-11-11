"""Actual selected-corpus recall uses Jasper quality_eval, with answer gates honestly unscored."""
import json,time
from pathlib import Path
from .ole_search import search
from .quality_eval import evaluate,write_private
from .state import State
from .manifest import load_manifest

def run(manifest: Path, receipts: Path, output: Path):
 packet=json.loads(receipts.read_text());s=State(load_manifest(manifest).state_db)
 with s.connect() as c: rows=c.execute("SELECT * FROM resources WHERE status='synced'").fetchall()
 look={r['relative_path']:r for r in rows}
 cases=[];predictions={'cases':[]}
 queries=[('person','Wer ist Alex Beispiel?','PROFIL.md'),('calendar','Terminvorschläge live im Kalender CODE Fuxam prüfen','AGENTS.md'),('drafts','Entwürfe schickt Alex selbst','entwuerfe-schickt-ole-selbst.md'),('crm','Acribic operativer Wechsel Twenty CRM','acribic-twenty-operativer-wechsel.md')]
 for i,(name,q,file)in enumerate(queries):
  record=next(v for v in packet['records'].values() if Path(v['original_path']).name==file);row=look.get(record['export_path']);expected=row['source_id'] if row else 'not-indexed'
  cases.append({'id':name,'category':name,'split':'development' if i==0 else 'held_out','label_status':'draft','label_basis':'selected original source path + SHA256','answerable':True,'expected_any':[expected]})
  t=time.monotonic();result=search(q,manifest=manifest,receipts=receipts,require_current=True)
  predictions['cases'].append({'id':name,'status':'ok','seconds':time.monotonic()-t,'retrieved':[{'source_id':h['source_id'],'snippet':h['quote'],'provenance':{'source_uri':h['source_uri'],'source_version':h['source_version']}}for h in result['results']],'citations':[h['source_id']for h in result['results']],'abstained':result['abstain']})
 spec={'scope':'personal_memory','cases':cases};report=evaluate(spec,predictions)
 write_private(output/'spec.json',spec);write_private(output/'predictions.json',predictions);write_private(output/'report.json',report)
 return {'case_count':report['case_count'],'avg_recall_at_k':report['avg_recall_at_k'],'labels_reviewed':report['labels_reviewed'],'quality_gate_passed':report['quality_gate_passed'],'answer_scored_count':report['answer_scored_count']}
if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--receipts',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();print(json.dumps(run(a.manifest,a.receipts,a.output)))
