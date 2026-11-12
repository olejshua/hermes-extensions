#!/usr/bin/env python3
"""Create exactly N checked CODE drafts. This module has no send or CRM-stage write."""
import argparse
import base64
import datetime as dt
from email import policy
from email.parser import BytesParser
import fcntl
import html
import json
from pathlib import Path
import re
import sys
import uuid
import versand_abschluss_sicher as close
import versand_senden_sicher as safe


def verify_draft(result, draft_id, mail):
    if result.get('id') != draft_id:
        raise close.UnsafeCompletion('Draft ID readback mismatch')
    message = result.get('message') or {}
    if 'DRAFT' not in message.get('labelIds', []):
        raise close.UnsafeCompletion('Readback is not a draft')
    if not isinstance(message.get('raw'), str):
        raise close.UnsafeCompletion('Draft raw MIME content missing')
    raw = message['raw']; raw += '=' * (-len(raw)%4)
    mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
    if (safe.single_address(str(mime.get('From',''))) != close.SENDER
            or safe.single_address(str(mime.get('To',''))) != close.mailbox(mail['to'])
            or str(mime.get('Subject','')) != mail['subject']
            or mime.get('Cc') or mime.get('Bcc')):
        raise close.UnsafeCompletion('Draft sender/recipient/subject differs')
    part = mime.get_body(preferencelist=('plain','html')) if mime.is_multipart() else mime
    if part is None:
        raise close.UnsafeCompletion('Draft body missing')
    body = part.get_content()
    if part.get_content_type() == 'text/html':
        body = re.sub(r'<br\s*/?>','\n',body,flags=re.I)
        body = html.unescape(re.sub(r'<[^>]+>','',body))
    if body.replace('\r\n','\n').rstrip() != mail['body'].replace('\r\n','\n').rstrip():
        raise close.UnsafeCompletion('Draft body readback mismatch')
    return safe.message_id(message)


def run(v, gmail, expected, journal, known_id, known_query, smtp_status, create=False):
    journal=Path(journal).resolve()
    if not journal.parent.is_dir() or journal.exists():
        raise close.UnsafeCompletion('Fresh durable journal path required')
    lock_path=Path(getattr(v,'SENDER_LOCK',Path(v.STATE)/'abschluss-sicher.lock'))
    with lock_path.open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise close.UnsafeCompletion('Another sender/draft/closure is running') from exc
        mails,batch=v.load(v.MAILS),v.load(v.BATCH)
        today=dt.date.today().isoformat()
        safe.validate_prepared(v,mails,batch,expected,smtp_status,today)
        registry=Path(getattr(v,'ATTEMPT_REGISTRY',Path(v.STATE)/'send-attempts.jsonl'))
        draft_registry=registry.with_name('draft-attempts.jsonl')
        occupied=set()
        for path in (registry,draft_registry):
            for e in close.read_journal(path) if path.exists() else []:
                if e.get('event') not in ('attempt','draft_attempt'):raise close.UnsafeCompletion('Invalid reservation history')
                occupied.add(close.mailbox(e['email']))
        if occupied & {close.mailbox(m['to']) for m in mails}:
            raise close.UnsafeCompletion('Previously attempted mailbox; reconcile before reuse')
        safe.profile_and_control(gmail,known_id,known_query)
        snapshot=safe.crm_snapshot(v)
        for mail in mails:
            safe.validate_live(v,mail,snapshot);safe.no_history(gmail,mail,v)
        snapshot=safe.crm_refresh(v,snapshot,dt.datetime.now(dt.timezone.utc).date().isoformat()+'T00:00:00Z')
        for mail in mails:safe.validate_live(v,mail,snapshot)
        if not create:return dict(preflight_ok=len(mails),drafts=0,sent=0,dry_run=True)
        created=[]
        for mail in mails:
            if dt.date.today().isoformat()!=today or v.load(v.MAILS)!=mails or v.load(v.BATCH)!=batch:
                raise close.UnsafeCompletion('Prepared draft content changed')
            safe.profile_and_control(gmail,known_id,known_query)
            snapshot=safe.crm_refresh(v,snapshot,None)
            safe.validate_live(v,mail,snapshot);safe.no_history(gmail,mail,v)
            common=dict(schema_version=1,batch_id=batch['batch_id'],batch_sha256=close.fingerprint(batch),
                        page_id=close.page_id(mail['page_id']),email=close.mailbox(mail['to']),
                        content_sha256=close.fingerprint(close.content(mail)),attempt_id=str(uuid.uuid4()))
            event=dict(common,event='draft_attempt',at=dt.datetime.now(dt.timezone.utc).isoformat())
            safe.append_event(draft_registry,event);safe.append_event(journal,event)
            ack=gmail('GMAIL_CREATE_EMAIL_DRAFT',dict(user_id='me',recipient_email=mail['to'],
                                                      subject=mail['subject'],body=mail['body'],is_html=False))
            draft_id=ack.get('id')
            if not isinstance(draft_id,str) or not draft_id:raise close.UnsafeCompletion('Uncertain draft create; never retry')
            safe.append_event(journal,dict(common,event='draft_ack',draft_id=draft_id,
                                          at=dt.datetime.now(dt.timezone.utc).isoformat()))
            result=gmail('GMAIL_GET_DRAFT',dict(user_id='me',draft_id=draft_id,format='raw'))
            mid=verify_draft(result,draft_id,mail)
            safe.append_event(journal,dict(common,event='draft_readback',draft_id=draft_id,message_id=mid,sender=close.SENDER,
                                          to=mail['to'],subject=mail['subject'],body_verified=True,
                                          at=dt.datetime.now(dt.timezone.utc).isoformat()))
            created.append(draft_id)
            print(f'Draft read back: {len(created)}/{expected}',flush=True)
        return dict(preflight_ok=len(mails),drafts=len(created),draft_ids=created,sent=0,dry_run=False)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--expected',required=True,type=int);p.add_argument('--journal',required=True)
    p.add_argument('--account',required=True);p.add_argument('--liste',required=True,choices=('it','ingenieur','medizin','beratung'))
    p.add_argument('--control-journal',required=True);p.add_argument('--create',action='store_true')
    a=p.parse_args();sys.argv=[sys.argv[0],'--liste',a.liste]
    import versand as v
    from smtp_pruefen import status
    known_id,known_query=safe.control_from_journal(a.control_journal)
    try:print(json.dumps(run(v,safe.Gmail(a.account),a.expected,a.journal,known_id,known_query,status,a.create)))
    except Exception as exc:raise SystemExit(f'STOP: {exc}; no automatic retry') from exc
