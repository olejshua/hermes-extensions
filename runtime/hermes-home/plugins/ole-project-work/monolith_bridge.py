"""Durable native linked-work v1 adapter. Polling never invokes a model.

Monolith owns tasks/approval/results; the existing Engine owns computation.
Every network write and Engine start has a persisted, reusable identity.
"""
import base64
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
import re
import threading
import time
import uuid
from .monolith_wire import Wire,Unavailable,LIMIT

FINAL={'completed','failed','cancelled','rejected','dismissed','expired'}
JS_WHITESPACE='\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'


def uid(value):
    if not isinstance(value,str):raise ValueError('monolith_identity_invalid')
    try:result=str(uuid.UUID(value))
    except ValueError:raise ValueError('monolith_identity_invalid') from None
    if value.lower()!=result:raise ValueError('monolith_identity_invalid')
    return result


def epoch(value):
    if not isinstance(value,str):raise ValueError('monolith_lease_receipt_invalid')
    try:parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:raise ValueError('monolith_lease_receipt_invalid') from None
    if parsed.tzinfo is None:raise ValueError('monolith_lease_receipt_invalid')
    return parsed.timestamp()


def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


class Bridge:
    def __init__(self,ledger,service,*,wire_factory,clock=time.time):
        self.ledger,self.s,self.wire_factory,self.clock=ledger,service,wire_factory,clock
        self.lock=threading.RLock();self.wire=None;self.reconciled=set()
        self.discovery_after=0;self.discovery_failures=0;self.discovery_error=None
        with ledger.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS monolith_links(id TEXT PRIMARY KEY, scope TEXT NOT NULL, native_request TEXT NOT NULL, doc TEXT NOT NULL, UNIQUE(scope,native_request))')
            c.execute('CREATE TABLE IF NOT EXISTS monolith_operations(id TEXT PRIMARY KEY, link TEXT NOT NULL, doc TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS monolith_controls(id TEXT PRIMARY KEY, link TEXT NOT NULL, doc TEXT NOT NULL)')
    def save(self,row):
        with self.ledger.db() as c:c.execute('UPDATE monolith_links SET doc=? WHERE id=?',(encoded(row),row['id']))
    def call(self,path,body=None):
        if self.wire is None:self.wire=self.wire_factory()
        return self.wire.call(path,body)
    def rows(self):
        with self.ledger.db() as c:return [json.loads(r[0]) for r in c.execute('SELECT doc FROM monolith_links ORDER BY rowid')]
    def find(self,identifier):
        if not isinstance(identifier,str):return None
        return next((r for r in self.rows() if identifier in (r['id'],r.get('engine_job_id'))),None)
    def owned(self,row,owner,session):
        self.s.scope(owner,session)
        if row['owner']!=owner or row['session']!=session:raise ValueError('current_private_human_required')
        return row
    def create(self,goal,task_id,native_request,owner,session,title=None,project_kind='acribic'):
        self.s.scope(owner,session);task_id=uid(task_id)
        if project_kind not in ('acribic','monolith'):raise ValueError('monolith_project_kind_invalid')
        canonical=goal.strip(JS_WHITESPACE) if isinstance(goal,str) else None
        if not canonical or len(canonical.encode('utf-16-le'))//2>12000:
            raise ValueError('linked_instruction_too_large')
        request_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'hermes-linked:'+owner+'\0'+str(session)+'\0'+native_request))
        scope=hashlib.sha256(encoded([owner,session]).encode()).hexdigest()
        title=' '.join((title or canonical).strip(JS_WHITESPACE).split())[:100]
        row={'id':'monolith_'+request_id.replace('-',''),'owner':owner,'session':session,'goal':goal,
             'task_id':task_id,'request_id':request_id,'phase':'proposal','engine_job_id':None,
             'next_at':0,'failures':0,'error':None,'paused':False,'project_kind':project_kind,
             'proposal':{'requestId':request_id,'taskId':task_id,'title':title,'instruction':canonical,'agentKind':'hermes'}}
        with self.lock,self.ledger.db() as c:
            c.execute('INSERT OR IGNORE INTO monolith_links VALUES(?,?,?,?)',(row['id'],scope,native_request,encoded(row)))
            actual=json.loads(c.execute('SELECT doc FROM monolith_links WHERE scope=? AND native_request=?',(scope,native_request)).fetchone()[0])
            if actual['goal']!=goal or actual['task_id']!=task_id or actual.get('project_kind','acribic')!=project_kind:raise ValueError('request_id_conflict')
        return self.public(actual)
    def adopt(self,items):
        """Persist explicit human Task handoffs. No proposal, claim or inference."""
        owner,session=self.s.policy['owner'],self.s.policy['session_id'];self.s.scope(owner,session)
        scope=hashlib.sha256(encoded([owner,session]).encode()).hexdigest();rows=[]
        for item in items:
            if not isinstance(item,dict):raise ValueError('monolith_discovery_invalid')
            assignment,request,task=[uid(item.get(key)) for key in ('assignmentId','requestId','taskId')]
            goal,title=item.get('instruction'),item.get('title')
            if not isinstance(goal,str) or not goal.strip() or len(goal.encode('utf-16-le'))//2>12000 or not isinstance(title,str) or not title.strip() or len(title.encode('utf-16-le'))//2>200:
                raise ValueError('monolith_discovery_invalid')
            rows.append({'id':'monolith_'+request.replace('-',''),'owner':owner,'session':session,'goal':goal,
             'task_id':task,'request_id':request,'assignment_id':assignment,'action_id':assignment,
             'phase':'claim','engine_job_id':None,'next_at':0,'failures':0,'error':None,'paused':False,
             'origin':'monolith_task','project_kind':'monolith','remote_status':'queued',
             'proposal':{'requestId':request,'taskId':task,'title':title,'instruction':goal,'agentKind':'hermes'}})
        with self.ledger.db() as c:
            for row in rows:
                old=c.execute('SELECT doc FROM monolith_links WHERE id=?',(row['id'],)).fetchone()
                if old:
                    prior=json.loads(old[0])
                    if prior['task_id']!=row['task_id'] or prior.get('assignment_id') not in (None,row['assignment_id']) or prior['proposal']['instruction']!=row['goal'] or prior['owner']!=owner or prior['session']!=session:
                        raise ValueError('monolith_discovery_identity_conflict')
                    continue
                c.execute('INSERT INTO monolith_links VALUES(?,?,?,?)',(row['id'],scope,'human-handoff:'+row['request_id'],encoded(row)))
    def discover(self):
        try:
            value=self.call('/agent/assignments/ready')
            if not isinstance(value,dict) or not isinstance(value.get('assignments'),list) or len(value['assignments'])>101 or type(value.get('truncated')) is not bool:
                raise ValueError('monolith_discovery_invalid')
            self.adopt(value['assignments'])
            self.discovery_after=self.clock()+5;self.discovery_failures=0;self.discovery_error=None
        except Exception:
            self.discovery_failures+=1;self.discovery_after=self.clock()+min(300,15*2**min(self.discovery_failures-1,5));self.discovery_error='monolith_discovery_unavailable'
    def public(self,row):
        job=self.s.status(row['engine_job_id'],row['owner'],row['session']) if row.get('engine_job_id') else {}
        if row['phase'] in ('proposal','approval'):
            state='awaiting_execution' if row.get('remote_status')=='awaiting_execution' else 'awaiting_approval' if row.get('action_id') else 'awaiting_monolith_submission'
        elif row['phase']=='published':state=row['remote_status']
        elif row['phase']=='cancelled':state='interrupted'
        elif row['phase']=='blocked':state='blocked'
        else:state=job.get('state','queued')
        if row['paused'] and not row.get('engine_job_id'):state='interrupted'
        result={**job,'ok':True,'job_id':row['id'],'title':row['proposal']['title'],'project':row.get('project_kind','acribic'),'state':state,
                'accepted':True,'revision':job.get('revision',0),'monolith_publication':{'phase':row['phase'],'request_id':row['request_id'],
                 'assignment_id':row.get('assignment_id'),'status':row.get('remote_status'),
                 'result_readback_verified':row['phase']=='published' and row.get('remote_status')=='completed','error':row.get('error')}}
        if isinstance(result.get('monolith_reference'),dict):
            result['monolith_reference']={**result['monolith_reference'],'status_published_to_monolith':row['phase']=='published'}
        return result
    def command(self,row,action,native_request,owner,session,**args):
        self.owned(row,owner,session)
        if action not in ('pause','resume','steer'):raise ValueError('linked_control_invalid')
        doc={'action':action,'args':args,'request':native_request,'state':'pending'}
        with self.ledger.db() as c:
            c.execute('INSERT OR IGNORE INTO monolith_controls VALUES(?,?,?)',(native_request,row['id'],encoded(doc)))
            old=json.loads(c.execute('SELECT doc FROM monolith_controls WHERE id=?',(native_request,)).fetchone()[0])
            if old['action']!=action or old['args']!=args:raise ValueError('request_id_conflict')
        return {**self.public(row),'accepted':old['state']=='acked','command_state':old['state'],**old.get('result',{}),'job_id':row['id']}
    def apply_controls(self,row):
        with self.ledger.db() as c:
            pending=c.execute('SELECT id,doc FROM monolith_controls WHERE link=? ORDER BY rowid',(row['id'],)).fetchall()
        for cid,raw in pending:
            control=json.loads(raw)
            if control['state'] not in ('pending','forwarded'):continue
            action=control['action'];job=row.get('engine_job_id');engine=self.engine(row)
            final_fence=row['phase'] in ('verify','published','cancelled') or (row.get('pending_report') or {}).get('body',{}).get('status') in ('completed','failed')
            if control['state']=='forwarded':
                # Replay the SAME durable engine command to read its actual ACK.
                # Queue acceptance is not evidence that the worker has applied it.
                if action=='steer':result=self.s.steer(job,control['args']['instruction'],control['args']['expected_revision'],cid,row['owner'],row['session'])
                else:
                    method=self.s.interrupt if action=='pause' else self.s.resume
                    result=method(job,cid,row['owner'],row['session'])
            elif final_fence:
                result={'ok':False,'error':'linked_final_requires_new_request'}
            elif action=='pause':
                row['paused']=True
                result=self.s.interrupt(job,cid,row['owner'],row['session']) if job else {'ok':True,'accepted':True}
            elif action=='resume':
                row['paused']=False
                if row['phase']=='blocked':row.update(phase=row.get('error_phase','approval'),error=None,next_at=0)
                result=self.s.resume(job,cid,row['owner'],row['session']) if job and engine['state'] in ('interrupted','blocked','failed') else {'ok':True,'accepted':True}
            elif job:
                result=self.s.steer(job,control['args']['instruction'],control['args']['expected_revision'],cid,row['owner'],row['session'])
            else:result={'ok':False,'error':'linked_pending_requires_new_request'}
            control.update(state=('acked' if result.get('accepted') else 'forwarded') if result.get('ok') else 'rejected',result=result)
            with self.ledger.db() as c:c.execute('UPDATE monolith_controls SET doc=? WHERE id=?',(encoded(control),cid))
    def operation(self,row,name,path,body,receipt_path):
        oid=str(uuid.uuid5(uuid.UUID(row['request_id']),name))
        expected={'path':path,'body':body,'receipt_path':receipt_path}
        with self.ledger.db() as c:
            c.execute('INSERT OR IGNORE INTO monolith_operations VALUES(?,?,?)',(oid,row['id'],encoded({**expected,'state':'prepared'})))
            op=json.loads(c.execute('SELECT doc FROM monolith_operations WHERE id=?',(oid,)).fetchone()[0])
        if any(op[k]!=v for k,v in expected.items()):raise ValueError('monolith_operation_input_changed')
        if op['state']=='acked':return self.call(receipt_path) if name.startswith('claim:') else op['receipt']
        if op['state']=='rejected':raise ValueError('monolith_operation_rejected')
        def save():
            with self.ledger.db() as c:c.execute('UPDATE monolith_operations SET doc=? WHERE id=?',(encoded(op),oid))
        recovering=op['state'] in ('sending','unknown')
        if not recovering:op['state']='sending';save()
        try:
            value=self.call(receipt_path) if recovering else self.call(path,body)
        except Unavailable as e:
            op.update(state='unknown',last_status=e.status);save();raise
        if value is None:
            if not recovering:raise ValueError('monolith_write_receipt_missing')
            if op.get('last_status')==409 and name.startswith('report:'):
                # The explicit 409 plus absent receipt proves this POST rejected.
                op['state']='rejected';save();row['report_conflict']=True;row['phase']='claim_check'
            else:op['state']='prepared';save()
            return None
        if not isinstance(value,dict):raise ValueError('monolith_write_receipt_invalid')
        op.update(state='acked',receipt=value);save();return value
    def engine(self,row):
        if not row.get('engine_job_id'):return None
        with self.s.db() as c:return self.s.owned(c,row['engine_job_id'],row['owner'],row['session'])
    def revoke(self,row):
        if row.get('engine_job_id'):self.s.revoke_monolith_lease(row['engine_job_id'],row['owner'],row['session'])
    def claim_receipt(self,row,value):
        if not isinstance(value,dict) or value.get('assignmentId')!=row['assignment_id'] or value.get('claimId')!=row['claim_id']:
            raise ValueError('monolith_claim_receipt_mismatch')
        job=value.get('assignment')
        if not isinstance(job,dict) or job.get('id')!=row['assignment_id'] or job.get('task_id')!=row['task_id'] or job.get('request_id')!=row['request_id'] or job.get('instruction')!=row['proposal']['instruction'] or job.get('assigned_agent_kind')!='hermes':
            raise ValueError('monolith_assignment_mismatch')
        state=value.get('claimState');row['remote_status']=value.get('status')
        if state!='active':
            self.revoke(row)
            if state=='final':row.update(phase='cancelled' if value.get('status')=='cancelled' else 'blocked',error='monolith_assignment_already_final');return
            engine=self.engine(row)
            if engine and (engine.get('process') or engine.get('inflight') or engine['state']=='uncertain'):
                row.update(phase='claim_check',next_at=self.clock()+15,error='monolith_execution_reconciliation_required');return
            if state not in ('expired','superseded'):raise ValueError('monolith_claim_receipt_invalid')
            if state=='superseded' and value.get('leaseExpiresAt') and epoch(value['leaseExpiresAt'])>self.clock():
                row.update(phase='claim_check',next_at=self.clock()+60);return
            row.update(phase='claim',claim_id=str(uuid.uuid4()),error=None,pending_report=None,report_conflict=False)
            return
        expiry=epoch(value.get('leaseExpiresAt'))
        if row.get('report_conflict'):
            row.update(phase='blocked',error='monolith_report_conflict');self.revoke(row);return
        if expiry<=self.clock()+30:
            self.revoke(row);row.update(phase='claim_check',next_at=self.clock()+15);return
        row.update(lease_expires_at=expiry,remote_status=value['status'],phase='active',claim_checked_at=self.clock())
        lease={'assignment_id':row['assignment_id'],'claim_id':row['claim_id'],'expires_at':expiry}
        if not row.get('engine_job_id'):
            snapshot=job.get('source_snapshot')
            if not isinstance(snapshot,dict) or not isinstance(snapshot.get('task'),dict) or snapshot['task'].get('id')!=row['task_id']:
                raise ValueError('monolith_source_snapshot_invalid')
            context={'source':'monolith','task':snapshot['task'],'project':snapshot.get('project'),'captured_at':epoch(snapshot.get('capturedAt'))}
            if snapshot.get('projectContext') is not None:context['project_context']=snapshot['projectContext']
            kind=row.get('project_kind','acribic')
            if context.get('project') is not None and str(context['project'].get('name','')).casefold()=='acribic':kind='acribic'
            row['project_kind']=kind
            result=self.s.submit(kind,row['goal'],'monolith:'+row['request_id'],row['owner'],row['session'],monolith_context=context,monolith_lease=lease)
            if not result.get('ok'):raise ValueError('monolith_engine_start_rejected')
            row['engine_job_id']=result['job_id']
            self.ledger.remember(result['job_id'],row['proposal']['title'])
        else:
            result=self.s.grant_monolith_lease(row['engine_job_id'],row['owner'],row['session'],lease)
            if not result.get('ok'):raise ValueError('monolith_execution_reconciliation_required')
        self.reconciled.add(row['id'])
    def prepare_report(self,row):
        with self.lock:
            engine=self.engine(row)
            if engine is None:return
            status={'completed':'completed','failed':'failed','interrupted':'interrupted','blocked':'blocked','uncertain':'blocked'}.get(engine['state'],'running')
            signature=[engine['revision'],engine['state'],engine['phase'],engine.get('error')]
            if signature==row.get('last_signature') and self.clock()-row.get('last_report_at',0)<60:return
            report_id=str(uuid.uuid4())
            summary={'running':'Die Projektarbeit läuft.','blocked':'Die Projektarbeit wartet auf eine geprüfte Voraussetzung.','interrupted':'Die Projektarbeit ist angehalten. Der gespeicherte Stand bleibt erhalten.','failed':'Die Projektarbeit ist fehlgeschlagen. Der gespeicherte Stand bleibt erhalten.','completed':'Die geprüften Ergebnisse liegen vor.'}[status]
            body={'reportId':report_id,'claimId':row['claim_id'],'status':status,'summary':summary}
            expected=[]
            if status=='completed':
                result=self.s.result(engine['id'],row['owner'],row['session'])
                if not result.get('ok'):
                    body.update(status='blocked',summary='Der Ergebnisstand muss nach einer Quellenänderung abgeglichen werden.')
                else:
                    body['summary']=result['summary']
                    body['resultText']=result['summary']+('\n\nOffene Punkte:\n'+'\n'.join('- '+v for v in result['uncertainties']) if result['uncertainties'] else '')
                    if len(body['resultText'].encode())>1048576:raise ValueError('monolith_result_text_too_large')
                    body['files']=[];total=0
                    for a in result['artifacts']:
                        data=self.s.artifact_read(a['handle'],row['owner'],row['session'])
                        if not data.get('ok'):raise ValueError('monolith_artifact_read_unavailable')
                        raw=base64.b64decode(data['base64'],validate=True);total+=len(raw)
                        if len(raw)!=a['size'] or hashlib.sha256(raw).hexdigest()!=a['sha256']:raise ValueError('monolith_artifact_changed')
                        if len(raw)>4194304 or total>12582912:raise ValueError('monolith_artifact_too_large')
                        name,mime=a['name'],a['mime']
                        if not isinstance(name,str) or not 1<=len(name.encode('utf-16-le'))//2<=240 or re.search(r'[/\\\x00-\x1f\x7f]',name):raise ValueError('monolith_artifact_name_invalid')
                        if not isinstance(mime,str) or len(mime)>120 or not re.fullmatch(r'[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+',mime):raise ValueError('monolith_artifact_mime_invalid')
                        fid=str(uuid.uuid5(uuid.UUID(report_id),a['handle']))
                        body['files'].append({'id':fid,'name':name,'mediaType':mime,'base64':data['base64']})
                        expected.append({'id':fid,'name':name,'mediaType':mime,'byteLength':len(raw),'sha256':a['sha256']})
            if len(encoded(body).encode())>LIMIT:raise ValueError('monolith_report_transport_too_large')
            if len(body['summary'].encode('utf-16-le'))//2>4000:raise ValueError('monolith_report_summary_too_large')
            row['pending_report']={'id':report_id,'body':body,'signature':signature,'expected_files':expected}
            # The immutable payload and final fence are committed before POST.
            row['phase']='report';self.save(row)
    def step(self,row):
        phase=row['phase']
        if phase in ('published','cancelled','blocked'):return
        if phase=='proposal':
            value=self.operation(row,'proposal','/agent/assignments/propose',row['proposal'],'/agent/assignments/requests/'+row['request_id'])
            if value is None:return
            self.accept_request(row,value);return
        if phase=='approval':
            self.accept_request(row,self.call('/agent/assignments/requests/'+row['request_id']));return
        if row.get('paused') and not row.get('engine_job_id'):return
        if phase=='claim':
            if not row.get('engine_job_id'):
                jobs=self.s.list_owned(row['owner'],row['session']).get('jobs',[])
                if sum(j['state'] in ('queued','working','reviewing','recovering') for j in jobs)>=self.s.policy.get('max_pending',10):
                    row['next_at']=self.clock()+10;return
            row.setdefault('claim_id',str(uuid.uuid4()));self.save(row)
            value=self.operation(row,'claim:'+row['claim_id'],'/agent/assignments/claim',{'assignmentId':row['assignment_id'],'claimId':row['claim_id']},'/agent/assignments/'+row['assignment_id']+'/claims/'+row['claim_id'])
            if value is not None:self.claim_receipt(row,value)
            return
        if phase=='report':
            report=row['pending_report'];body=report['body']
            value=self.operation(row,'report:'+report['id'],'/agent/assignments/'+row['assignment_id']+'/report',body,'/agent/assignments/'+row['assignment_id']+'/reports/'+report['id'])
            if value is None:return
            if value.get('assignmentId')!=row['assignment_id'] or value.get('claimId')!=body['claimId'] or value.get('reportId')!=report['id'] or value.get('status')!=body['status']:
                raise ValueError('monolith_report_receipt_mismatch')
            epoch(value.get('receivedAt'))
            expected={f['id']:{k:v for k,v in f.items() if k!='sha256'} for f in report['expected_files']}
            actual=value.get('files',[])
            if not isinstance(actual,list) or len(actual)!=len(expected) or any(not isinstance(f,dict) for f in actual) or {f.get('id'):f for f in actual}!=expected:raise ValueError('monolith_report_file_receipt_mismatch')
            row.update(last_signature=report['signature'],last_report_at=self.clock(),remote_status=body['status'])
            if body['status']=='completed':row.update(phase='verify',verify_index=0,verify_detail=False,final_report_id=report['id'])
            elif body['status']=='failed':row.update(phase='published',pending_report=None)
            elif body['status']=='interrupted':row.update(phase='paused',pending_report=None)
            else:row.update(phase='claim_check',pending_report=None)
            return
        if phase=='verify':
            report=row['pending_report'];files=report['expected_files'];index=row['verify_index']
            if not row.get('verify_detail'):
                value=self.call('/agent/assignments/'+row['assignment_id'])
                if not isinstance(value,dict) or value.get('assignment',{}).get('id')!=row['assignment_id']:raise ValueError('monolith_result_readback_mismatch')
                matches=[r for r in value.get('reports',[]) if isinstance(r,dict) and r.get('reportId')==report['id']]
                if len(matches)!=1 or matches[0].get('resultText')!=report['body'].get('resultText') or matches[0].get('summary')!=report['body']['summary']:raise ValueError('monolith_result_text_readback_mismatch')
                row['verify_detail']=True;return
            if index<len(files):
                f=files[index];value=self.call('/agent/assignments/'+row['assignment_id']+'/results/'+report['id']+'/files/'+f['id'])
                if not isinstance(value,dict) or any(value.get(k)!=f[k] for k in ('id','name','mediaType')):raise ValueError('monolith_result_file_readback_mismatch')
                raw=base64.b64decode(value.get('base64',''),validate=True)
                if len(raw)!=f['byteLength'] or hashlib.sha256(raw).hexdigest()!=f['sha256']:raise ValueError('monolith_result_file_readback_mismatch')
                row['verify_index']+=1;return
            row.update(phase='published',pending_report=None);return
        if phase=='paused':
            engine=self.engine(row)
            if engine and engine['state']=='queued' and not row['paused']:
                row.update(phase='claim_check')
            return
        if phase=='claim_check' or row['id'] not in self.reconciled:
            value=self.call('/agent/assignments/'+row['assignment_id']+'/claims/'+row['claim_id'])
            if value is None:raise ValueError('monolith_saved_claim_missing')
            self.claim_receipt(row,value);return
        if phase=='active':
            if self.clock()-row.get('claim_checked_at',0)>=30:
                row['phase']='claim_check';return
            self.prepare_report(row)
    def accept_request(self,row,value):
        if not isinstance(value,dict) or value.get('requestId')!=row['request_id']:raise ValueError('monolith_request_receipt_mismatch')
        action=uid(value.get('actionId'))
        if row.get('action_id') not in (None,action):raise ValueError('monolith_request_receipt_mismatch')
        row.update(action_id=action,remote_status=value.get('status'),phase='approval')
        assignment=value.get('assignmentId')
        if assignment is None:
            if value.get('status') in FINAL:row.update(phase='cancelled')
            elif value.get('status') not in ('awaiting_approval','awaiting_execution'):raise ValueError('monolith_request_receipt_invalid')
            row['next_at']=self.clock()+60;return
        if uid(assignment)!=action:raise ValueError('monolith_assignment_mismatch')
        row.update(assignment_id=assignment)
        if value.get('status') in FINAL:row.update(phase='cancelled',error='monolith_assignment_already_final')
        elif not row['paused']:row.update(phase='claim',next_at=0)
        else:row['next_at']=self.clock()+60
    def poll_once(self):
        with self.ledger.db() as c:
            controls={link for link,doc in c.execute('SELECT link,doc FROM monolith_controls') if json.loads(doc)['state'] in ('pending','forwarded')}
        rows=[r for r in self.rows() if r['id'] in controls or r['phase'] not in ('published','cancelled','blocked') and r.get('next_at',0)<=self.clock()]
        discover=self.s.policy.get('monolith_task_handoff_enabled') is True and self.clock()>=self.discovery_after
        if not rows and not discover:return
        fd=os.open(self.ledger.root/'monolith-sync.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            deadline=time.monotonic()+20
            for original in rows:
                if time.monotonic()>=deadline:break
                row=self.find(original['id'])
                if row is None:continue
                try:
                    self.apply_controls(row)
                    self.step(row);row.update(failures=0)
                except Unavailable as e:
                    row['failures']+=1;row['error']='monolith_http_unavailable' if e.status is None else 'monolith_http_'+str(e.status)
                    row['next_at']=self.clock()+min(300,15*2**min(row['failures']-1,5))
                    if e.status in (400,401,403,404,413):
                        row.update(error_phase=row['phase'],phase='blocked');self.revoke(row)
                except Exception:
                    row.update(error_phase=row['phase'],phase='blocked',error='monolith_sync_reconciliation_required')
                    self.revoke(row)
                self.save(row)
            if discover and time.monotonic()<deadline:self.discover()
        finally:os.close(fd)
