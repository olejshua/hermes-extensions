"""Durable project-specific metadata, intentionally separate from the readonly queue."""
import base64
import contextlib
import json
import mimetypes
import os
import sqlite3
import time
import uuid
import math
from functools import wraps
from pathlib import Path
from .safety import Fault,checked,private,read_file,write_private,digest,text,artifact_path
from .context import normalize as normalize_context, reference as context_reference, fingerprint as context_fingerprint, MAX_CONTEXT_BYTES

STATES={'queued':'Auftrag wartet auf einen freien Arbeitsplatz.','working':'Die Projektarbeit läuft.','reviewing':'Das Ergebnis wird gegen Auftrag und Quellen geprüft.','interrupted':'Die Arbeit ist angehalten und kann fortgesetzt werden.','recovering':'Die bestehende Arbeit wird nach einem Abbruch geprüft.','completed':'Das geprüfte Ergebnis ist bereit.','failed':'Die Arbeit ist mit einem konkreten Fehler angehalten.','uncertain':'Ein unklarer Vorgang muss zuerst abgeglichen werden.','blocked':'Eine notwendige Quelle oder Voraussetzung fehlt.'}

def api(fn):
    @wraps(fn)
    def call(*args,**kwargs):
        try: return fn(*args,**kwargs)
        except Fault as e:return {'ok':False,'error':e.code}
        except (OSError,sqlite3.Error):return {'ok':False,'error':'private_storage_error'}
    return call

class ProjectService:
    def __init__(self,policy:dict):
        self.policy=dict(policy)
        for key,default,maximum in (('max_run_seconds',3600,7200),('max_turns',8,16),('max_pending',10,20)):
            v=policy.get(key,default)
            if type(v) is not int or not 1<=v<=maximum:raise Fault('bounded_policy_required')
        if 'owner' not in policy or policy['owner'] is None or 'session_id' not in policy:raise Fault('trusted_identity_policy_required')
        self.root=private(policy['workspace_root'])
        self.jobs=private(self.root/'jobs')
        self.dbpath=checked(self.root/'project.sqlite3')
        fd=os.open(self.dbpath,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);os.close(fd)
        if self.dbpath.stat().st_mode&0o077:raise Fault('private_database_required')
        with self.db() as c:
            for statement in ('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, doc TEXT NOT NULL)', 'CREATE TABLE IF NOT EXISTS requests(scope TEXT, rid TEXT, fingerprint TEXT, job TEXT, command TEXT, PRIMARY KEY(scope,rid))', 'CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, job TEXT, kind TEXT, instruction TEXT, state TEXT, result TEXT)'):
                c.execute(statement)
    @contextlib.contextmanager
    def db(self):
        checked(self.dbpath)
        c=sqlite3.connect(self.dbpath,timeout=3,isolation_level=None)
        try:
            c.execute('PRAGMA journal_mode=DELETE');c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
            yield c;c.execute('COMMIT')
        except BaseException:
            if c.in_transaction:c.execute('ROLLBACK')
            raise
        finally:c.close()
    def get(self,c,job):
        r=c.execute('SELECT doc FROM jobs WHERE id=?',(job,)).fetchone()
        if not r:raise Fault('not_found')
        return json.loads(r[0])
    def save(self,c,j):
        j['updated_at']=time.time();c.execute('UPDATE jobs SET doc=? WHERE id=?',(json.dumps(j,ensure_ascii=False),j['id']))
    def scope(self,owner,session_id):
        if owner!=self.policy.get('owner') or session_id!=self.policy.get('session_id'):raise Fault('owner_session_mismatch')
        return digest([owner,session_id])
    def owned(self,c,job,owner,session):
        scope=self.scope(owner,session);j=self.get(c,job)
        if j['scope']!=scope:raise Fault('not_found')
        return j
    def public(self,j):
        result = {'ok':True,'job_id':j['id'],'project':j['project'],'state':j['state'],'description':STATES[j['state']], 'revision':j['revision'],'result_revision':j.get('result_revision'),'accepted_revision':j.get('turn_revision'),'error':j.get('error'),'thread_id':j.get('thread_id'),'turn_id':j.get('turn_id'),'phase':j['phase']}
        if j.get('monolith_context') is not None: result['monolith_reference']=context_reference(j['monolith_context'])
        return result
    def prior(self,c,scope,rid,fp):
        text(rid,200)
        r=c.execute('SELECT fingerprint,job,command FROM requests WHERE scope=? AND rid=?',(scope,rid)).fetchone()
        if r and r[0]!=fp:raise Fault('request_id_conflict')
        return r
    def source_specs(self,project):
        specs=self.policy.get('sources',{}).get(project)
        if not isinstance(specs,list) or not specs or len(specs)>100:raise Fault('verified_sources_required')
        return specs
    def snapshot(self,project,jobroot,monolith_context=None):
        folder=private(jobroot/'sources');manifest=[]
        for i,s in enumerate(self.source_specs(project)):
            p=checked(s['path'])
            if any(v in p.name.lower() for v in ('token','credential','secret','auth.json')) or p.name.startswith('.'):raise Fault('sensitive_source_forbidden')
            b=read_file(p,self.policy.get('max_source_bytes',4_000_000))
            canonical=s.get('mode')=='canonical'
            if canonical:
                if s.get('authority') not in ('rules','project_source'):raise Fault('source_authority_required')
            else:
                if time.time()-s['verified_at']>s.get('max_age_seconds',86400) or s['verified_at']>time.time()+60:raise Fault('source_stale')
                if digest(b)!=s['sha256']:raise Fault('source_changed_refresh_required')
            name=str(i).zfill(3)+'-'+digest(b)[:12]+'-'+p.name
            if p.suffix.lower() not in ('.md','.txt','.csv','.json','.py','.html'):raise Fault('source_type_forbidden')
            write_private(folder/name,b)
            manifest.append({'name':name,'title':text(s['title'],300),'origin':str(p),'sha256':digest(b),'mode':s.get('mode','dated'),'authority':s.get('authority','dated_evidence'),'captured_at':time.time(),'source_mtime':p.stat().st_mtime,'verified_at':s.get('verified_at'),'max_age_seconds':s.get('max_age_seconds',86400)})
        if monolith_context is not None:
            b=json.dumps(monolith_context,ensure_ascii=False,sort_keys=True).encode()
            name='monolith-task-'+digest(b)[:16]+'.json'
            write_private(folder/name,b)
            manifest.append({'name':name,'title':'Monolith-Aufgabe: '+monolith_context['task']['title'],
                'origin':'monolith:task:'+monolith_context['task']['id'],'sha256':digest(b),
                'mode':'monolith_snapshot','authority':'untrusted_task_context',
                'captured_at':monolith_context['captured_at']})
        write_private(folder/('MANIFEST-'+uuid.uuid4().hex+'.json'),json.dumps(manifest,ensure_ascii=False,indent=2))
        write_private(folder/'MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        return manifest
    @api
    def submission_receipt(self,project,goal,request_id,owner,session_id,task_id=None):
        """Recover the same accepted start before any optional network read."""
        scope=self.scope(owner,session_id)
        with self.db() as c:
            row=c.execute('SELECT job,command FROM requests WHERE scope=? AND rid=?',(scope,request_id)).fetchone()
            if row is None: return {'ok':True,'found':False}
            j=self.owned(c,row[0],owner,session_id)
            actual=(j.get('monolith_context') or {}).get('task',{}).get('id')
            if row[1] is not None or j['project']!=project or j['goal']!=goal or actual!=task_id:
                raise Fault('request_id_conflict')
            return {**self.public(j),'found':True}
    @api
    def submit(self,project,goal,request_id,owner,session_id,monolith_context=None,monolith_lease=None):
        scope=self.scope(owner,session_id);text(goal,80000)
        if project not in ('acribic','monolith'):raise Fault('project_not_allowed')
        monolith_context=normalize_context(monolith_context,project_kind=project)
        if project=='monolith' and (monolith_context is None or monolith_context.get('project') is not None and 'project_context' not in monolith_context):
            raise Fault('monolith_project_context_required')
        if monolith_lease is not None:
            if monolith_context is None: raise Fault('monolith_context_required')
            monolith_lease=self.valid_monolith_lease(monolith_lease)
        fp=digest(['submit',project,goal] + ([context_fingerprint(monolith_context)] if monolith_context is not None else []))
        with self.db() as c:
            prior=self.prior(c,scope,request_id,fp)
            if prior:return self.public(self.get(c,prior[1]))
            active=sum(json.loads(x[0])['state'] in ('queued','working','reviewing','recovering') for x in c.execute('SELECT doc FROM jobs'))
            if active>=self.policy.get('max_pending',10):raise Fault('queue_full')
            jid=uuid.uuid4().hex;r=private(self.jobs/jid);work=private(r/'work');private(work/'artifacts')
            manifest=self.snapshot(project,r,monolith_context)
            write_private(r/'goal.txt',goal)
            j={'id':jid,'project':project,'scope':scope,'goal':goal,'state':'queued','revision':0,'phase':'work','thread_id':None,'turn_id':None,'created_at':time.time(),'updated_at':time.time(),'sources':manifest,'instructions':[],'turns_used':0,'spent_seconds':0,'accounted_at':None,'inflight':None,'artifacts':[],'result_revision':None}
            if monolith_context is not None: j['monolith_context']=monolith_context
            if monolith_lease is not None: j['monolith_lease']=monolith_lease
            c.execute('INSERT INTO jobs VALUES(?,?)',(jid,json.dumps(j,ensure_ascii=False)))
            c.execute('INSERT INTO requests VALUES(?,?,?,?,?)',(scope,request_id,fp,jid,None))
            return self.public(j)
    @api
    def status(self,job_id,owner,session_id):
        with self.db() as c:return self.public(self.owned(c,job_id,owner,session_id))
    def valid_monolith_lease(self,value):
        from .context import exact_id
        if not isinstance(value,dict) or set(value)!={'assignment_id','claim_id','expires_at'}:
            raise Fault('monolith_lease_invalid')
        result={'assignment_id':exact_id(value['assignment_id']),'claim_id':exact_id(value['claim_id'])}
        expiry=value['expires_at']
        if type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry<=time.time():
            raise Fault('monolith_lease_expired')
        return {**result,'expires_at':expiry}
    @api
    def grant_monolith_lease(self,job_id,owner,session_id,lease):
        lease=self.valid_monolith_lease(lease)
        with self.db() as c:
            j=self.owned(c,job_id,owner,session_id);old=j.get('monolith_lease')
            if not old or old['assignment_id']!=lease['assignment_id']:raise Fault('monolith_assignment_mismatch')
            if old['claim_id']!=lease['claim_id'] and (old['expires_at']>time.time() or j.get('process') or j.get('inflight')):
                raise Fault('monolith_previous_execution_unreconciled')
            j['monolith_lease']=lease
            if j['state']=='blocked' and j.get('error')=='monolith_lease_expired' and not j.get('process'):
                j.update(state='queued',error=None)
            self.save(c,j);return self.public(j)
    @api
    def revoke_monolith_lease(self,job_id,owner,session_id):
        with self.db() as c:
            j=self.owned(c,job_id,owner,session_id)
            if j.get('monolith_lease') is not None:
                j['monolith_lease']['expires_at']=0;self.save(c,j)
            return self.public(j)
    @api
    def list_owned(self,owner,session_id):
        scope=self.scope(owner,session_id)
        with self.db() as c:return {'ok':True,'jobs':[self.public(j) for (d,) in c.execute('SELECT doc FROM jobs') if (j:=json.loads(d))['scope']==scope]}
    def command(self,kind,job_id,request_id,owner,session_id,instruction='',expected_revision=None):
        scope=self.scope(owner,session_id);fp=digest([kind,job_id,instruction,expected_revision])
        with self.db() as c:
            j=self.owned(c,job_id,owner,session_id);prior=self.prior(c,scope,request_id,fp)
            if prior:
                row=c.execute('SELECT state,result FROM commands WHERE id=?',(prior[2],)).fetchone()
                if row[0]=='rejected':return {'ok':False,'job_id':job_id,'command_state':'rejected','accepted':False,**(json.loads(row[1]) if row[1] else {'error':'control_rejected'})}
                return {**self.public(j),'command_state':row[0],'accepted':row[0]=='acked',**(json.loads(row[1]) if row[1] else {})}
            if j['state']=='uncertain':raise Fault('reconciliation_required')
            if c.execute("SELECT 1 FROM commands WHERE job=? AND state IN ('pending','sending','stopping')",(job_id,)).fetchone():raise Fault('control_pending')
            if kind=='steer':
                text(instruction,80000)
                if type(expected_revision)!=int or expected_revision!=j['revision']:raise Fault('revision_conflict')
                if j['state'] in ('failed','blocked'):raise Fault('resume_or_repair_required')
                j['revision']+=1;j['instructions'].append({'revision':j['revision'],'instruction':instruction})
                j['result_revision']=None;j['artifacts']=[]
                if j['state'] in ('completed','interrupted'):j['state']='queued';j['phase']='work'
            elif kind=='resume':
                if j['state'] not in ('interrupted','failed','blocked'):raise Fault('not_resumable')
                if j['inflight']:raise Fault('reconciliation_required')
                j['state']='queued';j['error']=None;j['turn_id']=None
                if j.get('monolith_lease') is not None:j['monolith_lease']['expires_at']=0
            elif kind=='interrupt':
                if j['state'] not in ('queued','working','reviewing','recovering'):raise Fault('not_running')
            cid=uuid.uuid4().hex
            c.execute('INSERT INTO commands VALUES(?,?,?,?,?,?)',(cid,job_id,kind,instruction,'pending',None))
            c.execute('INSERT INTO requests VALUES(?,?,?,?,?)',(scope,request_id,fp,job_id,cid));self.save(c,j)
            return {**self.public(j),'command_state':'pending','accepted':False}
    @api
    def steer(self,job_id,instruction,expected_revision,request_id,owner,session_id):return self.command('steer',job_id,request_id,owner,session_id,instruction,expected_revision)
    @api
    def interrupt(self,job_id,request_id,owner,session_id):return self.command('interrupt',job_id,request_id,owner,session_id)
    @api
    def resume(self,job_id,request_id,owner,session_id):return self.command('resume',job_id,request_id,owner,session_id)
    @api
    def result(self,job_id,owner,session_id):
        with self.db() as c:
            j=self.owned(c,job_id,owner,session_id)
            if j['state']!='completed' or j['result_revision']!=j['revision']:raise Fault('result_not_ready')
            if self.check_sources(j):
                return {'ok':False,'error':'historical_source_changed','job_id':j['id'],'revision':j['revision'],'result_revision':j['result_revision'],'state':j['state'],'historical':True,'source_status':'changed'}
            for a in j['artifacts']:
                if digest(read_file(artifact_path(self.jobs/job_id/'work',a['relative']),self.policy.get('max_artifact_bytes',4_194_304)))!=a['sha256']:raise Fault('artifact_changed')
            result = {'ok':True,'job_id':job_id,'revision':j['revision'],'summary':j['summary'],'uncertainties':j['uncertainties'],'artifacts':[{k:v for k,v in a.items() if k!='relative'} for a in j['artifacts']]}
            if j.get('monolith_context') is not None: result['monolith_reference']=context_reference(j['monolith_context'])
            return result
    @api
    def artifact_read(self,handle,owner,session_id):
        scope=self.scope(owner,session_id)
        with self.db() as c:
            for (d,) in c.execute('SELECT doc FROM jobs'):
                j=json.loads(d)
                if j['scope']!=scope or j['state']!='completed' or j['revision']!=j['result_revision']:continue
                for a in j['artifacts']:
                    if a['handle']==handle:
                        if self.check_sources(j):
                            return {'ok':False,'error':'historical_source_changed','job_id':j['id'],'revision':j['revision'],'result_revision':j['result_revision'],'state':j['state'],'historical':True,'source_status':'changed'}
                        b=read_file(artifact_path(self.jobs/j['id']/'work',a['relative']),self.policy.get('max_artifact_bytes',4_194_304))
                        if digest(b)!=a['sha256']:raise Fault('artifact_changed')
                        return {'ok':True,'name':a['name'],'mime':a['mime'],'sha256':a['sha256'],'size':len(b),'base64':base64.b64encode(b).decode()}
        raise Fault('artifact_not_found')
    def check_sources(self,j):
        current={s['path']:s for s in self.source_specs(j['project'])}
        changed=False
        linked=[s for s in j['sources'] if s.get('mode')=='monolith_snapshot']
        if len(linked) != (1 if j.get('monolith_context') is not None else 0):
            raise Fault('monolith_context_manifest_mismatch')
        for s in j['sources']:
            if s.get('mode')=='monolith_snapshot':
                context=normalize_context(j.get('monolith_context'),project_kind=j['project'])
                if context is None or s['origin']!='monolith:task:'+context['task']['id']:
                    raise Fault('monolith_context_invalid')
                b=read_file(self.jobs/j['id']/'sources'/s['name'],MAX_CONTEXT_BYTES)
                if digest(b)!=s['sha256'] or json.loads(b)!=context: raise Fault('source_snapshot_changed')
                continue
            spec=current.get(s['origin'])
            if not spec:raise Fault('source_removed')
            actual=digest(read_file(s['origin'],self.policy.get('max_source_bytes',4_000_000)))
            if spec.get('mode')=='canonical':
                if spec.get('authority') not in ('rules','project_source'):raise Fault('source_authority_required')
                changed=changed or actual!=s['sha256']
            else:
                if spec['sha256']!=s['sha256'] or actual!=s['sha256']:raise Fault('source_changed_refresh_required')
                if time.time()-spec['verified_at']>spec.get('max_age_seconds',86400):raise Fault('source_stale')
            if digest(read_file(self.jobs/j['id']/'sources'/s['name'],self.policy.get('max_source_bytes',4_000_000)))!=s['sha256']:raise Fault('source_snapshot_changed')
        return changed or set(current)!={s['origin'] for s in j['sources'] if s.get('mode')!='monolith_snapshot'}
    def _refresh(self,c,j):
        j['sources']=self.snapshot(j['project'],self.jobs/j['id'],j.get('monolith_context'))
        j['revision']+=1
        j['instructions'].append({'revision':j['revision'],'instruction':'Kanonische freigegebene Quellen wurden geändert. Lies das aktuelle MANIFEST.json und die neuen vollständigen Originale, prüfe Konflikte und passe alle Ergebnisse an. Frühere Quellenfassungen bleiben als Historie erhalten; aktuelle Regeln haben Vorrang.'})
        j.update(result_revision=None,artifacts=[],phase='work',state='queued')
        self.save(c,j)
    def refresh_canonical(self,job_id):
        with self.db() as c:
            j=self.get(c,job_id)
            if not self.check_sources(j):return False
            self._refresh(c,j);return True
