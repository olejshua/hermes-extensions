"""Thin, initially disabled bridge to the existing native send_message handler.

Report bundle + attempt are persisted before send. Any attempted outcome consumes
its individual ideas; crash/timeout/unknown never repeats them. No daily quota.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Callable

from .config import load_dreaming_config
from .reflection import claim_important_reflection, read_reflections, quality_is_useful
from .store import DreamStore, deterministic_id


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()


def _short_plain_text(text: str) -> bool:
    return (20 <= len(text) <= 420 and not re.search(r"[\n\r<>]|MEDIA:|\[\[|https?://|[\w.+-]+@[\w.-]+|\b(?:ins_|run_|cand_|rpt_|viking:)\w*|\b[0-9a-f]{24,}\b",text)
            and not any(unicodedata.category(c) in {"So","Cs","Co"} for c in text)
            and len(re.findall(r"[.!?](?:\s|$)",text)) <= 2)


def send_important_reflection(config_path: Path, *, sender: Callable | None = None,
                              owner: str | None = None) -> dict:
    packet=json.loads(Path(config_path).read_text())
    policy=packet.get("dreaming",{}).get("reflection_delivery",{})
    if not isinstance(policy,dict) or policy.get("enabled") is not True:
        return {"state":"disabled"}
    config=load_dreaming_config(config_path)
    target=policy.get("target","")
    match=re.fullmatch(r"telegram:([1-9][0-9]{0,19})",str(target))
    if not match or match.group(1) not in config.allowed_telegram_user_ids:
        return {"state":"blocked","reason":"fixed_private_destination_required"}
    items=read_reflections(config,limit=20)["items"]
    suppressed=set(policy.get("suppressed_insight_ids",[]))
    eligible={i["item_id"]:i for i in items if i["item_id"] not in suppressed}
    if not eligible:
        return {"state":"quiet"}
    claim_owner=owner or "reflection-"+str(time.time_ns())
    unsuitable={i["item_id"] for i in eligible.values()
                if not _short_plain_text(f"Vorschlag: {i['proposed_result']}")}
    delivery=claim_important_reflection(config,owner=claim_owner,
                                       suppressed_insight_ids=suppressed|unsuitable)
    if delivery is None:
        return {"state":"quiet"}
    store=DreamStore(config.dream_state_db)
    frozen=delivery.get("items") or [delivery]
    if (not frozen or delivery["text"] != "\n\n".join(i["text"] for i in frozen)
            or any(not eligible.get(i["item_id"])
                   or eligible[i["item_id"]]["item_sha256"]!=i["item_sha256"]
                   or eligible[i["item_id"]]["language"]!="de"
                   or not quality_is_useful(eligible[i["item_id"]].get("quality_review"))
                   or not _short_plain_text(i["text"]) for i in frozen)):
        # No send. Preserve claim as a quiet review block rather than cycling.
        return {"state":"blocked","reason":"reflection_changed_or_text_unsuitable"}
    args={"action":"send","target":target,"message":delivery["text"]}
    attempt={"schema_version":"dream-native-attempt/v1","attempt_id":deterministic_id("dat",delivery["delivery_id"],_sha(args)),
             "delivery_id":delivery["delivery_id"],"item_id":delivery["item_id"],"item_sha256":delivery["item_sha256"],
             "target_sha256":_sha(target),"message_sha256":_sha(delivery["text"]),
             "arguments_sha256":_sha(args),"started_at":time.time(),"state":"attempted","receipt":None}
    if "items" in delivery:
        attempt["items"]=delivery["items"]
    if not store.begin_reflection_attempt(delivery["delivery_id"],claim_owner,attempt):
        return {"state":"unknown","reason":"attempt_already_started"}
    if sender is None:
        # Native 3.14 + hermes_bootstrap is required by the script entry point.
        from tools.send_message_tool import send_message_tool
        sender=send_message_tool
    try:
        raw=sender(args)
        receipt=json.loads(raw) if isinstance(raw,str) else raw
        if (isinstance(receipt,dict) and receipt.get("success") is True
                and str(receipt.get("message_id") or "").isdigit()
                and int(receipt["message_id"])>0
                and receipt.get("skipped") is not True and receipt.get("partial_success") is not True
                and not receipt.get("error")):
            bound={"success":True,"message_id":str(receipt["message_id"]),
                   "attempt_id":attempt["attempt_id"],"arguments_sha256":attempt["arguments_sha256"],
                   "platform":"telegram","target_sha256":attempt["target_sha256"]}
            if store.ack_report(delivery["delivery_id"],claim_owner,native_receipt=bound):
                return {"state":"delivered","delivery_id":delivery["delivery_id"],"receipt":bound,
                        "item_ids":[i["item_id"] for i in frozen]}
        state="failed" if isinstance(receipt,dict) and receipt.get("success") is False else "unknown"
    except Exception:
        state="unknown"
    # Deliberately do not catch BaseException: a real crash must leave the
    # durable attempted row, which also blocks every automatic replay.
    store.finish_reflection_attempt(delivery["delivery_id"],attempt["attempt_id"],state)
    return {"state":state,"delivery_id":delivery["delivery_id"],"retry_authorized":False}


def send_stored_report(config_path: Path, *, sender: Callable | None = None, owner: str | None = None) -> dict:
    """Send the next stored Dream report natively. Disabled unless dreaming.report_delivery.enabled is true.

    Delivered means a native receipt bound to this attempt (id, argument hash, target, platform, report).
    Stdout hand-off through `dream-report --claim` stays legacy_unverified. Never replays an attempt."""
    packet=json.loads(Path(config_path).read_text())
    policy=packet.get("dreaming",{}).get("report_delivery",{})
    if not isinstance(policy,dict) or policy.get("enabled") is not True:
        return {"state":"disabled"}
    config=load_dreaming_config(config_path)
    target=policy.get("target","")
    match=re.fullmatch(r"telegram:([1-9][0-9]{0,19})",str(target))
    if not match or match.group(1) not in config.allowed_telegram_user_ids:
        return {"state":"blocked","reason":"fixed_private_destination_required"}
    from .runner import claim_dream_report
    claim_owner=owner or "report-"+str(time.time_ns())
    delivery=claim_dream_report(config,owner=claim_owner)
    if delivery is None:
        return {"state":"quiet"}
    store=DreamStore(config.dream_state_db)
    with store.transaction() as conn:
        fingerprint=conn.execute("SELECT fingerprint FROM reports WHERE report_id=?",(delivery.report_id,)).fetchone()[0]
    args={"action":"send","target":target,"message":delivery.text}
    attempt={"schema_version":"dream-report-attempt/v1","attempt_id":deterministic_id("dra",delivery.report_id,_sha(args)),
             "report_id":delivery.report_id,"fingerprint":fingerprint,"target_sha256":_sha(target),
             "message_sha256":_sha(delivery.text),"arguments_sha256":_sha(args),"platform":"telegram",
             "started_at":time.time(),"state":"attempted"}
    if not store.begin_report_attempt(delivery.report_id,claim_owner,attempt):
        return {"state":"unknown","reason":"attempt_not_recorded"}
    if sender is None:
        from tools.send_message_tool import send_message_tool
        sender=send_message_tool
    try:
        raw=sender(args)
        receipt=json.loads(raw) if isinstance(raw,str) else raw
        if isinstance(receipt,dict) and receipt.get("success") is True:
            bound={**{k:receipt.get(k) for k in ("success","message_id","error","skipped","partial_success") if k in receipt},
                   **{k:attempt[k] for k in ("attempt_id","arguments_sha256","target_sha256","platform","report_id")}}
            if store.ack_report_receipt(delivery.report_id,claim_owner,bound):
                return {"state":"delivered","report_id":delivery.report_id}
        state="failed" if isinstance(receipt,dict) and receipt.get("success") is False else "unknown"
    except Exception:
        state="unknown"
    store.finish_report_attempt(delivery.report_id,attempt["attempt_id"],state)
    return {"state":state,"report_id":delivery.report_id,"retry_authorized":False}
