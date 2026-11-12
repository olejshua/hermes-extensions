"""Bounded historical research hints; never a readiness or sending decision."""
from pathlib import Path
from datetime import date
import json

def collect(days,day,limit=300):
    rows={};sources=[]
    for previous in sorted(Path(days).iterdir(),reverse=True):
        if not previous.is_dir()or previous.name>=day:continue
        try:date.fromisoformat(previous.name)
        except ValueError:continue
        if len(sources)>=3:break
        work=previous/'work';index=work/'research-index.json'
        if not index.is_file()or index.stat().st_size>4_000_000:continue
        try:value=json.loads(index.read_text())
        except (OSError,ValueError):continue
        if not isinstance(value,dict)or value.get('schema')!='acribic-research-index/v1'or not isinstance(value.get('rows'),list):continue
        reviews={}
        for name in ('candidate-reviews.json','firm-evidence.json'):
            path=work/name
            if not path.is_file()or path.stat().st_size>4_000_000:continue
            try:packet=json.loads(path.read_text())
            except (OSError,ValueError):continue
            if isinstance(packet,dict)and isinstance(packet.get('rows'),dict):reviews.update(packet['rows'])
        rejected={};feedback=previous/'preparation-feedback.json'
        if feedback.is_file()and feedback.stat().st_size<=2_000_000:
            try:feedback_value=json.loads(feedback.read_text())
            except (OSError,ValueError):feedback_value={}
            if isinstance(feedback_value,dict):
                revisions=feedback_value.get('revisions',[])
                if not isinstance(revisions,list):revisions=[]
                for revision in revisions:
                    if not isinstance(revision,dict):continue
                    ids=revision.get('source_ids',[])
                    if not isinstance(ids,list):continue
                    for source_id in ids:
                        if isinstance(source_id,str):rejected[source_id]=str(revision.get('reason','previously rejected'))[:1200]
        sources.append(previous.name)
        for old in value['rows']:
            if not isinstance(old,dict):continue
            source_id=old.get('source_id');company=old.get('company')
            if not isinstance(source_id,str)or not isinstance(company,str)or source_id in rows or len(rows)>=limit:continue
            review=reviews.get(source_id,{})
            if not isinstance(review,dict):review={}
            hint={'source_id':source_id,'company':company[:500],'research_date':previous.name,
                  'historical_technical_state':old.get('technical_state'),
                  'historical_different_procedures':old.get('different_procedures_found'),
                  'historical_judgement':str(review.get('fit_review',''))[:1800],
                  'historical_family_judgement':str(review.get('family_review',''))[:1800],
                  'historical_rejection':rejected.get(source_id),
                  'historical_decision':str(review.get('decision','not independently approved'))[:100]}
            for key in ('pressure_sources','family_sources','family_aliases','family_domains'):
                values=review.get(key,[])
                hint[key]=[x[:1000]for x in values[:40]if isinstance(x,str)]if isinstance(values,list)else[]
            rows[source_id]=hint
    return {'schema':'acribic-historical-research-hints/v1','status':'historical_non_authoritative',
            'warning':'Discovery hints only. No current SMTP, Gmail, family, source, readiness or send approval. Recheck current originals and every native gate.',
            'source_days':sources,'rows':list(rows.values())}
