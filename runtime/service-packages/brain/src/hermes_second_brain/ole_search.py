"""Dense+FTS fusion; status/consent/hash are checked independently of source prose and URI."""
import concurrent.futures,json,re,sqlite3,subprocess,time,urllib.request
from pathlib import Path
from .ole_policy import verified_record,digest
from .state import State
from .memory_access import checked_record,historical_snapshot,section_spans,read_original,source_allowed

def semantic_span(query,text,allowed_ranges=None):
 # Original Unicode spans only. Local CPU similarity selects a passage, no model-generated quote.
 ranges=allowed_ranges if allowed_ranges is not None else[(0,len(text))]
 if not ranges:raise ValueError('no eligible original passage')
 if len(ranges)==1 and ranges[0][1]-ranges[0][0]<=2400:return ranges[0]
 windows=[]
 for begin,stop in ranges:
  start=begin
  while start<stop:
   end=min(stop,start+1800)
   if end<stop:
    boundary=text.rfind('\n',start+900,end)
    if boundary>start:end=boundary
   windows.append((start,end))
   if end==stop:break
   start=max(start+1,end-250)
 if len(windows)>128:raise ValueError('original passage expansion exceeds bounded source size')
 vectors=[]
 inputs=[query]+[text[a:b]for a,b in windows]
 for begin in range(0,len(inputs),32):
  request=urllib.request.Request('http://127.0.0.1:8766/v1/embeddings',data=json.dumps({'input':inputs[begin:begin+32]}).encode(),headers={'Content-Type':'application/json'})
  with urllib.request.urlopen(request,timeout=45)as response:payload=json.load(response)
  vectors.extend(v['embedding']for v in payload['data'])
 q=vectors[0];scores=[sum(a*b for a,b in zip(q,v))for v in vectors[1:]]
 return windows[max(range(len(scores)),key=lambda i:scores[i])]

def lexical_refresh(manifest):
 state=State(manifest.state_db);db=manifest.state_db.parent/'lexical.sqlite3';c=sqlite3.connect(db)
 c.execute('CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(source_id UNINDEXED, version UNINDEXED, text, tokenize="unicode61 remove_diacritics 2")')
 with state.connect()as registry:rows=registry.execute("SELECT * FROM resources WHERE status='synced'").fetchall()
 active={r['source_id']:r for r in rows};old={r[0]:r[1]for r in c.execute('SELECT source_id,version FROM chunks')}
 for key in set(old)-set(active):c.execute('DELETE FROM chunks WHERE source_id=?',(key,))
 for key,row in active.items():
  if old.get(key)==row['sha256']:continue
  c.execute('DELETE FROM chunks WHERE source_id=?',(key,));p=Path(row['path'])
  if p.is_file():c.execute('INSERT INTO chunks(source_id,version,text)VALUES(?,?,?)',(key,row['sha256'],p.read_bytes().decode('utf-8')))
 c.commit();c.close();return db

def search(query: str, *, manifest: Path, receipts: Path, limit=5, require_current=False, include_unconfirmed=True):
 from .manifest import load_manifest
 m=load_manifest(manifest);state=State(m.state_db)
 if len(query)>500 or not query.strip():raise ValueError('bounded query required')
 sources={s.id:s for s in m.sources if source_allowed(m,s)and(s.namespace=='ole-approved'or(include_unconfirmed and s.namespace=='ole_unconfirmed'))}
 with state.connect()as c:rows=[dict(r)for r in c.execute("SELECT * FROM resources WHERE status='synced'")if r['source_root_id']in sources]
 by_id={r['source_id']:r for r in rows};by_uri={r['ov_resource_id']:r for r in rows if r['ov_resource_id']};packets={sid:json.loads((s.approval_receipts or receipts).read_text())for sid,s in sources.items()}
 deep_alias={}
 with state.connect()as c:
  if c.execute("SELECT 1 FROM sqlite_master WHERE type='table'AND name='deep_resources'").fetchone():
   for deep in c.execute("SELECT * FROM deep_resources WHERE status='indexed_verified'"):
    matching=next((row for row in rows if packets[row['source_root_id']]['records'].get(Path(row['relative_path']).stem,{}).get('original_path')==deep['original_path']and packets[row['source_root_id']]['records'][Path(row['relative_path']).stem]['sha256']==deep['source_version']),None)
    p=Path(deep['input_path'])
    if matching and p.is_file()and digest(p.read_bytes())==deep['input_sha']:deep_alias[deep['native_uri']]={'row':matching,'deep_text_path':str(p),'deep_text_sha':deep['input_sha'],'native_uri':deep['native_uri']}
 # URI namespaces constrain candidates; returned URI must additionally resolve to active registry.
 def dense(namespace):
  p=subprocess.run([m.ov_binary,'find',query,'--uri','viking://resources/'+namespace,'--limit','30','-L','0,1,2','-o','json'],capture_output=True,text=True,timeout=45)
  if p.returncode:raise RuntimeError('Semantic index unavailable; no ready claim')
  result=json.loads(p.stdout).get('result',{});hits=result.get('resources',[])
  if not isinstance(hits,list):raise ValueError('unexpected OV search schema')
  return namespace,hits
 candidates={};namespaces=sorted({sources[r['source_root_id']].namespace for r in rows})
 with concurrent.futures.ThreadPoolExecutor(max_workers=2)as pool:
  for ns,hits in pool.map(dense,namespaces):
   for rank,hit in enumerate(hits,1):
    uri=hit.get('uri','')if isinstance(hit,dict)else''
    if not uri.startswith('viking://resources/'+ns+'/'):continue
    target=uri
    while target not in by_uri and target not in deep_alias and target.count('/')>3:target=target.rsplit('/',1)[0]
    deep=deep_alias.get(target);row=deep['row']if deep else by_uri.get(target)
    if not row or row['namespace']!=ns:continue
    key=('deep:'+target)if deep else row['source_id'];current=candidates.setdefault(key,{'row':row,'score':0,'modes':set()});current['semantic_score']=max(current.get('semantic_score',0),float(hit.get('score',1/(60+rank))));current['score']=.9*current['semantic_score']+current.get('lexical_bonus',0);current['modes'].add('semantic')
    if deep:current['deep']=deep
 terms=set(re.findall(r'\w{3,}',query.casefold()))-{'der','die','das','den','dem','des','und','oder','mit','von','aus','bei','für','ein','eine','einer','einen','einem','wie','was','wer','wann','ist','sind','war','waren','wird','werden','hat','haben','muss','darf','soll','kann','ich','mir','man','the','and','for','how','what'};db=m.state_db.parent/'lexical.sqlite3'
 if db.is_file()and terms:
  with sqlite3.connect(db)as c:
   matches=c.execute('SELECT source_id FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks) LIMIT 100',(' OR '.join('"'+t.replace('"','')+'"'for t in terms),)).fetchall()
  for rank,(key,)in enumerate(matches,1):
   if key not in by_id:continue
   current=candidates.setdefault(key,{'row':by_id[key],'score':0,'modes':set()});current['lexical_bonus']=6/(60+rank);current['score']=.9*current.get('semantic_score',0)+current['lexical_bonus'];current['modes'].add('lexical')
 elif terms:
  # Small regression fixtures may not have a built lexical derivative yet.
  for row in rows:
   p=Path(row['path'])
   if p.is_file()and any(t in p.read_bytes().decode('utf-8').casefold()for t in terms):candidates.setdefault(row['source_id'],{'row':row,'score':.01,'modes':{'lexical'}})
 seen=set();output=[]
 for candidate in sorted(candidates.values(),key=lambda x:x['score'],reverse=True):
  row=candidate['row'];source=sources[row['source_root_id']];path=source.approval_receipts or receipts;key=Path(row['relative_path']).stem
  record=checked_record(source,key,receipts=receipts,allow_stale=not require_current,packet=packets[source.id])
  if not record or record.get('export_sha256',record['sha256'])!=row['sha256']:continue
  if require_current and(record['status']!='confirmed'or historical_snapshot(record)):continue
  if record['original_path']in seen:continue
  deep=candidate.get('deep')
  if deep:
   original_text=Path(deep['deep_text_path'])
   if not original_text.is_file()or digest(original_text.read_bytes())!=deep['deep_text_sha']:continue
   text=original_text.read_bytes().decode('utf-8');record={**record,'char_start':0,'line_start':1}
   if len(text)>170000:
    # A bounded deep expansion cannot abort other results. Use an actually retrieved sibling chunk.
    siblings=[item for item in candidates.values()if not item.get('deep')and item['row']['source_root_id']==source.id and packets[source.id]['records'].get(Path(item['row']['relative_path']).stem,{}).get('original_path')==record['original_path']]
    if not siblings:continue
    sibling=max(siblings,key=lambda item:item['score']);row=sibling['row'];candidate=sibling
    record=checked_record(source,Path(row['relative_path']).stem,receipts=receipts,allow_stale=not require_current,packet=packets[source.id])
    if not record or record.get('export_sha256',record['sha256'])!=row['sha256']:continue
    text=(path.parent/'documents'/record['export_path']).read_bytes().decode('utf-8');deep=None

  else:
   raw=(path.parent/'documents'/record['export_path']).read_bytes()
   if digest(raw)!=record.get('export_sha256',record['sha256']):continue
   text=raw.decode('utf-8')
  sections=section_spans(text);ranges=[];begin=0
  if require_current:
   for span in sections:
    if begin<span['start']:ranges.append((begin,span['start']))
    begin=span['end']
   if begin<len(text):ranges.append((begin,len(text)))
  else:ranges=[(0,len(text))]
  if not ranges:continue
  if 'semantic'in candidate['modes'] and len(text)>2400:
   try:local_start,local_end=semantic_span(query,text,allowed_ranges=ranges)
   except ValueError:continue
  else:
   begin,stop=max(ranges,key=lambda span:sum(t in text[span[0]:span[1]].casefold()for t in terms))
   lines=text[begin:stop].splitlines(keepends=True);weights=[sum(t in l.casefold()for t in terms)for l in lines]
   center=max(range(len(lines)),key=lambda i:weights[i])if lines else 0
   local_start=begin+sum(len(line)for line in lines[:max(0,center-3)]);local_end=min(stop,local_start+2400)
  excerpt=text[local_start:local_end];start=text[:local_start].count('\n')
  if not excerpt:continue
  quote_start=record.get('char_start',0)+local_start;quote_end=quote_start+len(excerpt)
  section_states=sorted({span['status']for span in sections if span['start']<local_end and span['end']>local_start})
  open_section=any(s in ('open/not decided','draft/not confirmed')for s in section_states)
  historical_section='historical/not current'in section_states
  if require_current and section_states:continue
  fresh=checked_record(source,Path(row['relative_path']).stem,receipts=receipts,allow_stale=not require_current)
  if not fresh or fresh['sha256']!=record['sha256']or fresh.get('export_sha256',fresh['sha256'])!=row['sha256']or any(fresh.get(k)!=record.get(k)for k in ('original_path','status','role'))or not source_allowed(m,source):continue
  status=record['status'];authority='hypothesis'if open_section else 'historical_evidence'if historical_section or historical_snapshot(record)or record['stale']or status in ('unknown','superseded')else'hypothesis'if status=='hypothesis'else'source_evidence'
  output.append({'source_id':row['source_id'],'source_uri':row['ov_resource_id'],'original_path':record['original_path'],'source_version':record['sha256'],'indexed_version':row['sha256'],'quote_derivative_version':deep['deep_text_sha']if deep else record.get('export_sha256',record['sha256']),'retrieval_candidate_uri':deep['native_uri']if deep else row['ov_resource_id'],'section_status':'open/not decided'if open_section else'historical/not current'if historical_section else'unspecified','section_states':section_states,'source_status':status,'status_label':{'confirmed':'bestätigt/ausdrücklich freigegeben','unknown':'unbestätigt/unmarkiert','hypothesis':'Entwurf','superseded':'veraltet/abgelöst'}[status],'authority':authority,'source_role':record['role'],'last_verified':record['last_verified'],'age_seconds':record['age_seconds'],'stale':record['stale'],'current_authority':False,'line_start':record.get('line_start',1)+start,'line_end':record.get('line_start',1)+start+excerpt.count('\n'),'original_text_char_start':quote_start,'original_text_char_end':quote_end,'span_format':'parsed source text; Unicode character offsets, zero-based end-exclusive','line_coordinate_kind':'extracted text lines (not PDF pages)'if record.get('source_extension')in('.pdf','.docx')else'original text lines','quote':excerpt,'retrieval_mode':'+'.join(sorted(candidate['modes'])),'instructions_in_source':'data_only','original_host_verification':Path(record['original_path']).exists(),'live_precedence':['current calendar sources and reservations configured for the relevant project','current CRM or workspace source configured for the relevant project']})
  seen.add(record['original_path'])
  if len(output)>=min(10,limit):break
 return {'results':output,'abstain':not output,'index':'openviking-dense+verified-lexical','source_verification':'original hash + derivative hash + current consent receipt','source_refresh_deadline_seconds':1800}

if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('query',nargs='?',default='');p.add_argument('--manifest',type=Path,required=True);p.add_argument('--receipts',type=Path,required=True);p.add_argument('--require-current',action='store_true');p.add_argument('--approved-only',action='store_true');p.add_argument('--refresh-lexical',action='store_true');p.add_argument('--source-id');p.add_argument('--expected-version');p.add_argument('--offset',type=int,default=0);p.add_argument('--length',type=int,default=12000)
 a=p.parse_args()
 if a.refresh_lexical:
  from .manifest import load_manifest
  lexical_refresh(load_manifest(a.manifest));print(json.dumps({'lexical_refreshed':True}));raise SystemExit(0)
 if a.source_id:
  result=read_original(a.source_id,manifest=a.manifest,receipts=a.receipts,expected_version=a.expected_version,offset=a.offset,length=a.length,require_current=a.require_current)
 else:result=search(a.query,manifest=a.manifest,receipts=a.receipts,require_current=a.require_current,include_unconfirmed=not a.approved_only)
 print(json.dumps(result,ensure_ascii=False))
