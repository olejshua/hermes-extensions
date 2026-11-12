"""Native existing CODE Gmail SDK; transport is invoked exactly once on send."""
from email.message import EmailMessage
from email import policy
from email.utils import getaddresses
from email.parser import BytesParser
import base64
import threading

from .central import Stop,fingerprint
from .transport import OneSendHttp

SENDER='operator@primary.example.invalid'


class Gmail:
    def __init__(self,service_factory,*,allow_write=False,central=None,run_id=None,send_http_factory=OneSendHttp):
        self.factory=service_factory;self.local=threading.local()
        self.allow_write=allow_write;self.central=central;self.run_id=run_id
        self.send_http_factory=send_http_factory
        self.before_post=None

    def service(self):
        if not hasattr(self.local,'service'):self.local.service=self.factory()
        return self.local.service

    def profile(self):
        value=self.service().users().getProfile(userId='me').execute(num_retries=0)
        if value.get('emailAddress','').casefold()!=SENDER:raise Stop('wrong_gmail_account')
        return value

    def message(self,mid):
        result=self.service().users().messages().get(userId='me',id=mid,format='full').execute(num_retries=0)
        headers={h['name'].casefold():h['value']for h in result.get('payload',{}).get('headers',[])}
        def plain(part):
            if part.get('mimeType')=='text/plain'and part.get('body',{}).get('data'):
                data=part['body']['data'];return base64.urlsafe_b64decode(data+'='*((-len(data))%4)).decode('utf-8')
            values=[plain(p)for p in part.get('parts',[])];values=[v for v in values if v is not None]
            return values[0]if len(values)==1 else None
        return {'id':result['id'],'threadId':result.get('threadId'),'sender':headers.get('from'),
                'to':headers.get('to'),'subject':headers.get('subject'),'labelIds':result.get('labelIds',[]),
                'rfc_message_id':headers.get('message-id'),'internalDate':result.get('internalDate'),
                'body':plain(result.get('payload',{}))}

    @staticmethod
    def normalized_body(value):
        return value.replace('\r\n','\n').replace('\r','\n').rstrip('\n')

    def raw_message(self,mid):
        result=self.service().users().messages().get(userId='me',id=mid,format='raw').execute(num_retries=0)
        data=result['raw'];raw=base64.urlsafe_b64decode(data+'='*((-len(data))%4))
        return BytesParser(policy=policy.default).parsebytes(raw)

    def search(self,query,limit):
        messages=[];token=None;seen=set()
        while True:
            page=self.service().users().messages().list(userId='me',q=query,includeSpamTrash=True,
                maxResults=min(100,limit),**({'pageToken':token}if token else{})).execute(num_retries=0)
            if not isinstance(page,dict):raise Stop('gmail_search_incomplete')
            for row in page.get('messages',[]):messages.append(self.message(row['id']))
            token=page.get('nextPageToken')
            if not token:break
            if token in seen:raise Stop('gmail_pagination_repeated')
            seen.add(token)
            # Any match blocks a new contact; do not claim a partial positive
            # search is complete. Positive-control exact searches fit this cap.
            if len(messages)>=limit:return {'messages':messages,'nextPageToken':token}
        return {'messages':messages,'nextPageToken':None,'resultSizeEstimate':len(messages)}

    def __call__(self,slug,data):
        if slug=='GMAIL_GET_PROFILE':return self.profile()
        if slug=='GMAIL_FETCH_EMAILS':return self.search(data['query'],int(data.get('max_results',1)))
        if slug=='GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID':return self.message(data['message_id'])
        if slug!='GMAIL_SEND_EMAIL':raise Stop('unsupported_native_gmail_operation')
        if not self.allow_write or not self.central or not self.central.held:raise Stop('native_send_not_activated_or_unlocked')
        if not callable(self.before_post):raise Stop('bound_slot_gate_required')
        self.profile()
        recipients=getaddresses([data.get('recipient_email','')])
        if len(recipients)!=1 or not recipients[0][1]or data.get('from_email')!=SENDER:raise Stop('send_identity_invalid')
        address=recipients[0][1].casefold()
        reservation=self.central.states().get((self.run_id,address))
        if not reservation or reservation['kind']!='attempt':raise Stop('durable_central_attempt_required')
        if fingerprint(data)!=reservation['payload'].get('request_sha256'):raise Stop('reserved_request_changed')
        message=EmailMessage(policy=policy.SMTP)
        message['From']=SENDER;message['To']=address;message['Subject']=data['subject']
        message.set_content(data['body'])
        raw=base64.urlsafe_b64encode(message.as_bytes()).decode()
        # No application/SDK retry. Timeout/error leaves the pre-existing attempt.
        service=self.service()
        return service.users().messages().send(userId='me',body={'raw':raw}).execute(
            num_retries=0,http=self.send_http_factory(service,before_post=self.before_post))
