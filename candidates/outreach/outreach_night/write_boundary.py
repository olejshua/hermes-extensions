"""One VPS account boundary over existing native jobs and exact request intents.

This owns no job queue or scheduler. A process-local session carries the actual
native fence. An intent is durable before one provider call; a cold session can
only read back an existing intent, even when its SSH caller vanished.
"""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime,timezone
from email import policy as email_policy
from email.parser import BytesParser
import fcntl
import json
import os
from pathlib import Path
import threading
import time

from background_jobs.contracts import ContractError,PermanentFailure,approved_path,canonical,digest,private_file,private_dir
from background_jobs.outreach_candidate import content,addresses
from .write_scope import NativeWriteScope

SESSION=ContextVar('outreach_existing_native_write_session_v2',default=None)
MUTATIONS={'users.drafts.create','users.drafts.update','users.messages.send','users.drafts.send'}


def save(root,ref,value):
    raw=canonical(value).encode()
    try:private_file(Path(root)/ref,raw)
    except FileExistsError:
        if approved_path(root,ref).read_bytes()!=raw:raise ContractError('native_write_original_changed')
    return ref,digest(raw)


class NativeWriteBoundary:
    def __init__(self,root,*,host_id,enforcer,allowed_native_databases=None):
        self.root=Path(root);self.host_id=host_id;self.enforcer=enforcer
        self.allowed=None if allowed_native_databases is None else frozenset(str(Path(p).resolve())for p in allowed_native_databases)
        private_dir(self.root)
        self.locks=self.root/'account-locks';private_dir(self.locks)
    def key(self,scope):return digest([str(scope.lifecycle.db_path),scope.native_id])
    def intent_ref(self,scope):return 'native-write-intents/'+self.key(scope)+'.json'
    def check_enforcement(self):
        value=self.enforcer.verify();self.enforcer.check_original(value)
        if value.get('host_id')!=self.host_id:raise ContractError('native_write_host_changed')
        return value
    @contextmanager
    def session(self,scope,plan,*,source_gate):
        if not isinstance(scope,NativeWriteScope)or not callable(source_gate):raise ContractError('actual_native_write_scope_required')
        current=SESSION.get()
        if current is not None:
            if current['boundary']is not self or current['scope'].binding!=scope.binding or current['plan']!=plan:
                raise ContractError('native_write_session_scope_changed')
            self.check_session(current);yield current;return
        scope.check();proof=self.check_enforcement()
        route=('reply'if scope.identity.get('kind')=='reply'else'call_followup'if scope.kind=='gtm'else'outreach')
        if route not in proof.get('supported_routes',[]):raise ContractError('native_writer_route_not_enforced')
        path=self.locks/(digest(scope.account.encode())+'.lock')
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        stop=threading.Event();lost=threading.Event()
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ContractError('native_account_writer_busy')from None
            stat=os.fstat(fd)
            record={'boundary':self,'scope':scope,'plan':json.loads(canonical(plan)),'source_gate':source_gate,
                    'fd':fd,'lock_path':path,'lock_identity':(stat.st_dev,stat.st_ino),'enforcement':proof,'lost':lost}
            def heartbeat():
                while not stop.wait(10):
                    try:scope.lifecycle.heartbeat(scope.native_id,owner=scope.owner,lease_generation=scope.generation,lease_seconds=60)
                    except Exception:lost.set();return
            thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
            token=SESSION.set(record)
            try:self.check_session(record);yield record
            finally:SESSION.reset(token);stop.set();thread.join(timeout=1)
        finally:
            stop.set();fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
    def check_session(self,record):
        if SESSION.get()is not record or record['boundary']is not self or record['lost'].is_set():
            raise ContractError('native_write_session_not_active')
        if self.allowed is not None and str(Path(record['scope'].lifecycle.db_path).resolve())not in self.allowed:
            raise ContractError('unregistered_native_database')
        record['scope'].check();self.check_enforcement()
        live=record['lock_path'].stat();held=os.fstat(record['fd'])
        if (live.st_dev,live.st_ino)!=(held.st_dev,held.st_ino)or (held.st_dev,held.st_ino)!=record['lock_identity']:
            raise ContractError('native_account_lock_replaced')
        return record
    def _request(self,record,operation,kwargs):
        scope=record['scope'];plan=record['plan']
        if operation not in MUTATIONS or operation!=plan.get('operation'):raise ContractError('native_mutation_not_in_scope')
        # No existing automatic Reply/Outreach scope authorizes a Send. A future
        # Send adapter must provide an actual native admitted mode/receipt.
        if operation in {'users.messages.send','users.drafts.send'}:
            raise PermanentFailure('native_authentic_send_admission_not_available')
        if set(kwargs)!={'userId','body'}and not (operation=='users.drafts.update'and set(kwargs)=={'userId','body','id'}):
            raise ContractError('native_exact_mutation_parameters_required')
        if kwargs['userId']!='me':raise ContractError('native_exact_account_required')
        if operation=='users.drafts.update'and kwargs['id']!=plan.get('draft_id'):
            raise ContractError('native_owned_update_target_changed')
        body=json.loads(canonical(kwargs['body']));message=body.get('message',{})
        if set(body)!={'message'}or not isinstance(message,dict)or not {'raw'}<=set(message)<={'raw','threadId'}:
            raise ContractError('native_exact_raw_mime_required')
        try:raw=base64.b64decode(message['raw']+'='*(-len(message['raw'])%4),altchars=b'-_',validate=True)
        except (ValueError,TypeError):raise ContractError('native_exact_raw_mime_required')from None
        parsed=BytesParser(policy=email_policy.default).parsebytes(raw)
        if parsed.defects:raise ContractError('native_exact_raw_mime_required')
        if not parsed.get('From'):parsed['From']=scope.account
        if not parsed.get('Message-ID'):parsed['Message-ID']='<native-mail-'+self.key(scope)+'@hermes.local>'
        raw=parsed.as_bytes();complete=content(raw)
        if any(len(parsed.get_all(k,[]))>1 or str(parsed.get(k,'')).strip()!=v for k,v in plan.get('required_headers',{}).items()):
            raise ContractError('native_approved_reply_parent_changed')
        if (addresses(parsed,'From')!=[scope.account]or addresses(parsed,'To')!=plan.get('to')or
            addresses(parsed,'Cc')!=plan.get('cc',[])or addresses(parsed,'Bcc')or
            complete['headers']['subject']!=plan.get('subject')or
            message.get('threadId')!=plan.get('thread_id')or
            complete['parts']!=plan.get('parts')):
            raise ContractError('native_approved_draft_content_changed')
        message['raw']=base64.urlsafe_b64encode(raw).decode()
        request={**kwargs,'body':body}
        return request,raw,complete
    def execute(self,account,operation,kwargs,provider):
        record=SESSION.get()
        if record is None:raise ContractError('native_write_scope_required')
        self.check_session(record)
        if record['scope'].account!=account:raise ContractError('native_write_account_changed')
        request,raw,complete=self._request(record,operation,kwargs);scope=record['scope']
        intent={'contract':'native-existing-job-write-intent-v2','scope':scope.binding,'operation':operation,
                'request':request,'request_sha256':digest(request),'raw_sha256':digest(raw),'content_sha256':digest(complete),
                'message_id':complete['headers']['message-id'],'required_headers':record['plan'].get('required_headers',{}),'created_at':datetime.now(timezone.utc).isoformat()}
        ref=self.intent_ref(scope)
        path=self.root/ref
        if path.exists():
            old=json.loads(approved_path(self.root,ref).read_bytes())
            if old['operation']!=operation or old['request_sha256']!=intent['request_sha256']:
                raise ContractError('native_existing_intent_content_changed')
            raise PermanentFailure('native_attempt_readback_only')
        # Source freshness is checked while the actual VPS account lock is held,
        # immediately before durable intent and the provider call.
        source_version=record['source_gate'](scope,record['plan']);self.check_session(record)
        if scope.kind=='background'or(scope.kind=='gtm'and scope.identity.get('kind')in {'reply','call'}):
            project=scope.identity['item']['category']if scope.kind=='background'else scope.identity['category']
            required={'project','account','revision','entry_sha256','source_original_ref','source_original_sha256'}
            if (not isinstance(source_version,dict)or set(source_version)!=required or
                source_version['project']!=project or source_version['account']!=scope.account or
                type(source_version['revision'])is not int or source_version['revision']<1):
                raise ContractError('central_actual_reply_source_version_required')
            original_raw=approved_path(self.root,source_version['source_original_ref']).read_bytes()
            if digest(original_raw)!=source_version['source_original_sha256']:
                raise ContractError('central_reply_source_original_changed')
            original=json.loads(original_raw)['original']
            if any(original.get(k)!=source_version[k]for k in ('project','account','revision','entry_sha256')):
                raise ContractError('central_reply_source_version_changed')
            for entry in original['changes']:
                if digest(approved_path(self.root,entry['ref']).read_bytes())!=entry['sha256']:
                    raise ContractError('central_source_original_changed')
            intent['source_version']=source_version
        private_file(path,canonical(intent).encode())
        self.check_session(record)
        ack_ref='native-write-acks/'+self.key(scope)+'.json'
        try:
            # No SDK retry kwargs, fallback provider or Mac-side SDK mutation.
            ack=provider(request)
            save(self.root,ack_ref,{'intent_ref':ref,'request_sha256':intent['request_sha256'],'response':ack})
            return ack
        except BaseException:
            # The durable intent is the unknown-outcome fence. Releasing this
            # process lock cannot make a second Create permissible.
            raise
    def intent(self,scope):
        value=json.loads(approved_path(self.root,self.intent_ref(scope)).read_bytes())
        stable={k:v for k,v in scope.binding.items()if k not in {'owner','generation'}}
        if (value.get('contract')!='native-existing-job-write-intent-v2'or
            {k:v for k,v in value.get('scope',{}).items()if k not in {'owner','generation'}}!=stable):
            raise ContractError('native_owned_intent_changed')
        return value
    def readback_owned(self,scope,gmail):
        """Readback-only is available after cancellation/lease expiry/SSH loss."""
        intent=self.intent(scope)
        actual=gmail.users().getProfile(userId='me').execute()
        if actual.get('emailAddress')!=scope.account:raise ContractError('native_owned_account_changed')
        ack=self.root/'native-write-acks'/(self.key(scope)+'.json')
        ack_message={}
        if ack.exists():
            result=json.loads(approved_path(self.root,str(ack.relative_to(self.root))).read_bytes())
            if result['request_sha256']!=intent['request_sha256']:raise ContractError('native_owned_intent_changed')
            ids=[result['response'].get('id')]
            ack_message=result['response'].get('message',{})
            if not isinstance(ack_message,dict):raise ContractError('native_owned_ack_message_changed')
        else:
            page=None;tokens=set();ids=[]
            while True:
                result=gmail.users().drafts().list(userId='me',q='rfc822msgid:'+intent['message_id'],maxResults=100,pageToken=page).execute()
                for row in result.get('drafts',[]):
                    if row.get('id')in ids:raise ContractError('native_owned_draft_ambiguous')
                    ids.append(row.get('id'))
                page=result.get('nextPageToken')
                if not page:break
                if page in tokens or len(tokens)>=100:raise ContractError('native_owned_readback_pagination')
                tokens.add(page)
        expected_target=intent['request'].get('id')if intent['operation']=='users.drafts.update'else None
        if expected_target is not None and ids!=[expected_target]:
            return {'state':'conflict','write_replayed':False,'fully_ready':False}
        if len(ids)!=1 or not isinstance(ids[0],str)or not ids[0]:return {'state':'unknown','write_replayed':False,'fully_ready':False}
        result=gmail.users().drafts().get(userId='me',id=ids[0],format='raw').execute();message=result.get('message',{})
        expected_thread=intent['request']['body']['message'].get('threadId')
        acknowledged_thread=ack_message.get('threadId');acknowledged_message=ack_message.get('id')
        if (result.get('id')!=ids[0]or not isinstance(message.get('id'),str)or not message['id']or
            not isinstance(message.get('threadId'),str)or not message['threadId']or
            (expected_thread is not None and message['threadId']!=expected_thread)or
            (acknowledged_thread is not None and message['threadId']!=acknowledged_thread)or
            (acknowledged_message is not None and message['id']!=acknowledged_message)or
            (expected_target is not None and result.get('id')!=expected_target)or
            'DRAFT'not in message.get('labelIds',[])or 'SENT'in message.get('labelIds',[])):
            return {'state':'conflict','write_replayed':False,'fully_ready':False}
        try:raw=base64.b64decode(message['raw']+'='*(-len(message['raw'])%4),altchars=b'-_',validate=True)
        except (ValueError,KeyError,TypeError):raise ContractError('native_owned_raw_readback_required')from None
        parsed=BytesParser(policy=email_policy.default).parsebytes(raw)
        if any(len(parsed.get_all(k,[]))>1 or str(parsed.get(k,'')).strip()!=v for k,v in intent.get('required_headers',{}).items()):
            return {'state':'conflict','write_replayed':False,'fully_ready':False}
        if digest(content(raw))!=intent['content_sha256']:return {'state':'conflict','write_replayed':False,'fully_ready':False}
        ref,sha=save(self.root,'native-write-readbacks/'+self.key(scope)+'.json',
                     {'intent_ref':self.intent_ref(scope),'request_sha256':intent['request_sha256'],'response':result,
                      'raw_sha256':digest(raw),'content_sha256':intent['content_sha256']})
        return {'state':'verified','draft_id':ids[0],'readback_ref':ref,'readback_sha256':sha,'write_replayed':False,
                'owned_target_verified':True,'fully_ready':False}
