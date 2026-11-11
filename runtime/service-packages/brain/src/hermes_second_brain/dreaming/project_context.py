"""Read-only native project snapshots within the existing Dream source budget.

No model, task adoption, credentials store, business write or delivery. Full
selected originals either fit or are explicitly unavailable; never clip rows.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

VERSION = "dream-project-context/v1"
SOURCE = "monolith_project_context"
ACTIVE = frozenset({"active", "planning"})
CLOSED = frozenset({"done", "completed", "cancelled", "canceled", "archived", "resolved", "superseded", "rejected"})
GROUPS = {"tasks": "task", "planningGoals": "planning_goal", "openItems": "open_item",
          "documents": "note", "decisions": "decision"}


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(compact(value).encode()).hexdigest()


def closed(row: dict) -> bool:
    return row.get("completed") is True or str(row.get("status", "")).casefold() in CLOSED


def native_collect(reader, *, max_projects: int, character_budget: int, last_read: dict | None = None, views: bool = False) -> dict:
    """Use the installed, scoped native reader; account index is metadata only."""
    client = reader.NativeMCPReads.native()
    index = client.call("monolith_list_projects", {})
    rows = index.get("projects")
    if not isinstance(rows, list) or any(not isinstance(p, dict) for p in rows):
        raise ValueError("project_index_invalid")
    selection, ids = [], set()
    for row in rows:
        pid = str(uuid.UUID(row["id"]))
        if pid in ids:
            raise ValueError("project_index_identity_invalid")
        ids.add(pid)
        selection.append({"id": pid, "name": row.get("name"), "status": row.get("status"),
                          "selected": False})
    active = [p for p in selection if p["status"] in ACTIVE]
    packet = {"version": VERSION, "selection": selection, "contexts": [], "issues": []}
    last_read = last_read or {}
    active.sort(key=lambda p: float(last_read.get(p["id"], 0)))
    chosen = active[:max_projects]
    for project in chosen:
        project["selected"] = True
    for project in active[max_projects:]:
        packet["issues"].append({"project_id":project["id"], "project_name":project.get("name"),
                                 "reason":"project_context_deferred_by_round_budget"})
    for project in chosen:
        result = reader.read({"kind": "monolith_context", "project_id": project["id"]}, client=client)
        if not isinstance(result.get("data"), dict):
            reason = result.get("error")
            if reason not in {"monolith_scope_missing", "monolith_auth_unavailable", "monolith_source_missing_or_inaccessible", "monolith_rate_limited"}:
                reason = "native_context_unavailable"
            packet["issues"].append({"project_id": project["id"], "reason": reason})
            continue
        data = result["data"]
        # Count the complete serialized snapshot, including decisions and done
        # work. An 8 MiB provider allowance is not a Dream prompt allowance.
        characters = len(compact(data))
        if characters > character_budget and not views:
            packet["issues"].append({"project_id": project["id"], "reason": "project_context_over_budget",
                                     "characters": characters, "limit": character_budget})
            continue
        packet["contexts"].append(data)
    return packet


def validate_packet(packet: dict, config, now: datetime, *, focus="") -> list:
    """Build checkpointable native sources; capture time never creates novelty."""
    from .ingest import MessageCapsule, SessionCapsule
    if not isinstance(packet, dict) or packet.get("version") != VERSION:
        raise ValueError("native_context_packet_invalid")
    if not all(isinstance(packet.get(key), list) for key in ("contexts", "selection", "issues")):
        raise ValueError("native_context_packet_invalid")
    selection = [{**row,"name":str(row.get("name",""))[:200],"name_complete":False,"name_characters":len(str(row.get("name","")))}
                 if len(str(row.get("name","")))>200 else row for row in packet["selection"]]
    selected = {p["id"] for p in selection if isinstance(p, dict) and p.get("selected") is True
                and p.get("status") in ACTIVE}
    if len(selected) > config.budgets.sessions_per_round:
        raise ValueError("native_context_project_budget_invalid")
    if len(packet["contexts"]) > len(selected):
        raise ValueError("native_context_project_budget_invalid")
    present = {p.get("project", {}).get("id") for p in packet["contexts"]
               if isinstance(p, dict) and isinstance(p.get("project"), dict)}
    for missing in sorted(selected - present):
        if not any(i.get("project_id")==missing for i in packet["issues"] if isinstance(i,dict)):
            packet["issues"].append({"project_id":missing,"reason":"selected_project_context_missing"})
    from .working_context import ContextSources,view,archive_original
    result, seen = ContextSources(originals={p["project"]["id"]:p for p in packet["contexts"]}), set()
    for context in packet["contexts"]:
        project = context.get("project") if isinstance(context, dict) else None
        if (not isinstance(context, dict) or context.get("version") != "project-context-v1" or not isinstance(project, dict)
                or project.get("id") not in selected or project["id"] in seen):
            raise ValueError("native_context_identity_invalid")
        seen.add(project["id"])
        if len(compact(context)) > config.budgets.characters_per_batch and config.working_context is None:
            raise ValueError("native_context_source_over_budget")
        stamp = datetime.fromisoformat(context["capturedAt"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("native_context_capture_invalid")
        coverage = context.get("coverage")
        if (not isinstance(coverage, dict) or type(coverage.get("complete")) is not bool
                or not isinstance(coverage.get("missingScopes"), list)
                or not isinstance(coverage.get("unavailable"), list)
                or not isinstance(coverage.get("counts"), dict)
                or not isinstance(context.get("sources"), list)):
            raise ValueError("native_context_coverage_invalid")
        if coverage["complete"] and (coverage["missingScopes"] or coverage["unavailable"]):
            raise ValueError("native_context_coverage_invalid")
        for group in (*GROUPS, "assignments"):
            rows = context.get(group)
            if rows is None:
                if coverage["complete"]:
                    raise ValueError("native_context_section_missing")
            elif (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                  or coverage["counts"].get(group) != len(rows)):
                raise ValueError("native_context_count_invalid")
        if any(row.get("project_id") != project["id"] for row in context.get("tasks") or []):
            raise ValueError("native_context_task_identity_invalid")
        original = {k: v for k, v in context.items() if k != "capturedAt"}
        fingerprint = digest({"original": original,
                              "current": 0 <= (now - stamp).total_seconds() <= 86400})
        # Full original is carried only once per phase, outside the Telegram
        # scanner's clipping. Source dates and source versions remain original.
        selected_context = view(context, max(2000,config.budgets.characters_per_batch-1800),focus) if config.working_context else context
        envelope = {"context": selected_context, "selection": selection, "issues": packet["issues"]}
        if config.working_context:envelope["original_access"]=archive_original(config,context)
        text = compact(envelope)
        if len(text) > config.budgets.characters_per_batch:
            packet["issues"].append({"project_id": project["id"], "reason": "project_envelope_over_budget"})
            continue
        ref = f"monolith:{project['id']}#snapshot"
        result.append(SessionCapsule(profile="monolith", session_id=project["id"], source=SOURCE,
            title=str(project.get("name") or project["id"]), started_at=stamp.timestamp(),
            ended_at=stamp.timestamp(), fingerprint=fingerprint, has_user_content=False,
            messages=(MessageCapsule(ref, "assistant", text, stamp.timestamp(), digest(context), False, False),)))
    return result


def load_sources(config, now: datetime, *, deadline_monotonic: float | None = None, focus="") -> tuple[list, list]:
    if not config.project_context_command:
        return [], []
    try:
        command = [*config.project_context_command, "--max-projects", str(config.budgets.sessions_per_round),
                   "--character-budget", str(config.budgets.characters_per_batch),
                   "--last-read", compact(project_read_times(config))]
        if config.working_context is not None:command.append("--views")
        timeout = 60.0 if deadline_monotonic is None else min(60.0, deadline_monotonic - time.monotonic())
        if timeout <= 0:
            return [], [{"reason": "native_context_deadline"}]
        outcome = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
        # The collector limits each original before output. Defense in depth
        # against a misconfigured command; no stderr/source text in diagnostics.
        limit = (config.budgets.sessions_per_round*8*1024*1024+65536 if config.working_context else config.budgets.sessions_per_round * config.budgets.characters_per_batch * 4 + 16384)
        if outcome.returncode or len(outcome.stdout) > limit:
            return [], [{"reason": "native_context_transport_unavailable"}]
        packet = json.loads(outcome.stdout)
        sources = validate_packet(packet, config, now,focus=focus)
        return sources, packet["issues"]
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return [], [{"reason": "native_context_unavailable_or_invalid"}]


def _packets(sessions) -> list:
    return [json.loads(s.messages[0].text) for s in sessions if s.source == SOURCE]


def _redact_original(value, key=""):
    from .redaction import redact
    if isinstance(value, dict):
        return {k: _redact_original(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_original(v, key) for v in value]
    if isinstance(value, str) and key not in {"id", "version", "capturedAt", "type", "relation", "linkId", "status"} and not key.endswith(("_id", "_at")):
        return redact(value)
    return value


def phase_context(sessions) -> dict:
    packets = _packets(sessions)
    return {"status": "available" if packets else "not_in_this_batch", "projects": _redact_original(packets),
            "authority": "persisted_project_state_not_user_rules_or_permission"}


def evidence(sessions, config, now: datetime, *, originals=None) -> dict:
    from .provenance import source_day
    from .redaction import redact
    from .store import EvidenceRecord, snippet_hash
    result = {}
    for packet in _packets(sessions):
        data = packet["context"]
        project = data["project"]
        captured = datetime.fromisoformat(data["capturedAt"].replace("Z", "+00:00"))
        current = 0 <= (now - captured).total_seconds() <= 86400
        complete = data["coverage"]["complete"] and not any(
            issue.get("project_id") in {None,project["id"]} for issue in packet["issues"] if isinstance(issue,dict))
        original=(originals or {}).get(project["id"],data)
        versions = {(s.get("type"), s.get("id")): s.get("version") for s in original["sources"]}
        rows = [("project", project)]
        rows.extend((kind, row) for group, kind in GROUPS.items() for row in data.get(group) or [])
        for kind, row in rows:
            version = versions.get((kind, row.get("id")))
            verified = isinstance(version, str) and len(version) == 64 and set(version) <= set("e000000000000007")
            ref = f"monolith:{project['id']}#{kind}:{row.get('id')}"
            # Title/semantic content precede bookkeeping in the bounded quote.
            semantic = {key: row[key] for key in ("name", "title", "description", "notes", "content", "detail", "next_step", "status", "completed") if key in row}
            quote = redact(compact(semantic))[:config.budgets.evidence_snippet_characters]
            provenance = {"source": SOURCE, "source_uri": f"monolith:{kind}:{row.get('id')}",
                "source_version": version, "source_date": source_day(row.get("updated_at") or row.get("created_at")),
                "captured_at": data["capturedAt"], "evidence_status": "confirmed" if verified and current and complete else "unknown",
                "state_observed_at": data["capturedAt"], "project_id": project["id"], "entity_type": kind,
                "closed": closed(row) or closed(project), "coverage_complete": complete, "current": current,
                "authority": "persisted_state_not_user_rules_or_permission"}
            role = "canonical" if verified and current and complete else "assistant"
            result[ref] = EvidenceRecord(ref, "monolith", project["id"], role,
                captured.date().isoformat(), quote, snippet_hash(quote), provenance)
    return result


def project_read_times(config) -> dict:
    """Reuse source last_seen for fair reads; completion still gates new work."""
    if not config.dream_state_db.is_file():
        return {}
    try:
        with sqlite3.connect(config.dream_state_db.as_uri()+"?mode=ro",uri=True) as db:
            return {row[0]:row[1] or 0 for row in db.execute(
                "SELECT session_id,last_seen FROM sources WHERE profile='monolith'")}
    except sqlite3.Error:
        return {}


def selection(sources) -> list:
    rows = {}
    for packet in _packets(sources):
        rows.update((p["id"],p) for p in packet["selection"])
    return list(rows.values())


def mentioned(text: str, value: Any) -> bool:
    return isinstance(value,str) and len(value.strip()) >= 3 and bool(
        re.search(r"(?<!\w)"+re.escape(value.casefold())+r"(?!\w)",text.casefold()))


def review_sources(batch, current_sources, config) -> tuple[list, list]:
    """A seen snapshot remains review evidence; only changes trigger a run."""
    text = " ".join(m.text for s in batch if s.source!=SOURCE for m in s.messages if m.role=="user")
    existing = {s.source_key for s in batch}
    needed = [s for s in current_sources if s.source_key not in existing
              and (mentioned(text,s.title) or mentioned(text,s.session_id))]
    review, issues = [], []
    characters = sum(s.character_count() for s in batch)
    for source in needed:
        if (len(batch)+len(review) >= config.budgets.sessions_per_round
                or characters+source.character_count()>config.budgets.characters_per_batch):
            issues.append({"project_id":source.session_id,"reason":"project_review_context_over_round_budget"})
            continue
        review.append(source)
        characters += source.character_count()
    return review, issues


def proposal_gate(insight, catalog: dict, *, enabled: bool, action_review: dict | None = None) -> str | None:
    """Check explicit semantic action bindings, never infer intent from citations."""
    if not enabled or insight.get("reflection", {}).get("scope") != "ole_work":
        return None
    native = [catalog[e["ref"]] for e in insight["evidence"]
              if catalog[e["ref"]].provenance.get("source") == SOURCE]
    if any(e.role != "canonical" for e in native):
        return "Projektquelle ist veraltet, unvollständig oder nicht versionsbelegt."
    if not isinstance(action_review,dict) or action_review.get("relation")=="unknown":
        return "Zielhandlung und historische Belege sind noch nicht unabhängig eingeordnet."
    refs=action_review.get("target_refs")
    if not isinstance(refs,list):return "Die ausdrückliche Zielbindung fehlt."
    if action_review.get("relation")=="independent_new_idea":
        # Completed results can support a genuinely new inference/experiment.
        # All seven semantic quality/novelty gates still apply separately.
        return None if not refs else "Eine unabhängige neue Idee darf keine bestehende Arbeit als Ziel ausgeben."
    if action_review.get("relation")!="existing_project_work" or not refs:
        return "Die ausdrücklich gemeinte bestehende Projektarbeit ist nicht belegt."
    for ref in refs:
        target=catalog.get(ref)
        if target is None or target.provenance.get("source")!=SOURCE or target.role!="canonical":
            return "Die ausdrücklich gemeinte Zielarbeit ist nicht aktuell und vollständig belegt."
        if target.provenance.get("closed") is True:
            return "Die ausdrücklich vorgeschlagene Zielarbeit ist bereits abgeschlossen."
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-reader", type=Path, required=True)
    parser.add_argument("--max-projects", type=int, required=True)
    parser.add_argument("--character-budget", type=int, required=True)
    parser.add_argument("--last-read", default="{}")
    parser.add_argument("--views",action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.max_projects <= 8 or not 1000 <= args.character_budget <= 100000:
        raise ValueError("native_context_budget_invalid")
    spec = importlib.util.spec_from_file_location("dream_native_project_reads", args.native_reader)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    print(compact(native_collect(reader, max_projects=args.max_projects, character_budget=args.character_budget,
                                 last_read=json.loads(args.last_read),views=args.views)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError):
        print(compact({"version": VERSION, "selection": [], "contexts": [],
                       "issues": [{"reason": "native_project_read_unavailable"}]}))
        raise SystemExit(1)
