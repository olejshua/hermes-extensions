"""Durable full-cutoff backfill plus Gmail history, independent of draft filters.

Only metadata enters intake. The cursor advances after every message is durably
queued or recorded as a review case. A failed page does not advance it. Gmail
history expiry triggers a complete cutoff rescan, with native source-ID dedupe.
"""
from __future__ import annotations
import hashlib,json,re
from email.utils import getaddresses,parseaddr
from gtm.calendar.confirmation import CODE
from gtm.mail_observer.sources import FullOriginals,SourceBlocked,AUTHORITY_HEADERS,checked_headers


class MailObserver:
    def __init__(self,envelopes,gmail,*,page_limit=10000,calendar_replies_enabled=False):
        if envelopes.declared!=frozenset({'acribic'}):raise ValueError('Only explicit Acribic mapping supported')
        self.envelopes,self.gmail,self.co=envelopes,gmail,envelopes.coordinator
        self.page_limit=page_limit
        self.calendar_replies_enabled=calendar_replies_enabled
        scope=json.dumps({'account':CODE,'category':'acribic','cutoff':envelopes.cutoff.isoformat()},sort_keys=True)
        self.scope=hashlib.sha256(scope.encode()).hexdigest()
        with self.co.store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS gtm_reply_observer(scope TEXT PRIMARY KEY,contract TEXT NOT NULL,history_id TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS gtm_reply_observer_reviews(scope TEXT,message_id TEXT,reason TEXT,PRIMARY KEY(scope,message_id))')
            old=c.execute('SELECT contract FROM gtm_reply_observer LIMIT 1').fetchone()
            if old and old['contract']!=scope:raise SourceBlocked('activation_scope_changed')
            c.execute('INSERT OR IGNORE INTO gtm_reply_observer VALUES(?,?,NULL)',(self.scope,scope))
    def _pages(self,resource,method,**kwargs):
        token=None;seen=set()
        for _ in range(self.page_limit):
            result=getattr(resource,method)(**kwargs,**({'pageToken':token} if token else {})).execute()
            yield result
            token=result.get('nextPageToken')
            if not token:return
            if not isinstance(token,str) or token in seen:raise SourceBlocked('gmail_pagination_review')
            seen.add(token)
        raise SourceBlocked('gmail_pagination_review')
    def _queue(self,message_id):
        try:
            meta=self.gmail.users().messages().get(userId='me',id=message_id,format='metadata',
                metadataHeaders=list(AUTHORITY_HEADERS)).execute()
        except Exception as error:
            if getattr(getattr(error,'resp',None),'status',None)!=404:raise
            with self.co.store.connect() as c:
                c.execute('INSERT OR IGNORE INTO gtm_reply_observer_reviews VALUES(?,?,?)',(self.scope,message_id,'message_removed_review'))
            return
        try:
            if meta.get('id')!=message_id:raise SourceBlocked('metadata_identity_mismatch')
            header_rows=[(i['name'],i['value']) for i in meta.get('payload',{}).get('headers',[])]
            labels=set(meta.get('labelIds',[]))
            try:h=checked_headers(header_rows)
            except SourceBlocked:
                if not self.calendar_replies_enabled or 'SENT'not in labels or 'DRAFT'in labels:raise
                from gtm.calendar.reconciliation import own_calendar_reply_headers
                h=own_calendar_reply_headers(header_rows)
            sender=parseaddr(h.get('from',''))[1].casefold()
            recipients={e.casefold() for _,e in getaddresses([h[k] for k in ('to','cc','bcc') if h.get(k)])}
            own_calendar_reply=(self.calendar_replies_enabled and sender==CODE and 'SENT'in labels and 'DRAFT'not in labels
                and re.match(r'^(?:Accepted|Angenommen):\s',h.get('subject',''),re.I))
            if not own_calendar_reply and (labels&{'SENT','DRAFT'} or sender==CODE or CODE not in recipients):return
            if not sender or '@' not in sender:raise SourceBlocked('sender_metadata_review')
            self.envelopes.on_message({'account':'code','id':message_id,'threadId':meta['threadId'],
                'from':sender,'internalDate':meta['internalDate']})
        except (ValueError,KeyError,TypeError) as error:
            reason=error.code if isinstance(error,SourceBlocked) else 'metadata_scope_review'
            with self.co.store.connect() as c:
                c.execute('INSERT OR IGNORE INTO gtm_reply_observer_reviews VALUES(?,?,?)',(self.scope,message_id,reason))
    def poll(self):
        profile=self.gmail.users().getProfile(userId='me').execute()
        if profile.get('emailAddress')!=CODE:raise SourceBlocked('account_mismatch')
        target=profile.get('historyId')
        if not isinstance(target,str) or not target.isdigit():raise SourceBlocked('gmail_history_unavailable')
        with self.co.store.connect() as c:
            cursor=c.execute('SELECT history_id FROM gtm_reply_observer WHERE scope=?',(self.scope,)).fetchone()['history_id']
        if cursor is not None and int(target)<int(cursor):raise SourceBlocked('gmail_history_regression_review')
        def backfill():
            # No INBOX filter: archived replies must not disappear. after is
            # second-granular; the envelope enforces the exact millisecond cutoff.
            for page in self._pages(self.gmail.users().messages(),'list',userId='me',
                                   q='after:'+str(int(self.envelopes.cutoff.timestamp())-1),maxResults=500,includeSpamTrash=True):
                for row in page.get('messages',[]):self._queue(row['id'])
        if cursor is None:backfill()
        else:
            try:
                for page in self._pages(self.gmail.users().history(),'list',userId='me',startHistoryId=cursor,
                                       historyTypes=['messageAdded'],maxResults=500):
                    head=page.get('historyId')
                    if head is not None and (not isinstance(head,str) or not head.isdigit() or int(head)<int(cursor)):
                        raise SourceBlocked('gmail_history_regression_review')
                    for event in page.get('history',[]):
                        for row in event.get('messagesAdded',[]):self._queue(row['message']['id'])
            except Exception as error:
                if getattr(getattr(error,'resp',None),'status',None)!=404:raise
                backfill()
        with self.co.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT history_id FROM gtm_reply_observer WHERE scope=?',(self.scope,)).fetchone()['history_id']
            if current!=cursor:raise SourceBlocked('gmail_history_concurrent_review')
            if current is not None and int(target)<int(current):raise SourceBlocked('gmail_history_regression_review')
            c.execute('UPDATE gtm_reply_observer SET history_id=? WHERE scope=?',(target,self.scope))
        return {'state':'observed','model_calls':0,'saas_writes':0}
