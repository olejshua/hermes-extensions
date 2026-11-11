#!/usr/bin/env python3
"""Low-cost Gmail polling and thread-safe reply drafts for the two authorized mailboxes.

`poll` is a deterministic Hermes cron change-detector. It emits the SAME JSON until
another relevant message arrives, so an LLM is awakened only on a change. No send.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from google_auth_store import load_credentials_for_path, safe_error, verified_profile_for_service
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
DATA = HOME / "email_watcher"
STATE = DATA / "state.json"
ACCOUNTS = {"code": HOME / "google_token_code.json", "stealth": HOME / "google_token_stealth.json"}
MAX_PAGES = 12
BATCH_SIZE = 5
HEADERS = ["From", "To", "Cc", "Bcc", "Reply-To", "Subject", "Date", "Message-ID", "References", "Auto-Submitted", "Precedence", "List-Unsubscribe"]
AUTOREPLY = re.compile(r"^(automatische antwort|automatic reply|auto[ -]?reply|out of office|abwesenheitsnotiz|accepted:|declined:|tentative:|delivery status notification|undeliverable|unzustellbar)", re.I)
NO_REPLY = re.compile(r"^(no[._-]?reply|do[._-]?not[._-]?reply|mailer[._-]?daemon|postmaster|notifications?)$", re.I)
ACTION_ALERT = re.compile(r"(payment|zahlung|rechnung|invoice|security|sicherheit|konto|account|action required|handlungsbedarf|frist|deadline|vertrag|contract|incident|termin|meeting)", re.I)


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def credentials_for_account(alias):
    if alias not in ACCOUNTS:
        raise ValueError("unknown_account")
    return load_credentials_for_path(ACCOUNTS[alias])


def service(alias):
    return build("gmail", "v1", credentials=credentials_for_account(alias), cache_discovery=False)


def verified_profile(svc):
    return verified_profile_for_service(svc)


def headers(msg):
    return {h.get("name", "").lower(): h.get("value", "") for h in msg.get("payload", {}).get("headers", [])}


def list_inbox(svc, since):
    token = None
    results = []
    for _ in range(MAX_PAGES):
        page = svc.users().messages().list(userId="me", labelIds=["INBOX"], q=f"after:{int(since)}", maxResults=100, pageToken=token).execute()
        results.extend(page.get("messages", []))
        token = page.get("nextPageToken")
        if not token:
            return results
    raise RuntimeError("Inbox page limit reached; cursor not advanced. Manual review required")


def candidate(meta):
    h = headers(meta)
    sender = parseaddr(h.get("from", ""))[1]
    local = sender.split("@", 1)[0].lower()
    label = set(meta.get("labelIds", []))
    if not sender or "INBOX" not in label or "SENT" in label or "DRAFT" in label:
        return False
    if "CATEGORY_PROMOTIONS" in label or "CATEGORY_SOCIAL" in label:
        return False
    if AUTOREPLY.match(h.get("subject", "").strip()):
        return False
    if NO_REPLY.match(local) and not ACTION_ALERT.search(h.get("subject", "")):
        return False
    if h.get("auto-submitted", "no").lower() != "no" or h.get("list-unsubscribe"):
        return False
    if h.get("precedence", "").lower() in ("bulk", "list", "junk"):
        return False
    return True


def summary(alias, meta):
    h = headers(meta)
    return {"account": alias, "id": meta["id"], "threadId": meta["threadId"],
            "from": h.get("from", ""), "subject": h.get("subject", ""),
            "snippet": meta.get("snippet", "")[:350], "internalDate": meta.get("internalDate", "")}


def poll():
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "state.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = int(time.time())
        state = json.loads(STATE.read_text()) if STATE.exists() else None
        if state is None:
            # Bootstrap successful accounts independently; a failed Stealth login
            # must not stop CODE or skip its own unread interval after reconnect.
            seen, errors, checks, retries = {}, {}, {}, {}
            for alias in ACCOUNTS:
                checks[alias] = now - 3 * 86400
                seen[alias] = []
                try:
                    seen[alias] = [m["id"] for m in list_inbox(service(alias), checks[alias])]
                    checks[alias] = now
                except Exception as exc:
                    errors[alias] = safe_error(exc).code
                    try: token_mtime = ACCOUNTS[alias].stat().st_mtime_ns
                    except OSError: token_mtime = None
                    retries[alias] = {"reason": errors[alias], "token_mtime": token_mtime, "after": now + 300}
            state = {"last_check": now, "seen": seen, "pending": [],
                     "last_check_by_account": checks, "errors": errors, "account_retries": retries,
                     "event": {"seq": 0, "messages": []}}
            _atomic_json(STATE, state)
            print(json.dumps({**state["event"], "errors": errors}, ensure_ascii=False, sort_keys=True))
            return
        previous = int(state["last_check"])
        account_checks = {alias: int(state.get("last_check_by_account", {}).get(alias, previous))
                          for alias in ACCOUNTS}
        # At-most-once processing, with 10-minute overlap to cover indexing delay.
        seen = {a: set(state["seen"].get(a, [])) for a in ACCOUNTS}
        pending = list(state.get("pending", []))
        errors = {}
        retries = dict(state.get("account_retries", {}))
        for alias in ACCOUNTS:
            found = []
            seen_this_account = set(seen[alias])
            try:
                token_mtime = ACCOUNTS[alias].stat().st_mtime_ns
            except OSError:
                token_mtime = None
            retry = retries.get(alias, {})
            if now < retry.get("after", 0) and token_mtime == retry.get("token_mtime"):
                errors[alias] = retry["reason"]
                continue
            try:
                svc = service(alias)
                ids = list_inbox(svc, account_checks[alias] - 600)
                new = [m["id"] for m in ids if m["id"] not in seen_this_account]
                for mid in new:
                    meta = svc.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=HEADERS).execute()
                    if not {'SENT','DRAFT','SPAM','TRASH'} & set(meta.get('labelIds', [])):
                        # Durable intake precedes filtering and the seen cursor.
                        # The independent outcome monitor can find a skipped mail.
                        _atomic_json(DATA/'intake'/(alias+'-'+mid+'.json'), summary(alias, meta))
                    if candidate(meta):
                        found.append(summary(alias, meta))
                    seen_this_account.add(mid)
            except Exception as exc:
                errors[alias] = safe_error(exc).code
                retries[alias] = {"reason": errors[alias], "token_mtime": token_mtime,
                                  "after": now + (300 if not safe_error(exc).retryable else 30)}
                continue
            retries.pop(alias, None)
            seen[alias] = seen_this_account
            account_checks[alias] = now
            pending.extend(found)
        pending.sort(key=lambda x: int(x["internalDate"] or "0"))
        if pending:
            state["event"] = {"seq": state["event"]["seq"] + 1,
                              "messages": pending[:BATCH_SIZE]}
            pending = pending[BATCH_SIZE:]
        state["pending"] = pending
        state["seen"] = {a: sorted(ids) for a, ids in seen.items()}
        state["last_check_by_account"] = account_checks
        if len(errors) < len(ACCOUNTS):
            state["last_check"] = now
        state["errors"] = errors
        state["account_retries"] = retries
        _atomic_json(STATE, state)
        print(json.dumps({**state["event"], "errors": errors}, ensure_ascii=False, sort_keys=True))


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)
    def text(self):
        return unescape(" ".join(self.parts))


def body(payload):
    parts = [payload]
    plain, html = [], []
    while parts:
        part = parts.pop()
        parts.extend(part.get("parts", []))
        data = part.get("body", {}).get("data")
        if not data:
            continue
        text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
        if part.get("mimeType") == "text/plain": plain.append(text)
        elif part.get("mimeType") == "text/html":
            parser = _Text(); parser.feed(text); html.append(parser.text())
    return "\n".join(plain or html)


def thread(alias, tid):
    svc = service(alias)
    data = svc.users().threads().get(userId="me", id=tid, format="full").execute()
    all_messages = data.get("messages", [])
    msgs = []
    for msg in all_messages[-12:]:
        h = headers(msg)
        text = body(msg.get("payload", {}))
        msgs.append({"id": msg["id"], "labels": msg.get("labelIds", []),
                     "from": h.get("from"), "to": h.get("to"), "date": h.get("date"),
                     "subject": h.get("subject"), "messageId": h.get("message-id"),
                     "body": text[:4000], "body_truncated": len(text) > 4000})
    print(json.dumps({"account": alias, "threadId": tid,
                      "older_messages_omitted": max(0, len(all_messages) - 12),
                      "messages": msgs}, ensure_ascii=False))


_SIGNOFF = re.compile(
    r'(?is)(?:^|\n+)(?P<closing>Beste Grüße|Mit besten Grüßen|Mit freundlichen Grüßen|'
    r'Liebe Grüße|Viele Grüße|Herzliche Grüße|VG)'
    r'(?P<names>(?:\s*Alex(?: Beispiel)?)*)\s*\Z'
)


def canonical_draft_signature(text, *, force_formal=False):
    from mail_policy import format_body
    return format_body(text, force_formal=force_formal)


class SchedulingDraftReviewRequired(ValueError):
    """No managed draft write was attempted; reservation/text needs review."""
    def __init__(self, message, *, code='slot_reservation_unavailable', stage='reservation', cause_type=None):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.cause_type = cause_type

class DraftStateUnverified(RuntimeError):
    """Gmail write may exist. Never automatically retry this create."""

class SourceChangedBeforeDraft(RuntimeError):
    """A newer inbound or sent reply superseded this draft; no Gmail write."""


def require_latest_source(messages, mid):
    active = [m for m in messages if 'DRAFT' not in m.get('labelIds', [])]
    if not active or active[-1].get('id') != mid:
        raise SourceChangedBeforeDraft('source_changed_before_draft')

_REFERENCE_SOURCES={}

def complete_thread_sources(alias,messages,svc=None):
    """Read exact missing RFC ancestors, never a subject/company search.

    Gmail can put the original owned outreach in another thread. Only a unique
    older SENT from this verified mailbox to the same actor/corporate domain is
    admitted. Its real thread ID is retained and its origin is explicit.
    """
    active=[message for message in messages if'DRAFT'not in message.get('labelIds',[])]
    if not active:return list(messages)
    expected={'code':'operator@primary.example.invalid','stealth':'operator@secondary.example.invalid'}[alias]
    actor=parseaddr(headers(active[-1]).get('from')or'')[1].casefold()
    if not actor or actor==expected:return list(messages)
    shared={'gmail.com','googlemail.com','outlook.com','hotmail.com','yahoo.com','icloud.com','gmx.de','gmx.net','web.de','primary.example.invalid'}
    domain=actor.rsplit('@',1)[-1]
    known={headers(message).get('message-id')for message in messages}
    requests=[]
    for child in active:
        h=headers(child)
        for reference in [h.get('in-reply-to'),*re.findall(r'<[^<>\s]{1,250}>',h.get('references')or'')]:
            if reference and re.fullmatch(r'<[^<>\s]{1,250}>',reference)and reference not in known and reference not in{r for r,_ in requests}:
                requests.append((reference,child))
    if not requests:return list(messages)
    svc=svc or service(alias)
    if verified_profile(svc).casefold()!=expected:raise ValueError('mailbox_identity_mismatch')
    extra=[]
    for reference,child in requests[:4]:
        key=(alias,reference);parent=_REFERENCE_SOURCES.get(key)
        if parent is None:
            page=svc.users().messages().list(userId='me',q='rfc822msgid:'+reference,maxResults=2).execute()
            if page.get('nextPageToken')or len(page.get('messages',[]))!=1:continue
            candidates=[]
            for row in page.get('messages',[]):
                candidate=svc.users().messages().get(userId='me',id=row['id'],format='full').execute()
                if headers(candidate).get('message-id')==reference:candidates.append(candidate)
            if len(candidates)!=1:continue
            parent=candidates[0]
        h=headers(parent);targets={address.casefold()for _,address in getaddresses([h.get('to')or'',h.get('cc')or''])if address}
        external=targets-{expected,'contact-3@primary.example.invalid'}
        same_firm=bool(external)and all(address==actor or(domain not in shared and address.rsplit('@',1)[-1]==domain)for address in external)
        try:older=int(parent['internalDate'])<int(child['internalDate'])
        except (KeyError,ValueError,TypeError):older=False
        if (not parent.get('id')or not parent.get('threadId')or headers(parent).get('message-id')!=reference or'SENT'not in parent.get('labelIds',[])
                or'DRAFT'in parent.get('labelIds',[])or parseaddr(h.get('from')or'')[1].casefold()!=expected
                or not(actor in external or same_firm)or not older):continue
        if len(_REFERENCE_SOURCES)>=64:_REFERENCE_SOURCES.pop(next(iter(_REFERENCE_SOURCES)))
        _REFERENCE_SOURCES[key]=parent
        extra.append({**parent,'hermes_reference_source':True});known.add(reference)
    return sorted([*extra,*messages],key=lambda message:int(message.get('internalDate')or 0))


def make_draft(alias, mid, text, *, to=None, cc=None, slots=(), project=None, company=None,
               followup_to_last_sent=False,write_owner=None):
    from mail_policy import MailPolicyReviewRequired
    attempt = {'write_started': False}
    try:
        result=_make_draft(alias, mid, text, to=to, cc=cc, slots=slots, project=project,
                           company=company, followup_to_last_sent=followup_to_last_sent,write_owner=write_owner, _attempt=attempt)
        from mail_result_io import finish_owned_write
        finish_owned_write(result)
        return result
    except DraftStateUnverified:
        raise
    except SourceChangedBeforeDraft:
        raise
    except Exception as error:
        if attempt['write_started']:
            raise DraftStateUnverified('Draft write started; final state unverified, inspect without retry') from error
        if getattr(error,'code',None)in{'mail_model_quota','mail_model_auth','mail_model_unavailable','mail_model_output_invalid','mail_model_runtime_failed'}:
            from mail_result_io import model_failed_before_write
            error.write_generation=attempt['write_generation']
            model_failed_before_write(attempt['item'],error,write_owner=write_owner,write_generation=attempt['write_generation'])
            raise
        if slots or isinstance(error, MailPolicyReviewRequired):
            code = 'mail_text_validation' if isinstance(error, MailPolicyReviewRequired) else 'slot_reservation_unavailable'
            raise SchedulingDraftReviewRequired('Scheduling draft blocked before Gmail write; review reservation/text',
                code=code, stage=attempt.get('stage', 'preflight'), cause_type=type(error).__name__) from error
        raise

def _make_draft(alias, mid, text, *, to=None, cc=None, slots=(), project=None, company=None,
                followup_to_last_sent=False,write_owner=None, _attempt=None):
    svc = service(alias)
    original = svc.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=HEADERS).execute()
    h = headers(original)
    tid = original["threadId"]
    expected_sender = {'code': 'operator@primary.example.invalid',
                       'stealth': 'operator@secondary.example.invalid'}[alias]
    if original.get('id')!=mid or not tid:raise ValueError('write_source_binding_missing')
    if svc.users().getProfile(userId='me').execute()['emailAddress'].casefold()!=expected_sender:
        raise ValueError('Authenticated mailbox mismatch')
    from mail_result_io import attempt_generation
    _attempt['item']={'account':alias,'id':mid,'threadId':tid}
    _attempt['write_generation']=attempt_generation(_attempt['item'])
    current = svc.users().threads().get(userId="me", id=tid, format="metadata", metadataHeaders=["In-Reply-To", "From"]).execute()
    for m in current.get("messages", []):
        if "DRAFT" in m.get("labelIds", []):
            return {"status": "draft_already_in_thread", "account": alias, "threadId": tid, "messageId": m["id"]}
    require_latest_source(current.get('messages', []), mid)
    if followup_to_last_sent:
        addresses = [address for _, address in getaddresses([h.get('to', '')]) if address]
        copies = [address for _, address in getaddresses([h.get('cc', '')]) if address]
        hidden_copies = [address for _, address in getaddresses([h.get('bcc', '')]) if address]
        if ('SENT' not in original.get('labelIds', []) or
                parseaddr(h.get('from', ''))[1].casefold() != expected_sender or
                len(addresses) != 1 or addresses[0].casefold() == expected_sender or copies or hidden_copies or
                to is not None or cc is not None):
            raise ValueError('Follow-up requires the latest own sent message with one external recipient and no CC')
        recipient = addresses[0]
    else:
        if parseaddr(h.get('from', ''))[1].casefold() == expected_sender:
            raise ValueError('own_sent_requires_followup_mode: use --followup-to-last-sent for an explicitly requested follow-up; never reply to the mailbox itself')
        recipient = parseaddr(h.get("reply-to") or h.get("from", ""))[1]
    if not recipient or "@" not in recipient:
        raise ValueError("Reply recipient missing or invalid")

    # Referral replies may target only contacts copied on the original message.
    if to is not None or cc is not None:
        allowed = {address.lower() for _, address in getaddresses([h.get("cc", "")]) if "@" in address}
        requested = [address.lower() for _, address in getaddresses(to or [])]
        copied = [address.lower() for _, address in getaddresses(cc or [])]
        if (not requested or len(requested) > 5 or len(requested) != len(set(requested))
                or any(address not in allowed for address in requested)
                or copied != [parseaddr(h.get("from", ""))[1].lower()]):
            raise ValueError("Referral addresses must be distinct original CC contacts with introducer in CC")

    msgid = h.get("message-id", "")
    msg = EmailMessage()
    msg["To"] = ", ".join(requested) if to is not None else recipient
    if cc is not None:
        msg["Cc"] = ", ".join(copied)
    subj = h.get("subject", "")
    msg["Subject"] = subj if subj.lower().startswith(("re:", "aw:")) else "Re: " + subj
    if msgid:
        msg["In-Reply-To"] = msgid
        msg["References"] = (h.get("references", "") + " " + msgid).strip()
    from mail_policy import format_body, body_html, mime_body_matches, scheduling
    _attempt['stage'] = 'format'
    text = format_body(text, slots)
    msg.set_content(text)
    rendered = body_html(text)
    msg.add_alternative(rendered, subtype="html")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    from email_realtime import calendar_service
    _attempt['stage'] = 'reservation'
    verified_project = None;hold_identity=None
    # Independently read full live originals at the write boundary. An
    # automatic model or Telegram caller cannot assert an Acribic project.
    from mail_policy import project_from_thread
    full = svc.users().threads().get(userId='me', id=tid, format='full').execute()
    require_latest_source(full.get('messages', []), mid)
    sources = []
    for m in complete_thread_sources(alias,full.get('messages', []),svc):
        mh = headers(m)
        sources.append({'id': m['id'], 'thread_id': m.get('threadId')or tid, 'labels': m.get('labelIds', []),
            'reference_source':bool(m.get('hermes_reference_source')),
            'from': mh.get('from'), 'to': mh.get('to'), 'cc': mh.get('cc'),
            'internalDate': m.get('internalDate'), 'text': body(m.get('payload', {})),
            'rfc_message_id': mh.get('message-id'), 'in_reply_to': mh.get('in-reply-to'),
            'references': mh.get('references'),'subject':mh.get('subject'),
            'truncated': False})
    verified_project = project_from_thread(alias, sources)
    if project is not None and project != verified_project:
        raise ValueError('Live mail project differs from prepared source')
    from mail_policy import proposal_identity
    hold_identity=proposal_identity((requested if to is not None else [recipient])+(copied if cc is not None else []),sources,company)
    from mail_result_io import review_before_write,begin_owned_write
    item={'account':alias,'id':mid,'threadId':tid}
    _attempt['item']=item
    recipients=(requested if to is not None else [recipient])+(copied if cc is not None else [])
    _attempt['stage']='semantic_review'
    review=review_before_write(item,sources,text,slots,recipients,project=verified_project,
        write_owner=write_owner,followup=followup_to_last_sent,primary_recipients=requested if to is not None else[recipient])
    if not(hold_identity or{}).get('company'):
        hold_identity=proposal_identity(recipients,sources,(review.get('understanding')or{}).get('company'))
    _attempt['stage']='reservation'
    with scheduling(calendar_service() if slots else None, alias, tid,
                    (requested if to is not None else [recipient]) + (copied if cc is not None else []), slots,
                    project=verified_project,identity=hold_identity,source_messages=sources,
                    primary_recipients=requested if to is not None else[recipient]):
        latest = svc.users().threads().get(userId="me", id=tid, format="metadata", metadataHeaders=["From"]).execute()
        if any("DRAFT" in m.get("labelIds", []) for m in latest.get("messages", [])):
            raise ValueError("Draft appeared during preflight; inspect existing draft")
        require_latest_source(latest.get('messages', []), mid)
        begin_owned_write(item,sources,text,slots,recipients,review)
        _attempt['write_started'] = True
        try:
            draft = svc.users().drafts().create(userId="me", body={"message": {"raw": raw, "threadId": tid}}).execute()
            check = svc.users().drafts().get(userId="me", id=draft["id"], format="full").execute()
        except Exception as error:
            raise DraftStateUnverified('Gmail draft write/readback uncertain; preserve holds and inspect without retry') from error
        try:
            saved = check.get("message", {})
            saved_headers = headers(saved)
            saved_to = [address.lower() for _, address in getaddresses([saved_headers.get("to", "")])]
            saved_cc = [address.lower() for _, address in getaddresses([saved_headers.get("cc", "")]) if address]
            if (saved.get("threadId") != tid or saved_to != (requested if to is not None else [recipient.lower()])
                    or saved_cc != (copied if cc is not None else [])
                    or body(saved.get("payload", {})).replace("\r\n", "\n").rstrip("\n") != text.replace("\r\n", "\n").rstrip("\n")
                    or not mime_body_matches(saved.get("payload", {}), text, rendered)
                    or "DRAFT" not in saved.get("labelIds", []) or "SENT" in saved.get("labelIds", [])):
                raise DraftStateUnverified("Draft created but recipient, body, or thread readback mismatched")
            return {"status": "created", "account": alias,'source_message_id':mid, "draftId": draft["id"],
                    'write_owner':review['owner'],
                    "threadId": tid, "to": requested if to is not None else recipient,
                    "draft_message_id": saved.get('id'),
                    "body_sha256": hashlib.sha256(text.encode()).hexdigest()}
        except DraftStateUnverified:
            raise
        except Exception as error:
            raise DraftStateUnverified('Gmail draft readback malformed; preserve holds and inspect without retry') from error


def revise_draft(alias, mid, draft_id, expected_message_id, expected_body_sha256, text, *, slots=(), project=None, company=None,write_owner=None):
    attempt={'write_started':False}
    try:
        result=_revise_draft(alias,mid,draft_id,expected_message_id,expected_body_sha256,text,
                             slots=slots,project=project,company=company,write_owner=write_owner,_attempt=attempt)
        from mail_result_io import finish_owned_write
        finish_owned_write(result)
        return result
    except (DraftStateUnverified,SourceChangedBeforeDraft):raise
    except Exception as error:
        if attempt['write_started']:raise DraftStateUnverified('Draft update may exist; inspect without replay')from error
        if getattr(error,'code',None)in{'mail_model_quota','mail_model_auth','mail_model_unavailable','mail_model_output_invalid','mail_model_runtime_failed'}:
            from mail_result_io import model_failed_before_write
            error.write_generation=attempt['write_generation']
            model_failed_before_write(attempt['item'],error,write_owner=write_owner,write_generation=attempt['write_generation'])
            raise
        from mail_policy import MailPolicyReviewRequired
        code='mail_text_validation'if isinstance(error,MailPolicyReviewRequired)else'slot_reservation_unavailable'
        raise SchedulingDraftReviewRequired('Existing draft was not updated',code=code,stage='revision_preflight',
            cause_type=type(error).__name__)from error


def _revise_draft(alias, mid, draft_id, expected_message_id, expected_body_sha256, text, *, slots=(), project=None, company=None,write_owner=None, _attempt=None):
    """Update only the exact previously verified native draft, never create/send.

    A caller's statement of ownership is insufficient: the current source,
    authenticated mailbox and exact before-image are checked again here.
    """
    from mail_policy import format_body,body_html,mime_body_matches,scheduling,project_from_thread,MailPolicyReviewRequired
    service_ = service(alias)
    expected={'code':'operator@primary.example.invalid','stealth':'operator@secondary.example.invalid'}[alias]
    if verified_profile(service_).casefold()!=expected:raise ValueError('mailbox_identity_mismatch')
    expected_mime=None
    def existing():
        nonlocal expected_mime
        value=service_.users().drafts().get(userId='me',id=draft_id,format='full').execute()['message']
        own=body(value.get('payload',{})).replace('\r\n','\n').rstrip('\n')
        if (value.get('id')!=expected_message_id or'DRAFT'not in value.get('labelIds',[])
                or'SENT'in value.get('labelIds',[])or hashlib.sha256(own.encode()).hexdigest()!=expected_body_sha256):
            raise SourceChangedBeforeDraft('existing_draft_changed_preserve_user_edit')
        current_mime=hashlib.sha256(json.dumps(value.get('payload',{}),ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        if expected_mime is None:expected_mime=current_mime
        elif current_mime!=expected_mime:
            raise SourceChangedBeforeDraft('existing_draft_mime_changed_preserve_user_edit')
        return value
    old=existing();tid=old['threadId'];old_headers=headers(old)
    raw_thread=service_.users().threads().get(userId='me',id=tid,format='full').execute()
    messages=raw_thread.get('messages',[]);require_latest_source(messages,mid)
    if [m['id']for m in messages if'DRAFT'in m.get('labelIds',[])]!=[expected_message_id]:
        raise SourceChangedBeforeDraft('draft_set_changed')
    source=next(m for m in messages if m['id']==mid);source_headers=headers(source)
    if source.get('threadId')!=tid:raise ValueError('write_source_binding_missing')
    from mail_result_io import attempt_generation
    _attempt['item']={'account':alias,'id':mid,'threadId':tid}
    _attempt['write_generation']=attempt_generation(_attempt['item'])
    if not source_headers.get('message-id')or old_headers.get('in-reply-to')!=source_headers['message-id']:
        raise ValueError('existing_draft_source_unverified')
    recipients=sorted({a.casefold()for _,a in getaddresses([old_headers.get('to',''),old_headers.get('cc','')])if a})
    if not recipients:raise ValueError('existing_draft_recipients_missing')
    from mail_result_io import owned_recipient_set
    from email_realtime import read_case
    if not owned_recipient_set(read_case(_attempt['item']),recipients):
        raise SourceChangedBeforeDraft('existing_draft_recipients_changed_preserve_user_edit')
    primary=[a.casefold()for _,a in getaddresses([old_headers.get('to','')])if a]
    sources=[]
    for message in complete_thread_sources(alias,messages,service_):
        if'DRAFT'in message.get('labelIds',[]):continue
        h=headers(message)
        sources.append({'id':message['id'],'thread_id':message.get('threadId')or tid,'labels':message.get('labelIds',[]),
            'reference_source':bool(message.get('hermes_reference_source')),
            'from':h.get('from'),'to':h.get('to'),'cc':h.get('cc'),'internalDate':message.get('internalDate'),
            'text':body(message.get('payload',{})),'rfc_message_id':h.get('message-id'),
            'in_reply_to':h.get('in-reply-to'),'references':h.get('references'),'subject':h.get('subject')})
    verified_project=project_from_thread(alias,sources)
    if project is not None and project!=verified_project:raise ValueError('business_project_account_mismatch')
    from mail_policy import proposal_identity
    identity=proposal_identity(recipients,sources,company)
    text=format_body(text,slots);rendered=body_html(text)
    msg=EmailMessage()
    for name in('To','Cc','Subject','In-Reply-To','References'):
        if old_headers.get(name.lower()):msg[name]=old_headers[name.lower()]
    msg.set_content(text);msg.add_alternative(rendered,subtype='html')
    raw=base64.urlsafe_b64encode(msg.as_bytes()).decode('ascii')
    from email_realtime import calendar_service
    from mail_result_io import review_before_write,begin_owned_write
    item={'account':alias,'id':mid,'threadId':tid}
    _attempt['item']=item
    _attempt['stage']='semantic_review'
    review=review_before_write(item,sources,text,slots,recipients,project=verified_project,write_owner=write_owner,primary_recipients=primary)
    if not identity.get('company'):identity=proposal_identity(recipients,sources,(review.get('understanding')or{}).get('company'))
    _attempt['stage']='reservation'
    with scheduling(calendar_service() if slots else None,alias,tid,recipients,slots,project=verified_project,identity=identity,source_messages=sources,primary_recipients=primary):
        existing()
        current=service_.users().threads().get(userId='me',id=tid,format='metadata',metadataHeaders=['From']).execute()
        require_latest_source(current.get('messages',[]),mid)
        begin_owned_write(item,sources,text,slots,recipients,review,revision={
            'draft_id':draft_id,'message_id':expected_message_id,'body_sha256':expected_body_sha256,'recipients':recipients,'slots':list(slots)})
        _attempt['write_started']=True
        try:
            service_.users().drafts().update(userId='me',id=draft_id,body={'message':{'raw':raw,'threadId':tid}}).execute()
            saved=service_.users().drafts().get(userId='me',id=draft_id,format='full').execute()['message']
        except Exception as error:
            raise DraftStateUnverified('Existing draft update/readback uncertain; inspect exact draft, never create another')from error
        saved_headers=headers(saved)
        actual_recipients=sorted({a.casefold()for _,a in getaddresses([saved_headers.get('to',''),saved_headers.get('cc','')])if a})
        if (saved.get('threadId')!=tid or'DRAFT'not in saved.get('labelIds',[])or'SENT'in saved.get('labelIds',[])
                or actual_recipients!=recipients or saved_headers.get('in-reply-to')!=source_headers['message-id']
                or not mime_body_matches(saved.get('payload',{}),text,rendered)):
            raise DraftStateUnverified('Updated draft content/identity readback unverified')
        return {'status':'updated','account':alias,'source_message_id':mid,'draftId':draft_id,'draft_message_id':saved['id'],
                'body_sha256':hashlib.sha256(text.encode()).hexdigest(),'threadId':tid,'write_owner':review['owner']}



def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", nargs="?", default="poll", choices=("poll", "thread", "draft", "status"))
    p.add_argument("--account", choices=ACCOUNTS)
    p.add_argument("--thread-id")
    p.add_argument("--message-id")
    p.add_argument("--body-file")
    p.add_argument("--slot", action="append", default=[], help="ISO8601 start with offset; repeat per proposed start")
    p.add_argument("--followup-to-last-sent", action="store_true",
                   help="Manual follow-up in the same thread, to the sole verified recipient of its latest SENT message")
    a = p.parse_args()
    if a.command == "poll": poll()
    elif a.command == "status":
        s = json.loads(STATE.read_text()) if STATE.exists() else {}
        print(json.dumps({"initialized": bool(s), "accounts": list(ACCOUNTS), "last_check": s.get("last_check"), "pending": len(s.get("pending", [])), "seq": s.get("event", {}).get("seq")}, ensure_ascii=False))
    elif a.command == "thread":
        if not a.account or not a.thread_id: p.error("thread requires --account and --thread-id")
        thread(a.account, a.thread_id)
    else:
        if not a.account or not a.message_id or not a.body_file: p.error("draft requires --account, --message-id and --body-file")
        text = Path(a.body_file).read_text(encoding="utf-8").strip()
        if not text: p.error("Draft body is empty")
        print(json.dumps(make_draft(a.account, a.message_id, text, slots=a.slot,
                                    followup_to_last_sent=a.followup_to_last_sent), ensure_ascii=False))


if __name__ == "__main__":
    try: main()
    except Exception as exc:
        print(f"email_watcher error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
