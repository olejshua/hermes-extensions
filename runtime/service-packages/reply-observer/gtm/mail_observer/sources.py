"""Read entire RFC originals, including text parts stored as Gmail attachments.

No snippet, replacement decoding, 4k body, 12-message window, or sender guess.
An explicit byte limit blocks one case; it never publishes a truncated original.
"""
from __future__ import annotations
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from gtm.calendar.confirmation import CODE, PIET, MailEvidence, authored_text


class SourceBlocked(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code) # fixed local code only, never provider content


class _HTML(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.text=[]
    def handle_starttag(self,tag,attrs):
        values=dict(attrs)
        if tag=='blockquote' or 'gmail_quote' in (values.get('class') or '') or values.get('id')=='divRplyFwdMsg':
            self.text.append('\n> ')
        elif tag in ('br','p','div'):self.text.append('\n')
    def handle_data(self,value):self.text.append(value)


@dataclass(frozen=True)
class OriginalMail(MailEvidence):
    raw_b64: str=''
    auto_submitted: str='no'
    list_unsubscribe: str=''
    precedence: str=''
    subject: str=''
    text_attachments: tuple=()
    def source_text(self):
        # Exact original bytes remain reconstructable even when HTML is rendered.
        return super().source_text()+'\n\nGMAIL-RAW-RFC822-BASE64\n'+self.raw_b64


def sha(source):return hashlib.sha256(source.source_text().encode('utf-8')).hexdigest()


# A shared gate for metadata, raw originals and typed acceptance. Never reduce
# an ambiguous routing/authority field to its first or last occurrence.
AUTHORITY_HEADERS=('From','To','Cc','Bcc','Sender','Reply-To','Message-ID','In-Reply-To','References',
                   'Auto-Submitted','Precedence','List-Unsubscribe','List-Id','Subject',
                   'Resent-From','Resent-To','Resent-Cc','Resent-Bcc','Resent-Sender','Resent-Message-ID')
RFC_ID=re.compile(r'<[^<>\s]+@?[^<>\s]*>')
DECLINE=re.compile(r'\b(?:kein(?:erlei)? interesse|nicht (?:mehr )?kontaktieren|keine weiteren (?:mails|nachrichten|anfragen))\b',re.I)


def checked_headers(rows):
    grouped={}
    for name,value in rows:
        if not isinstance(name,str) or not isinstance(value,str):raise SourceBlocked('original_authority_review')
        lower=name.casefold();grouped.setdefault(lower,[]).append(value)
        if lower.startswith('resent-'):raise SourceBlocked('original_authority_review')
    for name in AUTHORITY_HEADERS:
        values=grouped.get(name.casefold(),[])
        if len(values)>1 or (name in ('From','To','Message-ID') and len(values)!=1):
            raise SourceBlocked('original_authority_review')
    h={k:v[0] for k,v in grouped.items() if k in {n.casefold() for n in AUTHORITY_HEADERS}}
    from email.headerregistry import HeaderRegistry
    registry=HeaderRegistry()
    for key in ('from','to','cc','bcc','sender','reply-to'):
        if key not in h:continue
        header=registry(key,h[key])
        if header.defects or not header.addresses or any(not a.username or not a.domain for a in header.addresses):
            raise SourceBlocked('original_authority_review')
        if key in ('from','sender','reply-to') and len(header.addresses)!=1:raise SourceBlocked('original_authority_review')
    sender=parseaddr(h['from'])[1].casefold()
    if any(parseaddr(h[k])[1].casefold()!=sender for k in ('sender','reply-to') if k in h):
        raise SourceBlocked('original_authority_review')
    for key in ('message-id','in-reply-to'):
        if key in h and not RFC_ID.fullmatch(h[key].strip()):raise SourceBlocked('original_authority_review')
    if 'references' in h:
        refs=RFC_ID.findall(h['references'])
        if not refs or re.sub(RFC_ID,'',h['references']).strip() or len(set(refs))!=len(refs):
            raise SourceBlocked('original_authority_review')
        if 'in-reply-to' in h and refs[-1]!=h['in-reply-to'].strip():raise SourceBlocked('original_authority_review')
    return h


def checked_original(source):
    if not isinstance(source,OriginalMail) or not source.raw_b64:raise SourceBlocked('original_authority_review')
    try:
        raw=base64.b64decode(source.raw_b64,validate=True)
        message=BytesParser(policy=policy.default).parsebytes(raw)
        if message.defects:raise SourceBlocked('original_authority_review')
        h=checked_headers([(k,str(v)) for k,v in message.items()])
        recipients={a.casefold() for _,a in getaddresses([h[k] for k in ('to','cc','bcc') if h.get(k)]) if a}
        if (parseaddr(h['from'])[1].casefold()!=source.sender or recipients!=set(source.recipients) or
            h['message-id']!=source.rfc_message_id or h.get('in-reply-to','')!=source.in_reply_to):
            raise SourceBlocked('original_authority_review')
        body,attachments=mime_text(message)
        if body!=source.body or list(attachments)!=list(source.text_attachments):raise SourceBlocked('original_content_review')
        return h
    except SourceBlocked:raise
    except (ValueError,UnicodeError,LookupError,TypeError):raise SourceBlocked('original_authority_review') from None


def mime_text(message):
    bodies={'text/plain':[],'text/html':[]};attachments=[]
    for part in message.walk():
        if part.defects or any(len(part.get_all(h,[]))>1 for h in ('Content-Type','Content-Transfer-Encoding','Content-Disposition','MIME-Version')):
            raise SourceBlocked('original_mime_review')
        if part.get_content_type() not in bodies:continue
        data=part.get_payload(decode=True)
        if data is None:raise SourceBlocked('original_mime_review')
        text=data.decode(part.get_content_charset() or 'utf-8',errors='strict')
        if part.get_content_type()=='text/html':
            parser=_HTML();parser.feed(text);parser.close();text=' '.join(parser.text)
        if part.get_content_disposition()=='attachment' or part.get_filename():
            attachments.append({'name':part.get_filename() or '', 'mime_type':part.get_content_type(),'text':text})
        else:bodies[part.get_content_type()].append(text)
    return '\n'.join(bodies['text/plain'] or bodies['text/html']),tuple(attachments)


class FullOriginals:
    def __init__(self,*,max_bytes=8*1024*1024,max_thread_bytes=16*1024*1024):
        self.max_bytes,self.max_thread_bytes=max_bytes,max_thread_bytes
    @staticmethod
    def profile(gmail):
        if gmail.users().getProfile(userId='me').execute().get('emailAddress')!=CODE:
            raise SourceBlocked('account_mismatch')
    def get(self,gmail,message_id,*,thread_id=None):
        result=gmail.users().messages().get(userId='me',id=message_id,format='raw').execute()
        if result.get('id')!=message_id or (thread_id and result.get('threadId')!=thread_id):
            raise SourceBlocked('message_identity_mismatch')
        encoded=result.get('raw')
        if not isinstance(encoded,str) or not encoded or len(encoded)>4*((self.max_bytes+2)//3):
            raise SourceBlocked('original_size_review')
        try:
            raw=base64.b64decode(encoded+'='*(-len(encoded)%4),altchars=b'-_',validate=True)
            if len(raw)>self.max_bytes:raise ValueError()
            message=BytesParser(policy=policy.default).parsebytes(raw)
            if message.defects:raise ValueError()
            h=checked_headers([(k,str(v)) for k,v in message.items()])
            body,attachments=mime_text(message)
            sender=parseaddr(h['from'])[1].casefold()
            recipients=tuple(sorted({e.casefold() for _,e in getaddresses([h[k] for k in ('to','cc','bcc') if h.get(k)]) if e}))
            rfc=h['message-id'];parent=h.get('in-reply-to','')
            if not body.strip() or not sender or '@' not in sender or (not rfc and 'DRAFT' not in result.get('labelIds',[])):raise ValueError()
            stamp=result.get('internalDate')
            if not isinstance(stamp,str) or not stamp.isdigit():raise ValueError()
        except SourceBlocked:raise
        except (ValueError,UnicodeError,LookupError,TypeError):
            raise SourceBlocked('original_mime_review') from None
        original=OriginalMail(CODE,message_id,result['threadId'],sender,recipients,body,
                              tuple(result.get('labelIds',[])),rfc,parent,True,
                              base64.b64encode(raw).decode('ascii'),str(message.get('Auto-Submitted','no')),
                              str(message.get('List-Unsubscribe','')),str(message.get('Precedence','')),str(message.get('Subject','')),tuple(attachments))
        return original,stamp
    def capture_full_message(self,gmail,row,category,*,profile_checked=False,source_pair=None):
        if category!='acribic' or row.get('account')!='code':raise SourceBlocked('project_not_mapped')
        if not profile_checked:self.profile(gmail)
        source,stamp=source_pair or self.get(gmail,row['id'],thread_id=row['threadId'])
        if checked_original(source).get('list-id'):raise SourceBlocked('newsletter_or_autoreply_ignored')
        if (source.message_id!=row['id'] or source.thread_id!=row['threadId'] or stamp!=row['internalDate'] or source.sender!=parseaddr(row['from'])[1].casefold() or
            CODE not in source.recipients or source.sender in (CODE,PIET,'operator@secondary.example.invalid') or
            set(source.labels)&{'SENT','DRAFT'}):raise SourceBlocked('incoming_scope_mismatch')
        if (source.auto_submitted.casefold()!='no' or source.list_unsubscribe or
            source.precedence.casefold() in ('bulk','list','junk') or
            source.sender.split('@')[0].casefold() in ('noreply','no-reply','donotreply','do-not-reply') or
            source.subject.casefold().startswith(('automatic reply:','automatische antwort:','out of office:','abwesenheitsnotiz:'))):
            raise SourceBlocked('newsletter_or_autoreply_ignored')
        return source,datetime.fromtimestamp(int(stamp)/1000,timezone.utc).isoformat()
    def thread(self,gmail,thread_id,*,include_drafts=False,profile_checked=False):
        if not profile_checked:self.profile(gmail)
        thread=gmail.users().threads().get(userId='me',id=thread_id,format='minimal').execute()
        rows=thread.get('messages')
        if thread.get('id')!=thread_id or not isinstance(rows,list) or not rows or len(rows)>1000:
            raise SourceBlocked('thread_size_review')
        seen=set();sources=[];size=0
        for row in rows:
            mid=row.get('id')
            if not isinstance(mid,str) or mid in seen:raise SourceBlocked('thread_membership_mismatch')
            seen.add(mid)
            if 'DRAFT' in row.get('labelIds',[]) and not include_drafts:continue
            source,stamp=self.get(gmail,mid,thread_id=thread_id)
            if 'DRAFT' in source.labels and not include_drafts:continue
            size+=len(source.raw_b64)
            if size>4*((self.max_thread_bytes+2)//3):raise SourceBlocked('thread_size_review')
            sources.append((source,stamp))
        return sources
    def verify_outreach_thread(self,gmail,row,category,*,sources=None):
        if category!='acribic':raise SourceBlocked('project_not_mapped')
        sources=self.thread(gmail,row['threadId']) if sources is None else sources
        sent=[];latest=[];incoming=0;own_outreach=set();indexed={}
        sender=parseaddr(row['from'])[1].casefold()
        for source,stamp in sources:
            checked_original(source)
            if source.rfc_message_id in indexed:raise SourceBlocked('rfc_thread_ambiguous')
            indexed[source.rfc_message_id]=(source,stamp)
            if source.message_id==row['id']:incoming+=1
            latest.append((int(stamp),source.message_id))
            if ('SENT' not in source.labels or source.sender!=CODE or set(source.recipients)!={sender} or
                int(stamp)>=int(row['internalDate'])):continue
            own=authored_text(source.body).casefold()
            if all(t in own for t in ('code university','ted','ausschreib')):
                sent.append((source.message_id,sha(source)))
                own_outreach.add(source.rfc_message_id)
        if incoming!=1 or not sent:raise SourceBlocked('outreach_direction_review')
        selected=[s for s,_ in sources if s.message_id==row['id']][0]
        if selected.sender!=sender or set(selected.recipients)!={CODE}:raise SourceBlocked('reply_recipient_review')
        parent=selected.in_reply_to;visited=set()
        while parent not in own_outreach:
            if not parent or parent in visited or parent not in indexed:raise SourceBlocked('outreach_rfc_chain_review')
            visited.add(parent);linked,stamp=indexed[parent]
            if int(stamp)>=int(row['internalDate']):raise SourceBlocked('outreach_rfc_chain_review')
            if linked.sender not in (CODE,sender):raise SourceBlocked('outreach_rfc_actor_review')
            parent=linked.in_reply_to
        newest=max(stamp for stamp,_ in latest)
        return {'sent_sources':sorted(sent),'status_current':[mid for stamp,mid in latest if stamp==newest]==[row['id']]}
    def acceptance_reader(self,gmail,*,thread_id,proposal_id,incoming_id,include_sources=False):
        def read():
            sources=self.thread(gmail,thread_id)
            newest=max(int(stamp) for _,stamp in sources)
            if [s.message_id for s,stamp in sources if int(stamp)==newest]!=[incoming_id]:
                raise SourceBlocked('acceptance_superseded')
            indexed={s.message_id:s for s,_ in sources}
            if proposal_id not in indexed or incoming_id not in indexed:raise SourceBlocked('proposal_missing')
            if include_sources:return indexed[proposal_id],indexed[incoming_id],tuple(sources)
            return indexed[proposal_id],indexed[incoming_id]
        return read
