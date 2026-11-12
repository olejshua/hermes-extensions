"""Read native DSN MIME fields; retrieval words never decide a bounce."""
from datetime import datetime,timezone
from email.utils import getaddresses
from .central import Stop


def delivery_status(message,known):
    original=set()
    for part in message.walk():
        for name in ('original-message-id','message-id'):
            value=part.get(name)
            if value:original.add(value.strip())
    failed=[];uncertain=[]
    for part in message.walk():
        if part.get_content_type()!='message/delivery-status':continue
        for block in part.get_payload():
            recipient=block.get('Final-Recipient')or block.get('Original-Recipient')or''
            address=recipient.partition(';')[2].strip().casefold()
            if address not in known:continue
            reference=known[address];action=(block.get('Action')or'').strip().casefold()
            status=(block.get('Status')or'').strip()
            record={'mailbox':address,'action':action,'status':status,
                    'diagnostic':str(block.get('Diagnostic-Code')or'')[:500]}
            expected=reference.get('rfc_message_id')
            if not expected or expected not in original:
                uncertain.append({**record,'reason':'selected_recipient_DSN_without_original_id'})
            elif action=='failed'and status.startswith('5.'):
                failed.append({**record,'original_message_id':expected,'message_id':reference['message_id']})
            elif action not in {'delivered','relayed','expanded'}:
                uncertain.append({**record,'reason':'selected_recipient_nonfinal_delivery_status'})
    return failed,uncertain


def check(gmail,events):
    known={e['email'].casefold():e for e in events if e['event']=='sent_readback'}
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'confirmed':[],'uncertain':[]}
    if not known:return result
    first=min(datetime.fromisoformat(e['at']).timestamp()for e in events if e['event']=='attempt')
    query=f'in:anywhere after:{int(first)-60} (from:mailer-daemon OR from:postmaster OR "Delivery Status Notification" OR "Undelivered Mail Returned")'
    found=gmail.search(query,1000)
    if found.get('nextPageToken'):raise Stop('delivery_search_incomplete')
    for row in found['messages']:
        failed,uncertain=delivery_status(gmail.raw_message(row['id']),known)
        result['confirmed'].extend({**value,'dsn_id':row['id']}for value in failed)
        result['uncertain'].extend({**value,'dsn_id':row['id']}for value in uncertain)
    return result
