"""Existing reply drafting, existing native phases, no extra model or N gate."""
from email.message import EmailMessage
from email.utils import getaddresses,parseaddr
import json
from background_jobs.contracts import ContractError,canonical,digest,approved_path
from background_jobs.outreach_candidate import content
from .host_runtime import ACCOUNTS
from .write_boundary import save


def source(watcher,svc,alias,mid):
    if svc.users().getProfile(userId='me').execute().get('emailAddress','').casefold()!=ACCOUNTS[alias]:
        raise ContractError('native_reply_account_changed')
    raw=svc.users().messages().get(userId='me',id=mid,format='raw').execute()
    if raw.get('id')!=mid or not raw.get('raw')or not raw.get('threadId'):raise ContractError('native_reply_raw_source_required')
    import email_realtime
    if raw['threadId']in email_realtime.CLOSED_THREADS.get(alias,set()):
        raise ContractError('native_reply_closed_conversation')
    thread=svc.users().threads().get(userId='me',id=raw['threadId'],format='full').execute()
    messages=[m for m in thread.get('messages',[])if'DRAFT'not in m.get('labelIds',[])]
    watcher.require_latest_source(messages,mid)
    if not messages or'SENT'in messages[-1].get('labelIds',[]):raise ContractError('native_reply_latest_inbound_required')
    # Reading a message changes UNREAD and historyId together. Neither changes
    # the business source. Preserve the complete RFC raw, MIME and ordering.
    clean=lambda m:{k:([v for v in val if v!='UNREAD']if k=='labelIds'else val)
                    for k,val in m.items()if k!='historyId'}
    original={'account':ACCOUNTS[alias],'alias':alias,'message':clean(raw),
              'thread':{'id':raw['threadId'],'messages':[clean(m)for m in messages]}}
    if any(m.get('threadId')!=raw['threadId']for m in messages):raise ContractError('native_reply_thread_identity_changed')
    return original,thread


def plan(watcher,original,text,*,to,cc,slots,project):
    import mail_policy
    latest=original['thread']['messages'][-1];h=watcher.headers(latest)
    recipient=parseaddr(h.get('reply-to')or h.get('from',''))[1].casefold()
    recipients=[a.casefold()for _,a in getaddresses(to)]if to is not None else[recipient]
    copied=[a.casefold()for _,a in getaddresses(cc)]if cc is not None else[]
    subject=h.get('subject','');subject=subject if subject.casefold().startswith(('re:','aw:'))else'Re: '+subject
    parent=h.get('message-id','');refs=(h.get('references','')+' '+parent).strip()if parent else''
    body=mail_policy.format_body(text,slots);message=EmailMessage();message.set_content(body)
    message.add_alternative(mail_policy.body_html(body),subtype='html')
    return {'operation':'users.drafts.create','to':recipients,'cc':copied,'subject':subject,
            'thread_id':original['thread']['id'],'parts':content(message.as_bytes())['parts'],
            'required_headers':{'in-reply-to':parent,'references':refs},'reply_source_sha256':digest(original),
            'slots':list(slots),'project':project}


class LocalPhase:
    """Source/decision originals already persisted by the existing caller."""
    def __init__(self,root,identity,ref,sha):self.root,self.identity,self.ref,self.sha=root,identity,ref,sha
    def readback(self,identity):
        from gtm.coordinator import Readback
        valid=identity==self.identity and digest(approved_path(self.root,self.ref).read_bytes())==self.sha
        return Readback('verified'if valid else'conflict',identity,self.ref if valid else None)
    def preflight(self,identity):raise ContractError('local_original_phase_never_writes')
    def execute_once(self,identity):raise ContractError('local_original_phase_never_writes')


class ReplyDraft:
    def __init__(self,runtime,watcher,identity,original,approved,args):
        self.runtime,self.watcher,self.identity=runtime,watcher,identity
        self.original,self.approved,self.args=original,approved,args;self.scope=None
    def bind_native_write_scope(self,scope):
        self.runtime.check_scope(scope)
        if scope.identity!=self.identity.fields()or scope.phase!='draft':raise ContractError('native_reply_scope_changed')
        self.scope=scope
    def source_gate(self,scope=None,plan=None):
        actual,thread=source(self.watcher,self.watcher.service(self.original['alias']),self.original['alias'],self.original['message']['id'])
        if actual!=self.original:raise self.watcher.SourceChangedBeforeDraft('source_changed_before_draft')
        if any('DRAFT'in m.get('labelIds',[])for m in thread.get('messages',[])):
            raise ContractError('native_existing_draft_needs_review')
        if scope is not None:return self.runtime.capture_sources(scope,plan)
    def readback(self,identity):
        from gtm.coordinator import Readback
        if identity!=self.identity:return Readback('conflict',identity)
        scope=self.runtime.recovery_scope(identity,'draft');b=self.runtime.boundary
        if (b.root/b.intent_ref(scope)).exists():
            result=b.readback_owned(scope,self.watcher.service(self.original['alias']))
            return Readback(result['state'],identity,result.get('draft_id'))
        self.source_gate();return Readback('absent',identity)
    def preflight(self,identity):
        self.runtime.boundary.check_enforcement();self.source_gate()
    def execute_once(self,identity):
        if self.scope is None:raise ContractError('native_reply_scope_required')
        self.runtime.check_scope(self.scope)
        with self.runtime.boundary.session(self.scope,self.approved,source_gate=self.source_gate):
            self.watcher._make_draft_owned(**self.args)
            result=self.runtime.boundary.readback_owned(self.scope,self.watcher.service(self.original['alias']))
            if result['state']!='verified':raise self.watcher.DraftStateUnverified('native owned draft readback unverified')


def recover_existing(runtime,watcher,alias,mid,text,*,to,cc,slots,project,_attempt):
    """Find the old native intent before consulting current inbox freshness.

    The stable workflow key omits source_hash by the existing Identity contract;
    its actual immutable hash is loaded from gtm_sources, never fabricated.
    This branch can only verify the already attempted exact request.
    """
    from gtm.coordinator import Identity
    fields=dict(category='acribic'if alias=='code'else'stealth',account=ACCOUNTS[alias],kind='reply',
                source_id='reply-draft:'+alias+':'+mid,version=1)
    workflow=digest(json.dumps(fields,sort_keys=True).encode());co=runtime.coordinator;root=runtime.boundary.root
    with co.store.connect()as c:
        row=c.execute('SELECT identity FROM gtm_sources WHERE workflow_id=?',(workflow,)).fetchone()
    if row is None:return None
    identity=Identity(**json.loads(row[0])).checked()
    if identity.key!=workflow:raise ContractError('native_reply_recovery_identity_changed')
    scope=runtime.recovery_scope(identity,'draft')
    if not(root/runtime.boundary.intent_ref(scope)).exists():return None
    status=co.status(identity)
    with co.store.connect()as c:
        attempted=c.execute('SELECT 1 FROM gtm_attempts WHERE phase_key=?',(co._phase_key(identity,'draft'),)).fetchone()
    if not attempted or any(status['phases'].get(p)!='completed'for p in ('source','classification')):
        raise ContractError('native_reply_recovery_original_phase_required')
    original=json.loads(approved_path(root,'native-reply-sources/'+workflow+'.json').read_bytes())
    decision=json.loads(approved_path(root,'native-reply-decisions/'+workflow+'.json').read_bytes())
    expected={'text':text,'to':to,'cc':cc,'slots':list(slots),'project':project}
    if (digest(original)!=identity.source_hash or original.get('alias')!=alias or original.get('account')!=identity.account or
        original.get('message',{}).get('id')!=mid or decision.get('identity')!=identity.fields()or
        any(decision.get(k)!=v for k,v in expected.items())):
        raise ContractError('native_reply_recovery_original_changed')
    approved=decision['plan']
    if approved.get('reply_source_sha256')!=identity.source_hash or approved.get('thread_id')!=original['thread']['id']:
        raise ContractError('native_reply_recovery_plan_changed')
    intent=runtime.boundary.intent(scope)
    request,raw,complete=runtime.boundary._request({'scope':scope,'plan':approved},intent['operation'],intent['request'])
    if digest(request)!=intent['request_sha256']or digest(raw)!=intent['raw_sha256']or digest(complete)!=intent['content_sha256']:
        raise ContractError('native_reply_recovery_intent_changed')
    args=dict(alias=alias,mid=mid,**expected,_attempt=_attempt)
    adapter=ReplyDraft(runtime,watcher,identity,original,approved,args)
    proof=adapter.readback(identity)
    if proof.state!='verified':raise watcher.DraftStateUnverified('native reply recovery remains readback only')
    lifecycle=co.recover_verified(identity,'draft',adapter)
    return {'status':'created','account':alias,'draftId':proof.destination_id,'threadId':original['thread']['id'],
            'to':approved['to']if to is not None else approved['to'][0],'native_workflow_id':workflow,
            'readback_only':True,'native_phase_state':lifecycle['state']}


def run(runtime,watcher,alias,mid,text,*,to=None,cc=None,slots=(),project=None,_attempt=None):
    from gtm.coordinator import Identity
    recovered=recover_existing(runtime,watcher,alias,mid,text,to=to,cc=cc,slots=slots,project=project,_attempt=_attempt)
    if recovered is not None:return recovered
    original,thread=source(watcher,watcher.service(alias),alias,mid)
    identity=Identity('acribic'if alias=='code'else'stealth',ACCOUNTS[alias],'reply','reply-draft:'+alias+':'+mid,1,digest(original))
    approved=plan(watcher,original,text,to=to,cc=cc,slots=slots,project=project)
    root=runtime.boundary.root
    source_ref,source_sha=save(root,'native-reply-sources/'+identity.key+'.json',original)
    decision_ref,decision_sha=save(root,'native-reply-decisions/'+identity.key+'.json',
        {'identity':identity.fields(),'plan':approved,'text':text,'to':to,'cc':cc,'slots':list(slots),'project':project})
    co=runtime.coordinator;co.plan(identity,reply_phases=('source','classification','draft'))
    for phase,ref,sha in [('source',source_ref,source_sha),('classification',decision_ref,decision_sha)]:
        result=co.run_phase(identity,phase,LocalPhase(root,identity,ref,sha))
        if result['state']!='completed':raise ContractError('native_reply_original_phase_not_completed')
    args=dict(alias=alias,mid=mid,text=text,to=to,cc=cc,slots=slots,project=project,_attempt=_attempt)
    adapter=ReplyDraft(runtime,watcher,identity,original,approved,args)
    result=co.run_phase(identity,'draft',adapter)
    if result['state']=='verified_result_recovery_required':
        result=co.recover_verified(identity,'draft',adapter)
    if result['state']!='completed':raise watcher.DraftStateUnverified('native reply phase requires owned readback')
    proof=adapter.readback(identity)
    if proof.state!='verified':raise watcher.DraftStateUnverified('native reply destination changed')
    return {'status':'created','account':alias,'draftId':proof.destination_id,'threadId':original['thread']['id'],
            'to':approved['to']if to is not None else approved['to'][0],'native_workflow_id':identity.key}
