"""Recover an existing company from the actual original outreach recipient.

Only when the calendar contact has no CRM record. A person/company ambiguity
must never take this route; an RFC-linked same-company role-mailbox may.
"""
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr
import re
from gtm.calendar.routing import CODE, INTERNAL_RECIPIENTS, project_from_thread

SHARED={'gmail.com','googlemail.com','outlook.com','hotmail.com','gmx.de','gmx.net','web.de','yahoo.com','icloud.com','primary.example.invalid'}


def original_recipients(binding, contacts, *, before_call=True, project_router=None):
    if binding.get('category')!='acribic' or binding.get('account')!=CODE:return None
    messages=binding.get('mail_context',{}).get('messages',[])
    if project_router is None:
        try:
            from gtm.briefing_sources import native_routing
            project_router=native_routing().project_from_thread
        except (ImportError,ValueError):
            if any(m.get('reference_source')is True for m in messages):raise ValueError('native_project_router_missing')
            project_router=project_from_thread
    clock=datetime.fromisoformat(binding['occurred_at'].replace('Z','+00:00')) if before_call else datetime.now(timezone.utc)
    limit=int(clock.timestamp()*1000)
    results=[]
    for contact in contacts:
        domain=contact.casefold().rsplit('@',1)[-1]
        if domain in SHARED:return None
        found=[]
        for incoming in messages:
            if (incoming.get('author')!=contact or 'SENT'in incoming.get('labels',[]) or
                    'DRAFT'in incoming.get('labels',[]) or incoming.get('calendar_notification') or
                    int(incoming.get('internal_date')or 0)>limit):continue
            original=[]
            for m in messages:
                if ((m.get('thread_id')!=incoming['thread_id'] and m.get('reference_source')is not True) or 'DRAFT'in m.get('labels',[]) or
                        m.get('calendar_notification') or int(m.get('internal_date')or 0)>int(incoming['internal_date'])):continue
                hd=m.get('headers',{})
                original.append(dict(id=m['message_id'],thread_id=m['thread_id'],reference_source=m.get('reference_source',False),internalDate=m['internal_date'],labels=m['labels'],
                    text=m['original'],**{'from':hd.get('from',''),'to':hd.get('to',''),'cc':hd.get('cc',''),
                    'rfc_message_id':hd.get('message-id'),'in_reply_to':hd.get('in-reply-to'),'references':hd.get('references')}))
            original.sort(key=lambda m:int(m['internalDate']))
            if not original or original[-1]['id']!=incoming['message_id']:continue
            try:project=project_router('code',original)
            except ValueError:continue
            if project!='acribic':continue
            index={m['rfc_message_id']:m for m in original};current=original[-1];seen=set()
            while current.get('in_reply_to'):
                parent=current['in_reply_to']
                if parent not in index:
                    known=[v for v in re.findall(r'<[^<>\s]+>',current.get('references')or'')if v in index and v not in seen]
                    if not known:break
                    parent=known[-1]
                if parent in seen:break
                seen.add(parent);current=index[parent]
                own=re.split(r'(?im)^\s*(?:>|Von:|From:|Am .+schrieb.+:|On .+wrote:|-----)',current['text'],maxsplit=1)[0].casefold()
                if parseaddr(current.get('from',''))[1].casefold()==CODE and 'SENT'in current['labels'] and all(k in ' '.join(own.split())for k in ('code university','ted','ausschreib')):
                    addresses=sorted({a.casefold()for _,a in getaddresses([current['to'],current['cc']])if a and a.casefold()not in INTERNAL_RECIPIENTS})
                    if addresses and all(a.rsplit('@',1)[-1]==domain for a in addresses):
                        found.append(dict(thread_id=incoming['thread_id'],incoming_message_id=incoming['message_id'],
                            original_message_id=current['id'],original_recipients=addresses))
                    break
        if not found:return None
        choices={tuple(row['original_recipients'])for row in found}
        if len(choices)!=1:return None
        results.append(found[-1])
    return dict(addresses=sorted({a for row in results for a in row['original_recipients']}),proof=results)if results else None
