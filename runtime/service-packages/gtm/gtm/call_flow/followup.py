"""One source-grounded, unsent follow-up using the existing managed mail boundary."""
import base64
from email.message import EmailMessage
from email.utils import getaddresses
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata

MAIL_PYTHON='/home/hermes/.hermes/hermes-agent/venv/bin/python3'
ACCOUNTS={'acribic':'operator@primary.example.invalid','stealth':'operator@secondary.example.invalid'}


def helper(action,**payload):
    result=subprocess.run([MAIL_PYTHON,str(Path(__file__).resolve()),action],input=json.dumps(payload),
                          text=True,capture_output=True,timeout=180)
    if result.returncode:raise ValueError('call_mail_helper_unavailable')
    try:value=json.loads(result.stdout)
    except ValueError:raise ValueError('call_mail_helper_invalid_response') from None
    if value.get('error'):raise ValueError('call_mail_helper_unavailable')
    return value


def context(event,category):return helper('context',event=event,category=category)


def plan(raw,identity,binding,validate_evidence):
    value=raw.get('followup_email')
    if value is None:return None  # Already saved legacy analyses remain immutable.
    if not isinstance(value,dict) or value.get('decision')not in ('none','draft','review') or not isinstance(value.get('reason'),str) or not value['reason'].strip():
        raise ValueError('followup_email_decision_required')
    if value['decision']!='draft':
        if any(value.get(k)for k in ('body','subject','thread_id','recipients','slots')):raise ValueError('followup_no_draft_with_content')
        return dict(decision=value['decision'],reason=value['reason'])
    evidence=value.get('evidence')
    if not isinstance(evidence,list) or not evidence:raise ValueError('followup_source_evidence_required')
    for item in evidence:validate_evidence(item)
    ctx=binding.get('mail_context')
    if not isinstance(ctx,dict) or ctx.get('account')!=identity.account:raise ValueError('followup_mail_context_missing')
    thread=value.get('thread_id');recipients=value.get('recipients');subject=value.get('subject');body=value.get('body')
    if (not isinstance(thread,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',thread)
            or not isinstance(recipients,list) or not 1<=len(recipients)<=5 or any(not isinstance(x,str)for x in recipients)
            or len(set(recipients))!=len(recipients) or not set(recipients)<=set(ctx.get('external_emails',[]))
            or not isinstance(subject,str) or not 1<=len(subject)<=240 or any(c in subject for c in '\r\n')
            or not isinstance(body,str) or not 1<=len(body)<=10000):raise ValueError('followup_exact_thread_and_recipients_required')
    messages=[m for m in ctx.get('messages',[])if m.get('thread_id')==thread and 'DRAFT'not in m.get('labels',[])]
    if not messages or any(not any(e==m.get('author')or e in m.get('recipients',[])for m in messages)for e in recipients):
        raise ValueError('followup_existing_correspondence_required')
    slots=value.get('slots',[])
    if not isinstance(slots,list) or len(slots)>8 or any(not isinstance(x,str)for x in slots):raise ValueError('followup_slots_invalid')
    # Subject is authored too; the mail helper formats and verifies both MIME parts.
    subject=''.join('-'if unicodedata.category(c)=='Pd' or c in '\u2212\u2043' else c for c in subject)
    if re.search(r'Berliner\s+Zeit|Europe/Berlin',subject,re.I):raise ValueError('followup_subject_timezone_prose')
    return dict(decision='draft',reason=value['reason'],account=identity.account,category=identity.category,
                workflow_id=identity.key,source_hash=identity.source_hash,thread_id=thread,recipients=recipients,
                subject=subject,body=body,slots=slots)


class DraftAdapter:
    def __init__(self,identity,value,directory):self.identity=identity;self.value=value;self.receipt=Path(directory)/'followup-draft.json'
    def readback(self,identity):
        from gtm.coordinator import Readback
        if identity!=self.identity:raise ValueError('call_followup_identity_changed')
        if self.value['decision']=='none':return Readback('verified',identity,'not_applicable_no_followup_needed')
        if self.value['decision']=='review':return Readback('unknown',identity)
        known=json.loads(self.receipt.read_text()).get('draft_id') if self.receipt.exists() else None
        result=helper('readback',plan=self.value,known_draft_id=known)
        return Readback(result['state'],identity,result.get('draft_id'))
    def preflight(self,identity):
        if self.readback(identity).state!='absent':raise ValueError('followup_draft_not_absent')
        result=helper('preflight',plan=self.value)
        if result.get('ready')is not True:
            if result.get('reason')=='closed_conversation':raise ValueError('followup_closed_conversation')
            raise ValueError('followup_preflight_unverified')
    def execute_once(self,identity):
        result=helper('write',plan=self.value)
        if result.get('draft_id'):
            body=json.dumps(result,sort_keys=True).encode()
            fd=os.open(self.receipt,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as out:out.write(body);out.flush();os.fsync(out.fileno())


def mail_service(category):
    import email_watcher
    alias={'acribic':'code','stealth':'stealth'}[category]
    svc=email_watcher.service(alias)
    if svc.users().getProfile(userId='me').execute().get('emailAddress','').casefold()!=ACCOUNTS[category]:
        raise ValueError('mailbox_identity_mismatch')
    return svc


def formatted(value):
    import mail_policy
    return mail_policy.format_body(value['body'],value['slots'])


def draft_matches(draft,value):
    import mail_policy
    message=draft.get('message',{});payload=message.get('payload',{})
    headers={h['name'].casefold():h['value']for h in payload.get('headers',[])}
    labels=set(message.get('labelIds',[]))
    return (message.get('threadId')==value['thread_id'] and 'DRAFT'in labels and 'SENT'not in labels
            and headers.get('x-hermes-call')==value['workflow_id']
            and [a.casefold()for _,a in getaddresses([headers.get('from','')])]==[value['account']]
            and sorted(a.casefold()for _,a in getaddresses([headers.get('to','')]))==sorted(value['recipients'])
            and not headers.get('cc') and not headers.get('bcc') and headers.get('subject')==value['subject']
            and mail_policy.mime_body_matches(payload,formatted(value),mail_policy.body_html(formatted(value))))


def readback(svc,value,known_draft_id=None):
    if known_draft_id:
        draft=svc.users().drafts().get(userId='me',id=known_draft_id,format='full').execute()
        return dict(state='verified'if draft_matches(draft,value)else'conflict',draft_id=known_draft_id)
    mid='hermes-call-'+value['workflow_id']+'@hermes.local'
    result=svc.users().drafts().list(userId='me',q='rfc822msgid:'+mid,maxResults=10).execute()
    if result.get('nextPageToken'):return dict(state='unknown')
    ids=result.get('drafts',[])
    if not ids:return dict(state='absent')
    if len(ids)!=1:return dict(state='conflict')
    draft=svc.users().drafts().get(userId='me',id=ids[0]['id'],format='full').execute()
    return dict(state='verified'if draft_matches(draft,value)else'conflict',draft_id=ids[0]['id'])


def preflight(svc,value):
    import email_realtime
    closed=getattr(email_realtime,'CLOSED_THREADS',None)
    if not isinstance(closed,dict):raise ValueError('followup_global_mail_policy_unavailable')
    alias={'acribic':'code','stealth':'stealth'}[value['category']]
    if value['thread_id']in closed.get(alias,set()):return dict(ready=False,reason='closed_conversation')
    formatted(value)  # Full managed date/time and formatting validation before any write.
    thread=svc.users().threads().get(userId='me',id=value['thread_id'],format='full').execute()
    messages=thread.get('messages',[])
    if not messages or any('DRAFT'in m.get('labelIds',[])for m in messages):return dict(ready=False,reason='existing_draft_needs_review')
    external=set()
    for message in messages:
        for h in message.get('payload',{}).get('headers',[]):
            if h['name'].casefold()in ('from','to','cc'):
                external.update(a.casefold()for _,a in getaddresses([h['value']]))
    if not set(value['recipients'])<=external:return dict(ready=False,reason='recipient_not_in_original_thread')
    return dict(ready=True,thread=thread)


def write(svc,value):
    from contextlib import nullcontext
    import mail_policy
    current=readback(svc,value)
    if current['state']!='absent':return current
    check=preflight(svc,value)
    if not check['ready']:return dict(state='conflict')
    body=formatted(value);message=EmailMessage()
    message['From']=value['account'];message['To']=', '.join(value['recipients']);message['Subject']=value['subject']
    message['Message-ID']='<hermes-call-'+value['workflow_id']+'@hermes.local>'
    message['X-Hermes-Call']=value['workflow_id']
    last=check['thread']['messages'][-1]
    headers={h['name'].casefold():h['value']for h in last.get('payload',{}).get('headers',[])}
    parent=headers.get('message-id')
    if not parent or '\n'in parent or '\r'in parent:raise ValueError('reply_parent_message_id_missing')
    message['In-Reply-To']=parent;message['References']=parent
    message.set_content(body);message.add_alternative(mail_policy.body_html(body),subtype='html')
    guard=nullcontext()
    if value['slots']:
        import email_realtime
        guard=mail_policy.scheduling(email_realtime.calendar_service(),value['account'],value['thread_id'],value['recipients'],value['slots'])
    with guard:
        # Coordinator commits the one write attempt before this subprocess.
        # A lost create result is recovered only by readback, never another create.
        try:
            created=svc.users().drafts().create(userId='me',body={'message':{'threadId':value['thread_id'],'raw':base64.urlsafe_b64encode(message.as_bytes()).decode()}}).execute()
        except Exception:return readback(svc,value)
        saved=svc.users().drafts().get(userId='me',id=created['id'],format='full').execute()
        return dict(state='verified'if draft_matches(saved,value)else'conflict',draft_id=created['id'])


def main():
    sys.path.insert(0,'/home/hermes/.hermes/scripts')
    payload=Path(os.environ.get('OLE_GTM_PAYLOAD','/home/hermes/hermes-extensions-staging/runtime/payload'))
    sys.path.insert(0,str(payload))
    args=json.load(sys.stdin);action=sys.argv[1]
    if action=='context':
        from gtm.briefing_daemon import ExistingReads,external_attendees
        result=ExistingReads().context(args['event'],args['category'])
        result=dict(result,account=ACCOUNTS[args['category']],external_emails=external_attendees(args['event']))
    else:
        value=args['plan']
        if value['category']not in ACCOUNTS or value['account']!=ACCOUNTS[value['category']]:raise ValueError('project_account_mismatch')
        svc=mail_service(value['category'])
        result=readback(svc,value,args.get('known_draft_id')) if action=='readback' else {'preflight':preflight,'write':write}[action](svc,value)
        if action=='preflight':result={k:v for k,v in result.items()if k!='thread'}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except Exception:
        print(json.dumps({'error':'call_mail_operation_unavailable'}));sys.exit(1)
