"""Explicitly authorized existing drafts: exempt only their own verified reservation.

No reservation is deleted or changed. Every other CRM/Gmail/SMTP/TED gate remains.
The draft itself is sent, so no second message or second draft is created.
"""
import copy
from pathlib import Path
from types import SimpleNamespace
import versand_abschluss_sicher as close
import versand_senden_sicher as safe
from versand_entwuerfe_sicher import verify_draft


def authorizations(journal, mails):
    events = close.read_journal(journal)
    result = {}
    for mail in mails:
        email = close.mailbox(mail['to'])
        matches = [e for e in events if e.get('event') == 'draft_readback'
                   and e.get('email') == email]
        if len(matches) != 1:
            raise safe.STOP('Existing draft needs exactly one durable readback')
        event = matches[0]
        if (event.get('schema_version') != 1 or event.get('sender') != close.SENDER
                or event.get('to') != email or event.get('subject') != mail['subject']
                or event.get('page_id') != close.page_id(mail['page_id'])
                or event.get('content_sha256') != close.fingerprint(close.content(mail))
                or event.get('body_verified') is not True
                or not event.get('draft_id') or not event.get('message_id')):
            raise safe.STOP('Existing draft durable identity/content mismatch')
        result[email] = event
    if len(result) != len(mails):
        raise safe.STOP('Duplicate authorized draft mailbox')
    return result


def reservation_matches(event, authorization):
    return (event.get('event') == 'draft_attempt'
            and all(event.get(k) == authorization.get(k) for k in
                    ('schema_version', 'page_id', 'email', 'content_sha256', 'attempt_id')))


def eligible_row(v, row, authorization):
    backend = v.TWENTY
    if not backend or row.get('_TwentyKind') != 'company':
        raise safe.STOP('Existing draft requires a live Twenty company source')
    rid = row['_TwentyId']; native = backend.records['company'][rid]
    history = native.get('outreachHistorie') or []
    if (native.get('kontaktiert') is True or native.get('kontaktiertAm')
            or len(history) != 1):
        raise safe.STOP('Existing draft has another contact/reservation history')
    h = history[0]
    if (h.get('event') != 'gmail_draft' or h.get('draft_id') != authorization['draft_id']
            or h.get('message_id') != authorization['message_id']
            or h.get('mailbox') != authorization['email']
            or h.get('subject') != authorization['subject'] or h.get('body_verified') is not True):
        raise safe.STOP('Live draft reservation differs from the authorized draft')
    from twenty_review_resolution import validate_resolved
    for review in backend.records['review'].values():
        if (any(c['id'] == rid for c in review.get('kandidaten') or [])
                and not validate_resolved(review, backend.records['company'])):
            raise safe.STOP('Existing draft company is held by an unresolved identity case')
    # In-memory view only. The reservation and native CRM flags remain untouched.
    clean = dict(row, _TwentyBlocked=False)
    if (close.page_id(row['id']) != authorization['page_id']
            or (row.get('E-Mail') or '').strip().lower() != authorization['email']
            or row.get('Stage') != 'Nicht kontaktiert' or v.crm_gesperrt(clean)):
        raise safe.STOP('Existing draft source no longer eligible')
    return clean


def validate_live(v, mail, snapshot, authorized):
    if close.mailbox(mail['to']) not in authorized:
        return safe.validate_live(v, mail, snapshot)
    a = authorized[close.mailbox(mail['to'])]
    groups = {name: [eligible_row(v, r, authorized[(r.get('E-Mail') or '').strip().lower()])
                         if (r.get('E-Mail') or '').strip().lower() in authorized else r
                         for r in rows] for name, rows in snapshot[0].items()}
    view = SimpleNamespace(**vars(v))
    checked_snapshot = safe.assemble_crm_snapshot(view, groups)
    def api(method, path, *args, **kwargs):
        page = v.api(method, path, *args, **kwargs)
        if method == 'GET' and path == '/pages/' + mail['page_id']:
            fresh = v.TWENTY.row(mail['page_id'])
            eligible_row(v, fresh, a)
            page = copy.deepcopy(page)
            page['properties']['_TwentyBlocked'] = dict(type='checkbox', checkbox=False)
        return page
    view.api = api
    return safe.validate_live(view, mail, checked_snapshot)


def no_history(gmail, mail, v, authorized):
    a = authorized.get(close.mailbox(mail['to']))
    if not a:
        return safe.no_history(gmail, mail, v)
    raw = gmail('GMAIL_GET_DRAFT', dict(user_id='me', draft_id=a['draft_id'], format='raw'))
    if verify_draft(raw, a['draft_id'], mail) != a['message_id']:
        raise safe.STOP('Existing draft message ID changed')
    email = close.mailbox(mail['to']); domain = v.basis(v.dom(email))
    queries = [f'in:anywhere (to:{email} OR from:{email})',
               f'in:anywhere (to:{domain} OR from:{domain})',
               f'in:anywhere "{mail["firma"]}"', f'in:drafts to:{email}',
               f'in:scheduled to:{email}']
    # Explicit family aliases from the original three-draft acceptance.
    if domain == 'mci.de':
        queries += ['in:anywhere "Studio Hamburg"',
                    'in:anywhere (to:studio-hamburg.de OR from:studio-hamburg.de)']
    if domain == 'provideo.eu': queries += ['in:anywhere "PRO VIDEO"']
    if domain == 'heinekingmedia.de': queries += ['in:anywhere "heinekingmedia"']
    for result in safe.fetch_queries(gmail, queries, 100):
        if result.get('nextPageToken'):
            raise safe.STOP('Existing draft Gmail history incomplete')
        for m in result['messages']:
            if (safe.message_id(m) != a['message_id'] or m.get('labelIds') != ['DRAFT']
                    or safe.single_address(m.get('to')) != email
                    or safe.single_address(m.get('sender')) != close.SENDER
                    or m.get('subject') != mail['subject']):
                raise safe.STOP('History beyond the specifically authorized draft')
