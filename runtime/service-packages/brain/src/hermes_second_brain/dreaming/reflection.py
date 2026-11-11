"""On-demand committed reflections; existing Dream reports own delivery claims.

No external actions, new queue, memory writes or implicit repair approvals.
The pure read surface opens SQLite mode=ro and never creates/migrates a store.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DreamingConfig, load_dreaming_config
from .ingest import readonly_uri
from .redaction import redact
from .schemas import QUALITY_CRITERIA
from .store import DreamStore

SCHEMA_VERSION = "dream-reflection/v1"


def quality_is_useful(review: object) -> bool:
    """Validate the persisted Deep conclusion, not a substitute for AI review."""
    return (isinstance(review, dict)
            and review.get("version") == "dream-quality/v1"
            and review.get("decision") == "useful"
            and bool(re.fullmatch(r"(?:ole_work|hermes):[a-z0-9][a-z0-9-]{5,85}",
                                 str(review.get("novelty_key", ""))))
            and review.get("duplicate_of") == []
            and isinstance(review.get("summary"), str) and bool(review["summary"].strip())
            and all(isinstance(review.get(key), dict)
                    and review[key].get("passed") is True
                    and isinstance(review[key].get("reason"), str)
                    and bool(review[key]["reason"].strip()) for key in QUALITY_CRITERIA))


def read_reflections(config: DreamingConfig, *, limit: int = 5,
                     include_hypotheses: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "items": [],
                              "action_authorized": False, "executed": False}
    path = config.dream_state_db
    if not path.is_file() or path.is_symlink():
        return result
    with sqlite3.connect(readonly_uri(path), uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(insights)")}
        if "reflection_json" not in columns:
            return result
        rows = conn.execute(
            "SELECT i.* FROM insights i JOIN runs r ON i.run_id=r.run_id "
            "WHERE r.status='complete' AND i.deep_supported=1 "
            "ORDER BY r.finished_at DESC,i.confidence DESC,i.insight_id LIMIT 100"
        ).fetchall()
    for row in rows:
        item = _record(dict(row), include_hypotheses=include_hypotheses)
        if item is not None:
            result["items"].append(item)
        if len(result["items"]) >= max(1, min(20, int(limit))):
            break
    return result


def _record(row: dict[str, Any], *, include_hypotheses: bool) -> dict[str, Any] | None:
    meta = json.loads(row["reflection_json"] or "{}")
    if meta.get("scope") not in {"ole_work", "hermes"}:
        return None
    status, kind = row["status"], row["kind"]
    verdict = row.get("deep_verdict")
    if verdict in {None, "defer", "reject"}:
        return None
    if kind == "proposal" and status == "proposed":
        classification = "proposal"
    elif status == "grounded" and kind not in {"contradiction", "stale_conflict", "proposal", "hypothesis"}:
        classification = "fact" if kind == "fact" else "connection"
    elif include_hypotheses:
        classification = "hypothesis"
    else:
        return None
    evidence = []
    for source in json.loads(row["evidence_json"] or "[]"):
        provenance = source.get("provenance") or {}
        quote = redact(str(source.get("quote") or ""))[:400]
        evidence.append({"ref": source["ref"], "role": source["role"],
                         "day": source.get("day", "unknown"), "quote": quote,
                         "excerpt_sha256": hashlib.sha256(quote.encode()).hexdigest(),
                         "source_uri": provenance.get("source_uri"),
                         "source_version": provenance.get("source_version"),
                         "message_sha256": provenance.get("message_sha256"),
                         "evidence_status": provenance.get("evidence_status", "unknown")})
        if provenance.get("source") == "monolith_project_context":
            # Snapshot observation time must not overwrite the original fact
            # date in consumers or saved review handoffs. Legacy hashes stay.
            evidence[-1].update({key: provenance.get(key) for key in (
                "source_date", "captured_at", "state_observed_at", "authority",
                "project_id", "entity_type", "coverage_complete")})
    if not evidence or not any(x["role"] in {"user", "canonical"} for x in evidence):
        return None
    if not all(x["source_version"] for x in evidence):
        return None
    item = {"schema_version": SCHEMA_VERSION, "insight_id": row["insight_id"],
            "run_id": row["run_id"], "created_at": row["created_at"],
            "classification": classification, "scope": meta["scope"],
            "claim": row["claim"], "detail": row["detail"],
            "proposed_result": meta.get("proposed_result", ""),
            "expected_check": meta.get("expected_check", ""),
            "uncertainty": meta.get("uncertainty", ""),
            "importance": meta.get("importance", "normal"),
            "novelty": meta.get("novelty", "unknown"),
            "language": meta.get("language", "unknown"),
            "confidence": row["confidence"], "evidence": evidence,
            "deterministic_status": "supported_proposal" if status == "proposed" else status,
            "deep_supported": True, "deep_verdict": verdict,
            "deep_rationale": row.get("deep_rationale", ""),
            "requires_approval": True, "action_authorized": False, "executed": False,
            "authority": "reflection_not_user_rule"}
    if "quality_review" in meta:
        # Additive v1 field. Existing records/hashes stay byte-for-byte stable.
        item["quality_review"] = meta["quality_review"]
    if "action_review" in meta:
        item["action_review"] = meta["action_review"]
    frozen = dict(item)
    if frozen["language"] == "unknown":
        frozen.pop("language")  # Preserve v1 hashes for already committed rows.
    digest = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    item["item_sha256"] = digest
    item["item_id"] = row["insight_id"]
    return item


def claim_important_reflection(config: DreamingConfig, *, owner: str,
                               now: datetime | None = None,
                               suppressed_insight_ids: set[str] | None = None) -> dict[str, Any] | None:
    """Freeze independently useful items in one existing report, without a daily quota.

    Reading never claims or sends. Each item's durable reservation/attempt is
    independent of the calendar day. Unknown/failed/crashed attempts never retry.
    The batch size is the existing generation resource budget, not a daily goal.
    """
    current = (now or config.now()).astimezone(config.tzinfo())
    evidence_cutoff = (current - timedelta(days=7)).date().isoformat()
    items = [i for i in read_reflections(config, limit=20)["items"]
             if i["item_id"] not in (suppressed_insight_ids or set())
             if i["importance"] == "important" and i["confidence"] >= 0.8
             and i["novelty"] == "new_experiment"
             and i["language"] == "de"
             and quality_is_useful(i.get("quality_review"))
             and any(e.get("role") in {"user", "canonical"}
                     and evidence_cutoff <= str(e.get("day") or "") <= current.date().isoformat()
                     for e in i["evidence"])
             and current.timestamp() - 7 * 86400 <= i["created_at"] <= current.timestamp()
             and i["proposed_result"] and i["expected_check"]]
    if not items or not config.dream_state_db.is_file():
        return None
    store = DreamStore(config.dream_state_db)
    return store.claim_reflection_bundle(
        owner=owner, now=current.timestamp(), delivery_day=current.date().isoformat(),
        items=[{"item_id": i["item_id"], "item_sha256": i["item_sha256"],
                "run_id": i["run_id"], "novelty_key": i["quality_review"]["novelty_key"],
                "text": f"Vorschlag: {i['proposed_result']}"} for i in items],
        limit=max(1, min(20, config.budgets.max_connections_per_round)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--include-hypotheses", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = read_reflections(load_dreaming_config(args.config), limit=args.limit,
                                  include_hypotheses=args.include_hypotheses)
    except (OSError, ValueError, sqlite3.Error, KeyError):
        print(json.dumps({"schema_version": SCHEMA_VERSION, "abstain": True,
                          "reason": "reflection_store_unavailable", "action_authorized": False}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
