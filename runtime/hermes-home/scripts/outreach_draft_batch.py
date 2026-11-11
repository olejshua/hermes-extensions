#!/usr/bin/env python3
# Scheduling-bearing outreach bodies fail closed through the central mail policy.
"""Review-gated outreach drafting: never send, never alter Gmail/CRM state except drafts.create.

Two isolated invocations, one for each account. A draft is created only after
an account/profile check and fresh domain-level Gmail collision check. The
idempotent ledger is persisted after each verified draft; all outcomes are
reported even if a later candidate fails.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from email_watcher import service, headers, body, canonical_draft_signature

EXPECTED = {"code": "operator@primary.example.invalid", "stealth": "operator@secondary.example.invalid"}
CHAT = "telegram:5644072286"
HERMES = "/home/hermes/.local/bin/hermes"
EMAIL = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.I)
BLOCK = re.compile(r"(^|[.@])(?:no[._-]?reply|postmaster|mailer-daemon|support|jobs|karriere)(?:[.@]|$)", re.I)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate_plan(plan, alias):
    if alias not in EXPECTED or plan.get("account") not in (alias, EXPECTED[alias]):
        raise ValueError("Requested account does not match candidate plan")
    rows = plan.get("candidates")
    if not isinstance(rows, list) or len(rows) != 25:
        raise ValueError("Plan must contain exactly 25 candidates")
    seen_email, seen_domain = set(), set()
    for n, row in enumerate(rows, 1):
        addr = row.get("to", "").strip().lower()
        if not EMAIL.fullmatch(addr) or BLOCK.search(addr):
            raise ValueError(f"Candidate {n}: missing, unsafe or invalid recipient")
        domain = addr.split("@", 1)[1]
        if addr in seen_email or domain in seen_domain:
            raise ValueError(f"Candidate {n}: duplicate recipient or domain within plan")
        seen_email.add(addr); seen_domain.add(domain)
        for key in ("company", "subject", "body", "source", "verification"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"Candidate {n}: missing {key}")
        if "\n" in row["subject"] or "\r" in row["subject"] or len(row["subject"]) > 180:
            raise ValueError(f"Candidate {n}: invalid subject")
        if len(row["body"]) > 10000 or "\x00" in row["body"]:
            raise ValueError(f"Candidate {n}: invalid body")
        if alias == "stealth" and re.search(r"250\s+Gründer|250\s+Founder", row["body"], re.I):
            raise ValueError(f"Candidate {n}: stale 250-founder claim")
        if not re.search(r"smtp|serverprüfung", row["verification"], re.I):
            raise ValueError(f"Candidate {n}: no recorded positive mailbox verification")
    return rows


def mailbox_collision(svc, addr):
    """Search from/to/cc of the whole domain, conservatively blocking any hit.

    Gmail API does not expand aliases in q; this search may miss another
    legal entity of the same group. The research plan must cover those too.
    """
    domain = addr.rsplit("@", 1)[1]
    q = f"in:anywhere {{to:{domain} from:{domain} cc:{domain}}}"
    result = svc.users().messages().list(userId="me", q=q, maxResults=10, includeSpamTrash=True).execute()
    hits = result.get("messages", [])
    if hits:
        return {"query": q, "first_message": hits[0]["id"], "estimate": result.get("resultSizeEstimate", len(hits))}
    return None


def readback(svc, did, row, alias):
    item = svc.users().drafts().get(userId="me", id=did, format="full").execute()
    msg = item.get("message", {})
    h = headers(msg)
    labels = set(msg.get("labelIds", []))
    if "DRAFT" not in labels or "SENT" in labels:
        raise RuntimeError(f"Draft {did}: unexpected Gmail label state")
    if parseaddr(h.get("to", ""))[1].lower() != row["to"].lower():
        raise RuntimeError(f"Draft {did}: wrong recipient")
    if parseaddr(h.get("from", ""))[1].lower() != EXPECTED[alias]:
        raise RuntimeError(f"Draft {did}: wrong sender")
    if h.get("subject", "") != row["subject"]:
        raise RuntimeError(f"Draft {did}: wrong subject")
    if body(msg.get("payload", {})).strip() != canonical_draft_signature(row["body"], force_formal=True):
        raise RuntimeError(f"Draft {did}: wrong body")
    if not msg.get("id") or not msg.get("threadId"):
        raise RuntimeError(f"Draft {did}: incomplete Gmail readback")
    return {"id": did, "messageId": msg["id"], "threadId": msg["threadId"], "to": row["to"], "company": row["company"], "status": "verified_draft"}

def notify_draft(verified, alias):
    """Notify only after Gmail readback; one message per new saved draft."""
    project = "Acribic" if alias == "code" else "Stealth Campus"
    text = (f"Neuer Gmail-Entwurf ({project}), nicht versendet: "
            f"{verified['company']} · {verified['to']} · Draft {verified['id']}")
    sent = subprocess.run([HERMES, "send", "--to", CHAT, "--quiet", "--file", "-"],
                          input=text, text=True, capture_output=True, timeout=30)
    if sent.returncode:
        raise RuntimeError(f"Draft {verified['id']} saved, but notification failed: {sent.stderr[-300:]}")


def run(alias, planpath, execute, ledgerpath):
    plan = json.loads(planpath.read_text(encoding="utf8"))
    rows = validate_plan(plan, alias)
    svc = service(alias)
    actual = svc.users().getProfile(userId="me").execute()["emailAddress"].lower()
    if actual != EXPECTED[alias]:
        raise RuntimeError(f"Connected Gmail profile differs for {alias}: {actual}")
    ledger = json.loads(ledgerpath.read_text(encoding="utf8")) if ledgerpath.exists() else {"account": alias, "plan": str(planpath), "verified": [], "skipped": [], "notified": []}
    if ledger["account"] != alias or ledger["plan"] != str(planpath):
        raise ValueError("Ledger account/plan mismatch")
    completed = {z["to"].lower(): z for z in ledger["verified"]}
    for i, row in enumerate(rows, 1):
        addr = row["to"].lower()
        if addr in completed:
            # A previously recorded external write must still exist before skipping it.
            checked = readback(svc, completed[addr]["id"], row, alias)
            if checked["id"] not in ledger.get("notified", []):
                notify_draft(checked, alias)
                ledger.setdefault("notified", []).append(checked["id"])
                atomic_json(ledgerpath, ledger)
            print(json.dumps({"index": i, "account": alias, "action": "existing_verified", **checked}, ensure_ascii=False), flush=True)
            continue
        collision = mailbox_collision(svc, addr)
        if collision:
            item = {"index": i, "to": addr, "company": row["company"], "reason": "gmail_domain_collision", "evidence": collision}
            if item not in ledger["skipped"]:
                ledger["skipped"].append(item)
            if execute: atomic_json(ledgerpath, ledger)
            print(json.dumps({"account": alias, "action": "skipped", **item}, ensure_ascii=False), flush=True)
            continue
        if not execute:
            print(json.dumps({"index": i, "account": alias, "action": "eligible", "to": addr, "company": row["company"]}, ensure_ascii=False), flush=True)
            continue
        msg = EmailMessage()
        msg["From"] = EXPECTED[alias]
        msg["To"] = addr
        msg["Subject"] = row["subject"]
        msg.set_content(canonical_draft_signature(row["body"], force_formal=True))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        created = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        # If readback fails, stop. Do not retry create: resource might already exist.
        verified = readback(svc, created["id"], row, alias)
        ledger["verified"].append(verified)
        atomic_json(ledgerpath, ledger)
        notify_draft(verified, alias)
        ledger.setdefault("notified", []).append(verified["id"])
        atomic_json(ledgerpath, ledger)
        print(json.dumps({"index": i, "account": alias, "action": "created", **verified}, ensure_ascii=False), flush=True)
    print(json.dumps({"account": alias, "verified": len(ledger["verified"]), "skipped": len(ledger["skipped"]), "mode": "execute" if execute else "dry_run"}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--account", required=True, choices=EXPECTED)
    p.add_argument("--plan", required=True, type=Path)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    a = p.parse_args()
    ledger = a.plan.with_suffix(".ledger.json")
    run(a.account, a.plan.resolve(), a.execute, ledger)

if __name__ == "__main__":
    try: main()
    except Exception as exc:
        print(f"outreach_draft_batch: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
