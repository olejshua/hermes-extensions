"""Bounded content-free native Hermes telemetry. Physical and worker totals stay separate."""
from __future__ import annotations
import atexit, hashlib, json, os, queue, sqlite3, stat, threading, time, uuid
from pathlib import Path
from statistics import median
SCHEMA=1
HOOKS=('pre_api_request','post_api_request','api_request_error','pre_auxiliary_call','post_auxiliary_call','pre_tool_call','post_tool_call','pre_llm_call','post_llm_call','on_session_start','on_session_end','post_context_compression','post_context_pruning','ole_worker_usage','ole_external_provider_call','ole_coding_progress','ole_lcm_compression','ole_lcm_pruning')
PUBLIC=('provider','model','response_model','api_mode','aux_task','platform','tool_name','error_type','finish_reason','source_event','token_count_method')
NUMERIC=('retry_count','api_call_count','message_count','tool_count','approx_input_tokens','request_char_count','assistant_content_chars','assistant_tool_call_count','api_duration','started_at','ended_at','first_chunk_at','duration','duration_seconds','queue_wait_ms','before_tokens','after_tokens','duration_ms','removed_messages','original_tokens','compressed_tokens','pruned_tokens','before_message_count','after_message_count','before_chars','after_chars','summary_count')
def ident(v):
    return hashlib.sha256(str(v).encode()).hexdigest()[:24] if v not in ('',None) else None

def number(v):
    return v if isinstance(v,(int,float)) and not isinstance(v,bool) and v>=0 else None

def usage(u,route=None):
    if not isinstance(u,dict): return None
    u=dict(u)
    for legacy,explicit in (('input_available','input_tokens_available'),('cache_read_available','cache_read_tokens_available'),('cache_write_available','cache_write_tokens_available'),('output_available','output_tokens_available'),('reasoning_available','reasoning_tokens_available')):
        if explicit in u:u[legacy]=u[explicit]
    # Hermes CanonicalUsage input_tokens EXCLUDES cache; prompt_tokens INCLUDES it.
    cache=number(u.get('cache_read_tokens')) if u.get('cache_read_available') is not False else None
    write=number(u.get('cache_write_tokens')) if u.get('cache_write_available') is not False else None
    new=number(u.get('input_tokens')) if u.get('input_available') is not False and cache is not None and write is not None else None
    inclusive=number(u.get('prompt_tokens')) if u.get('prompt_tokens_available',u.get('input_available')) is not False else None
    if inclusive is None and all(v is not None for v in (new,cache,write)): inclusive=new+cache+write
    new_source='canonical_components_known' if new is not None else None
    if route in ('codex_responses','openai-codex') and inclusive is not None and cache is not None and cache<=inclusive:
        new=inclusive-cache;new_source='derived_codex_prompt_minus_cache_read'
    # Canonical normalization loses unavailable-vs-zero reasoning distinction.
    reasoning=number(u.get('reasoning_tokens')) if u.get('reasoning_available') is True else None
    return dict(input_inclusive=inclusive,input_new=new,input_new_source=new_source,cache_read=cache,cache_write=write,output=number(u.get('output_tokens')) if u.get('output_available') is not False else None,reasoning=reasoning,reasoning_known=reasoning is not None)

def project(event, p):
    out={'schema':SCHEMA,'event_id':str(uuid.uuid4()),'event':event,'time':time.time(),'role':'auxiliary' if 'auxiliary' in event else ('worker_aggregate' if event in ('worker_usage','ole_worker_usage') else 'main')}
    # External producers use wall-clock ended_at; monotonic durations never date events.
    if event=='ole_external_provider_call':
        stamp=number(p.get('ended_at'))
        if stamp is not None and 946684800<=stamp<=time.time()+300:out['time']=stamp
    if event=='ole_external_provider_call' and p.get('aux_task') in ('brain_ingest','brain_query') and not p.get('role'):
        p=dict(p,role='auxiliary')
        old=dict(p.get('usage') or {})
        for field,flag in (('cache_read_tokens','cache_read_available'),('cache_write_tokens','cache_write_available')):
            if flag not in old and old.get(field)==0:old[flag]=False
        p['usage']=old
    out['profile_id']=ident(p.get('profile_id') or os.environ.get('HERMES_HOME',str(Path.home()/'.hermes')))
    if p.get('role') in ('main','auxiliary','worker_physical','worker_aggregate'):out['role']=p['role']
    for k in ('task_id','turn_id','api_request_id','session_id','parent_task_id','job_id','tool_call_id'):
        out[k]=ident(p.get(k))
    for k in PUBLIC:
        v=p.get(k)
        # Identifiers only, no free-text error strings or paths.
        if isinstance(v,str) and len(v)<=160 and not any(c in v for c in '/\\\n\r'):
            out[k]=v
    for k in NUMERIC:
        out[k]=number(p.get(k))
    out['summary_degraded']=p.get('summary_degraded') if isinstance(p.get('summary_degraded'),bool) else None
    out['usage']=usage(p.get('usage'),p.get('api_mode') or p.get('provider'))
    if event in ('worker_usage','ole_worker_usage'):
        u=p.get('usage') or {}; inc=number(u.get('input_tokens')); cache=number(u.get('cached_input_tokens'))
        out['usage']=dict(input_inclusive=inc,input_new=inc-cache if inc is not None and cache is not None and cache<=inc else None,cache_read=cache,cache_write=None,output=number(u.get('output_tokens')),reasoning=number(u.get('reasoning_output_tokens')),reasoning_known='reasoning_output_tokens' in u)
    start=out.get('started_at'); first=out.get('first_chunk_at')
    out['ttfb_seconds']=first-start if start is not None and first is not None and first>=start else None
    request=p.get('request'); body=request.get('body') if isinstance(request,dict) else None
    if isinstance(body,dict):
        # Measurements of sanitized hook payload, NOT necessarily original full request.
        out['hook_request_bytes']=len(json.dumps(body,ensure_ascii=False,separators=(',',':')).encode())
        out['hook_schema_bytes']=len(json.dumps(body.get('tools',[]),ensure_ascii=False,separators=(',',':')).encode())
        out['hook_request_complete']=False # host sanitizer may truncate; no exactness claim.
    history=p.get('conversation_history')
    out['history_message_count']=len(history) if isinstance(history,list) else None
    seq=p.get('request_messages') or p.get('messages') or p.get('conversation_history')
    roles={}
    if isinstance(seq,list):
        for message in seq:
            if not isinstance(message,dict):continue
            role=message.get('role','unknown')
            if role not in ('system','developer','user','assistant','tool'):role='unknown'
            content=message.get('content');chars=0
            if isinstance(content,str):chars=len(content)
            elif isinstance(content,list):chars=sum(len(x.get('text','')) for x in content if isinstance(x,dict) and isinstance(x.get('text'),str))
            bucket=roles.setdefault(role,{'messages':0,'text_chars':0});bucket['messages']+=1;bucket['text_chars']+=chars
    system=p.get('system_prompt')
    out['system_prompt_chars']=len(system) if isinstance(system,str) else None
    out['context_by_role']=roles
    out['context_chars_scope']='hook messages text fields; system prompt separate; image/hidden/sanitized segments unknown'
    result=p.get('result')
    out['tool_result_utf8_bytes']=len(result.encode()) if isinstance(result,str) else None
    out['tool_result_structured_json_bytes']=len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode()) if isinstance(result,(dict,list)) else None
    out['output_truncated']=p.get('truncated') if isinstance(p.get('truncated'),bool) else None
    out['rss_kib']=None;out['cpu_process_seconds']=None
    if event in ('post_llm_call','on_session_end'):
        import resource
        resource_usage=resource.getrusage(resource.RUSAGE_SELF);out['rss_kib']=resource_usage.ru_maxrss;out['cpu_process_seconds']=resource_usage.ru_utime+resource_usage.ru_stime
    out['context_queue_seconds']=None
    return out

class Store:
    def __init__(self, home, capacity=512, retain_days=90, max_events=100000):
        self.root=Path(home)/'ole-observability'; self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        st=self.root.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid!=os.getuid(): raise PermissionError('unsafe telemetry directory')
        os.chmod(self.root,0o700)
        self.db=self.root/'events.sqlite3'
        try:
            fd=os.open(self.db,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600);os.close(fd)
        except FileExistsError:
            st=self.db.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid():raise PermissionError('unsafe telemetry database')
            os.chmod(self.db,0o600)
        self.q=queue.Queue(capacity); self.loss={'queue':0,'writer':0}; self.stop=threading.Event(); self.retain_days=retain_days; self.max_events=max_events
        self.spool=self.root/'external-spool';self.spool.mkdir(mode=0o700,exist_ok=True)
        if self.spool.is_symlink():raise PermissionError('unsafe external spool')
        self.thread=threading.Thread(target=self._writer,name='ole-telemetry',daemon=True); self.thread.start()
    def emit(self,event,**p):
        try: self.q.put_nowait(project(event,p))
        except queue.Full: self.loss['queue']+=1
        except Exception: self.loss['writer']+=1
    def _writer(self):
        try:
            con=sqlite3.connect(self.db); os.chmod(self.db,0o600)
            con.execute('PRAGMA journal_mode=DELETE'); con.execute('CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, t REAL, kind TEXT, task TEXT, data TEXT)')
            con.execute('CREATE INDEX IF NOT EXISTS task_time ON events(task,t)')
            con.execute('CREATE TABLE IF NOT EXISTS daily(day TEXT, family TEXT, model TEXT, provider TEXT, aux TEXT, count INTEGER, input INTEGER, cache INTEGER, output INTEGER, usage_known INTEGER, PRIMARY KEY(day,family,model,provider,aux))')
            con.execute('CREATE TABLE IF NOT EXISTS daily_known(day TEXT,family TEXT,model TEXT,provider TEXT,aux TEXT,field TEXT,n INTEGER,PRIMARY KEY(day,family,model,provider,aux,field))')
            last_cleanup=0
            while not self.stop.is_set() or not self.q.empty():
                try: row=self.q.get(timeout=.2)
                except queue.Empty:
                    for f in list(self.spool.glob('*.json'))[:32]:
                        try:
                            st=f.lstat()
                            if f.is_symlink() or st.st_uid!=os.getuid() or st.st_size>32768:continue
                            raw=json.loads(f.read_text()); rawid=raw.pop('event_id',None);event=raw.pop('event','ole_external_provider_call')
                            if event!='ole_external_provider_call':continue
                            row=project(event,raw)
                            if isinstance(rawid,str):row['event_id']=str(uuid.UUID(rawid))
                            self.q.put_nowait(row);f.unlink()
                        except Exception:self.loss['writer']+=1
                    continue
                try:
                    cur=con.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)',(row['event_id'],row['time'],row['event'],row['task_id'],json.dumps(row,separators=(',',':'))))
                    if cur.rowcount and row['event'] in ('post_api_request','post_auxiliary_call','api_request_error','worker_usage','ole_worker_usage','ole_external_provider_call'):
                        u=row.get('usage') or {}; key=(time.strftime('%Y-%m-%d',time.gmtime(row['time'])),row['role'],row.get('model','unknown'),row.get('provider','unknown'),row.get('aux_task',''))
                        con.execute('INSERT INTO daily VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(day,family,model,provider,aux) DO UPDATE SET count=count+1,input=input+excluded.input,cache=cache+excluded.cache,output=output+excluded.output,usage_known=usage_known+excluded.usage_known',(*key,1,u.get('input_inclusive') or 0,u.get('cache_read') or 0,u.get('output') or 0,int(any(isinstance(v,(int,float)) and not isinstance(v,bool) for v in u.values()))))
                        for field,value in u.items():
                            if isinstance(value,(int,float)) and not isinstance(value,bool):
                                con.execute('INSERT INTO daily_known VALUES(?,?,?,?,?,?,1) ON CONFLICT(day,family,model,provider,aux,field) DO UPDATE SET n=n+1',(*key,field))
                    con.commit()
                    if time.time()-last_cleanup>60:
                        con.execute('DELETE FROM events WHERE t<?',(time.time()-86400*self.retain_days,))
                        con.execute('DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY t DESC LIMIT -1 OFFSET ?)',(self.max_events,)); con.commit(); last_cleanup=time.time()
                except Exception: self.loss['writer']+=1
                finally: self.q.task_done()
            con.close()
        except Exception:
            self.loss['writer']+=1
            while True:
                try:self.q.get_nowait();self.q.task_done();self.loss['writer']+=1
                except queue.Empty:break
        finally:
            try:
                path=self.root/'loss.json'; fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0),0o600)
                with os.fdopen(fd,'w') as f: json.dump(self.loss,f)
            except Exception: pass
    def close(self): self.stop.set();self.thread.join(timeout=3)

_STORE=None
_STORE_LOCK=threading.Lock()
def get_store():
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            from hermes_cli.config import get_hermes_home
            _STORE=Store(get_hermes_home());atexit.register(_STORE.close)
    return _STORE

def report(home, task=None):
    db=Path(home)/'ole-observability/events.sqlite3'
    if not db.exists(): return {'events':0,'measurement':'no observations'}
    con=sqlite3.connect(f'file:{db}?mode=ro',uri=True)
    daily=[dict(zip(('day','role','model','provider','aux_task','attempts','input_inclusive','cache_read','output','usage_known'),r)) for r in con.execute('SELECT * FROM daily')]
    field_known={tuple(r[:5])+ (r[5],):r[6] for r in con.execute('SELECT * FROM daily_known')}
    for row in daily:
        key=tuple(row[k] for k in ('day','role','model','provider','aux_task'))
        row['known_by_field']={k:field_known.get(key+(k,),0) for k in ('input_inclusive','input_new','cache_read','cache_write','output','reasoning')}
        for field in ('input_inclusive','cache_read','output'):
            if row['known_by_field'][field]==0:row[field]=None
    rows=[json.loads(r[0]) for r in con.execute('SELECT data FROM events'+(" WHERE task=? OR json_extract(data,'$.job_id')=?" if task else ''),(ident(task),ident(task)) if task else ())];con.close()
    result={'events':len(rows),'subscription_limits':'separate account-level source; no token-to-percent estimate','retention_days':90,'physical':{},'worker_aggregate':{},'jobs':{},'profiles':{}}
    for r in rows:
        role=r['role']; event=r['event']; job=r.get('task_id') or r.get('job_id') or 'unattributed'
        if event not in ('post_api_request','post_auxiliary_call','api_request_error','worker_usage','ole_worker_usage','ole_external_provider_call'):continue
        key='worker_aggregate' if role=='worker_aggregate' else 'physical'; group=result[key].setdefault('|'.join(str(r.get(k) or 'unknown') for k in ('role','provider','model','aux_task')),{'attempts':0,'errors':0,'usage_known':0,'tokens':{},'latencies':[],'ttfb':[]})
        profile=r.get('profile_id') or 'unknown';result['profiles'][profile]=result['profiles'].get(profile,0)+1
        group['attempts']+=1;group['errors']+=bool(r.get('error_type') or event=='api_request_error')
        u=r.get('usage')
        if u:
            actual={k:v for k,v in u.items() if isinstance(v,(int,float)) and not isinstance(v,bool)}
            group['usage_known']+=bool(actual)
            group['usage_partial']=group.get('usage_partial',0)+int(0<len(actual)<6)
            known=group.setdefault('known_by_field',{})
            for k in ('input_inclusive','input_new','cache_read','cache_write','output','reasoning'):known[k]=known.get(k,0)+int(k in actual)
            for k,v in u.items():
                if isinstance(v,(int,float)) and not isinstance(v,bool):group['tokens'][k]=group['tokens'].get(k,0)+v
        if r.get('api_duration') is not None:group['latencies'].append(r['api_duration'])
        if r.get('ttfb_seconds') is not None:group['ttfb'].append(r['ttfb_seconds'])
        result['jobs'][job]=result['jobs'].get(job,0)+1
    for family in ('physical','worker_aggregate'):
        for g in result[family].values():
            for field in ('latencies','ttfb'):
                vals=sorted(g.pop(field));g[field+'_samples']=len(vals);g[field+'_p50']=median(vals) if len(vals)>=5 else None;g[field+'_p90']=vals[min(len(vals)-1,int(.9*len(vals)))] if len(vals)>=20 else None
    loss=Path(home)/'ole-observability/loss.json'
    result['daily_all_time']=daily if not task else None
    result['daily_usage_note']='Daily token columns sum observed values only. usage_known counts at least one known field; unknown attempts are not measured zero. Exact per-field provenance remains in retained request details.'
    result['request_details']=rows if task else None
    result['tool_calls']=sum(r['event']=='post_tool_call' for r in rows)
    result['tool_duration_ms']=sum(r.get('duration_ms') or 0 for r in rows if r['event']=='post_tool_call')
    result['retries_observed']=sum((r.get('retry_count') or 0)>0 for r in rows if r['event'] in ('pre_api_request','pre_auxiliary_call'))
    result['loss']=json.loads(loss.read_text()) if loss.exists() else {'status':'live process loss not yet flushed'}
    return result

def register(ctx):
    get_store() # Start bounded external-spool importer without a user or model turn.
    for event in HOOKS:
        ctx.register_hook(event,lambda _event=event,**p:get_store().emit(_event,**p))
    def setup(parser):parser.add_argument('--job')
    def handler(args):
        from hermes_cli.config import get_hermes_home
        print(json.dumps(report(get_hermes_home(),args.job),ensure_ascii=False,indent=2))
    ctx.register_cli_command('ole-usage','Measured physical calls and separate worker aggregates',setup,handler)
    ctx.register_command('ole-usage',lambda raw:json.dumps(report(__import__('hermes_cli.config',fromlist=['get_hermes_home']).get_hermes_home(),raw.strip() or None),ensure_ascii=False),description='Content-free measured usage')
