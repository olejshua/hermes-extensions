#!/usr/bin/env python3
"""Legacy SENT-only compatibility helper. New managed drafts reserve via mail_policy before creation."""
import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone

PRIMARY = 'operator@primary.example.invalid'

def hold(calendar, account, sent, start, label):
    if 'SENT' not in sent.get('labelIds', []) or 'DRAFT' in sent.get('labelIds', []):
        raise ValueError('A sent message is required for this legacy helper; draft reservations use mail_policy')
    if start.tzinfo is None:
        raise ValueError('Start must include timezone')
    end = start + timedelta(minutes=30)
    key = '|'.join((account, sent['threadId'], start.astimezone(timezone.utc).isoformat()))
    event_id = hashlib.sha256(key.encode()).hexdigest()[:40]
    try:
        existing = calendar.events().get(calendarId=PRIMARY, eventId=event_id).execute()
    except Exception as error:
        if getattr(getattr(error, 'resp', None), 'status', None) != 404:
            raise
        existing = None
    if existing is None:
        calendars = calendar.calendarList().list().execute().get('items', [])
        fuxam = [item['id'] for item in calendars if item.get('summary', '').casefold() == 'fuxam calendar']
        if not any(item.get('id') == PRIMARY for item in calendars) or len(fuxam) != 1:
            raise ValueError('Both CODE calendars must be available')
        ids = [PRIMARY, fuxam[0]]
        checked = calendar.freebusy().query(body={
            'timeMin': (start - timedelta(minutes=15)).isoformat(), 'timeMax': end.isoformat(),
            'timeZone': 'Europe/Berlin', 'items': [{'id': cid} for cid in ids],
        }).execute().get('calendars', {})
        for cid in ids:
            if cid not in checked or checked[cid].get('errors'):
                raise ValueError('Calendar availability could not be verified')
            if checked[cid].get('busy'):
                raise ValueError('Slot now conflicts; report sent proposal without a hold for review')
        body = {'id': event_id, 'summary': 'Terminvorschlag – ' + label,
                'start': {'dateTime': start.isoformat(), 'timeZone': 'Europe/Berlin'},
                'end': {'dateTime': end.isoformat(), 'timeZone': 'Europe/Berlin'},
                'visibility': 'private', 'transparency': 'opaque',
                'description': 'Reservierter Terminvorschlag, noch kein bestätigter Termin.',
                'extendedProperties': {'private': {'hermesSlotHold': '1', 'sourceAccount': account,
                                                  'sourceThread': sent['threadId'], 'sourceMessage': sent['id']}}}
        # One attempt only. On an uncertain API write, read the deterministic ID.
        try:
            calendar.events().insert(calendarId=PRIMARY, body=body, sendUpdates='none').execute()
        except Exception:
            existing = calendar.events().get(calendarId=PRIMARY, eventId=event_id).execute()
        else:
            existing = calendar.events().get(calendarId=PRIMARY, eventId=event_id).execute()
    valid = (datetime.fromisoformat(existing['start']['dateTime']) == start
             and datetime.fromisoformat(existing['end']['dateTime']) == end
             and existing.get('visibility') == 'private'
             and existing.get('transparency', 'opaque') == 'opaque'
             and existing.get('status') != 'cancelled' and not existing.get('attendees')
             and existing.get('extendedProperties', {}).get('private', {}).get('hermesSlotHold') == '1')
    if not valid:
        raise ValueError('Saved hold did not match; manual review required')
    return {'verified': True, 'event_id': event_id, 'start': start.isoformat(), 'end': end.isoformat()}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', choices=['code', 'stealth'], required=True)
    parser.add_argument('--sent-message-id', required=True)
    parser.add_argument('--start', required=True, help='Verified offered start, ISO8601 with offset')
    parser.add_argument('--label', required=True)
    args = parser.parse_args()
    import email_watcher as w
    from email_realtime import calendar_service
    api = w.service(args.account)
    expected = PRIMARY if args.account == 'code' else 'operator@secondary.example.invalid'
    if api.users().getProfile(userId='me').execute()['emailAddress'].lower() != expected:
        raise ValueError('Authenticated mailbox mismatch')
    sent = api.users().messages().get(userId='me', id=args.sent_message_id, format='minimal').execute()
    print(json.dumps(hold(calendar_service(), args.account, sent,
                          datetime.fromisoformat(args.start), args.label), ensure_ascii=False))

if __name__ == '__main__':
    main()
