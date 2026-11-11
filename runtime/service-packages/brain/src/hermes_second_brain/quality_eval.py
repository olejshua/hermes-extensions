"""Separate source recall, answer correctness, citation validity and human review.

Standalone CLI; collection calls the actual Dreaming retrieval path. It never
imports documents, starts an agent, or changes the production vector index.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .dreaming.config import load_dreaming_config
from .dreaming.retrieval import retrieve_context


def write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def validate_spec(spec: dict) -> None:
    if spec.get("scope") not in {"synthetic_regression", "personal_memory"}:
        raise ValueError("spec requires an explicit evaluation scope")
    cases = spec.get("cases", [])
    if not cases or len(cases) > 200:
        raise ValueError("evaluation requires 1..200 cases")
    ids = [case["id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate case IDs")
    for case in cases:
        if type(case.get("answerable")) is not bool:
            raise ValueError("answerability must be explicitly labeled")
        if case.get("split") not in {"development", "held_out"}:
            raise ValueError("each case needs a development or held_out split")
        if case["answerable"] and not (case.get("expected_all") or case.get("expected_any")):
            raise ValueError("answerable cases require expected evidence")


def matches(source: str, expected: str) -> bool:
    return source == expected or (expected.startswith("viking://") and source.startswith(expected.rstrip("/") + "/"))


def identities(hit: dict) -> list[str]:
    provenance = hit.get("provenance") or {}
    return [str(value) for value in (hit.get("ref"), hit.get("source_id"),
            provenance.get("source_id"), provenance.get("source_uri")) if value]


def recall(case: dict, sources: list[str]) -> float | None:
    if not case["answerable"]:
        return None
    required, alternatives = case.get("expected_all", []), case.get("expected_any", [])
    found = lambda expected: any(matches(source, expected) for source in sources)
    value = sum(map(found, required)) / len(required) if required else 1.0
    return min(value, float(not alternatives or any(map(found, alternatives))))


def normalized(value: str) -> str:
    return " ".join(value.casefold().split()).rstrip(".!?")


def percentile(values: list[float], q: float) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values) * q) - 1)] if values else None


def evaluate(spec: dict, predictions: dict) -> dict:
    validate_spec(spec)
    supplied = predictions.get("cases", [])
    ids = [row["id"] for row in supplied]
    if len(set(ids)) != len(ids) or set(ids) - {case["id"] for case in spec["cases"]}:
        raise ValueError("duplicate or unknown prediction IDs")
    by_id = {row["id"]: row for row in supplied}
    rows = []
    for case in spec["cases"]:
        result = by_id.get(case["id"], {})
        hits = result.get("retrieved", [])
        sources = [source for hit in hits if str(hit.get("snippet", "")).strip() for source in identities(hit)]
        answer = result.get("answer")
        correct = grounded = citations_valid = None
        abstained = None
        if isinstance(answer, dict):
            abstained = answer.get("abstained") is True
            citations = answer.get("citations", [])
            citations_valid = bool(citations) and all(citation in sources for citation in citations)
            if not case["answerable"]:
                correct = abstained and not citations and not str(answer.get("text", "")).strip()
                grounded = correct
            elif case.get("accepted_answers"):
                correct = not abstained and normalized(str(answer.get("text", ""))) in {
                    normalized(value) for value in case["accepted_answers"]}
            review = answer.get("review", {})
            if isinstance(review.get("reviewer"), str) and review["reviewer"].strip():
                if type(review.get("correct")) is bool:
                    correct = review["correct"] and not abstained if case["answerable"] else review["correct"] and bool(correct)
                if type(review.get("grounded")) is bool:
                    grounded = review["grounded"] and (bool(citations_valid) if case["answerable"] else bool(correct))
            # Synthetic exact-answer fixtures have known, controlled evidence.
            # Personal free-form grounding always requires an explicit review.
            if spec["scope"] == "synthetic_regression" and case["answerable"]:
                grounded = bool(correct and citations_valid and recall(case, citations) == 1.0)
            if case["answerable"] and not citations_valid:
                grounded = False
        if result.get("status") != "ok":
            correct = grounded = None
        first = next((i for i, hit in enumerate(hits, 1)
                      if str(hit.get("snippet", "")).strip() and any(matches(source, expected) for source in identities(hit)
                             for expected in case.get("expected_all", []) + case.get("expected_any", []))), None)
        rows.append({"id": case["id"], "category": case["category"], "split": case["split"],
                     "label_status": case.get("label_status", "draft"),
                     "status": result.get("status", "missing"),
                     "recall_at_k": recall(case, sources) if result.get("status") == "ok" else (0.0 if case["answerable"] else None),
                     "reciprocal_rank": (1.0 / first if first else 0.0) if case["answerable"] else None,
                     "correct": correct, "grounded": grounded, "citations_valid": citations_valid,
                     "abstained": abstained, "answerable": case["answerable"],
                     "seconds": result.get("seconds"),
                     "context_characters": sum(len(hit.get("snippet", "")) for hit in hits),
                     "sources_with_date": sum(bool((h.get("provenance") or {}).get("source_date")) for h in hits),
                     "sources_with_uri": sum(bool((h.get("provenance") or {}).get("source_uri")) for h in hits),
                     "source_count": len(hits)})
    def average(key, subset=rows):
        values = [row[key] for row in subset if row[key] is not None]
        return sum(values) / len(values) if values else None
    times = [float(row["seconds"]) for row in rows if isinstance(row["seconds"], (int, float)) and math.isfinite(row["seconds"]) and row["seconds"] >= 0]
    reviewed = all(case.get("label_status") == "reviewed" and case.get("label_reviewer") for case in spec["cases"])
    all_scored = all(row["correct"] is not None and row["grounded"] is not None for row in rows)
    return {"scope": spec["scope"], "checked_at": datetime.now(timezone.utc).isoformat(),
            "spec_sha256": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
            "case_count": len(rows), "status_counts": dict(Counter(row["status"] for row in rows)),
            "answer_scored_count": sum(row["correct"] is not None for row in rows),
            "grounding_scored_count": sum(row["grounded"] is not None for row in rows),
            "avg_recall_at_k": average("recall_at_k"), "mrr": average("reciprocal_rank"),
            "answer_accuracy_on_scored": average("correct"), "grounding_on_scored": average("grounded"),
            "correct_abstention_on_scored": average("correct", [row for row in rows if not row["answerable"]]),
            "latency_p50_seconds": percentile(times, .5), "latency_p95_seconds": percentile(times, .95),
            "latency_sample_count": len(times), "labels_reviewed": bool(reviewed),
            "quality_gate_passed": bool(reviewed and all_scored and all(row["status"] == "ok" and row["correct"] and row["grounded"] for row in rows)),
            "by_category": {category: {"cases": sum(r["category"] == category for r in rows),
                "recall_at_k": average("recall_at_k", [r for r in rows if r["category"] == category]),
                "answer_accuracy_on_scored": average("correct", [r for r in rows if r["category"] == category])}
                for category in sorted({r["category"] for r in rows})},
            "by_split": {split: {"cases": sum(r["split"] == split for r in rows),
                "recall_at_k": average("recall_at_k", [r for r in rows if r["split"] == split]),
                "answer_accuracy_on_scored": average("correct", [r for r in rows if r["split"] == split])}
                for split in sorted({r["split"] for r in rows})},
            "scope_warning": "Synthetic cases validate mechanics, not personal-memory quality." if spec["scope"] == "synthetic_regression" else "Unreviewed labels or missing answer/grounding reviews cannot pass the quality gate.",
            "cases": rows}


def collect(spec: dict, manifest: Path, limit: int) -> dict:
    validate_spec(spec)
    config = load_dreaming_config(manifest)
    if not config.retrieval.enabled:
        raise ValueError("Dreaming retrieval is disabled")
    rows = []
    for case in spec["cases"][:limit]:
        namespace = case["namespace"]
        if namespace not in config.retrieval.namespaces:
            raise ValueError("case namespace is not enabled in the actual retrieval config")
        local = replace(config, retrieval=replace(config.retrieval, namespaces=(namespace,),
                        canonical_namespaces=tuple(n for n in config.retrieval.canonical_namespaces if n == namespace)))
        started = time.monotonic()
        outcome = retrieve_context([case["query"]], local,
                                   deadline_monotonic=started + config.retrieval.timeout_seconds)
        rows.append({"id": case["id"], "status": "ok" if outcome.queried and not outcome.failed else "failed",
                     "seconds": round(time.monotonic() - started, 3), "answer": None,
                     "retrieved": [{"ref": hit.ref, "snippet": hit.snippet, "provenance": hit.provenance} for hit in outcome.items]})
    return {"scope": "actual Dreaming retrieval; no answering model called", "cases": rows}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    score = sub.add_parser("score")
    score.add_argument("--spec", type=Path, required=True)
    score.add_argument("--predictions", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    gather = sub.add_parser("collect")
    gather.add_argument("--spec", type=Path, required=True)
    gather.add_argument("--manifest", type=Path, required=True)
    gather.add_argument("--cases", type=int, choices=range(1, 13), default=4)
    gather.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text())
    if args.command == "collect":
        report = collect(spec, args.manifest, args.cases)
        code = 0 if all(row["status"] == "ok" for row in report["cases"]) else 2
    else:
        report = evaluate(spec, json.loads(args.predictions.read_text()))
        code = 0 if report["quality_gate_passed"] else 2
    write_private(args.output, report)
    print(json.dumps({"output": str(args.output), "quality_gate_passed": report.get("quality_gate_passed"),
                      "cases": len(report["cases"])}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
