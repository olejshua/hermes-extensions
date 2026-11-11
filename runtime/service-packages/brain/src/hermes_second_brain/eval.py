from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ov_adapter import safe_namespace


@dataclass(frozen=True)
class EvalResult:
    query: str
    recall_at_k: float
    mrr: float
    context_bytes: int
    hits: list[str]


def run_eval(spec_path: Path, output_path: Path, ov_binary: str = "ov", threshold: float = 0.75, timeout: float = 60.0) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    k = int(spec.get("k", 5))
    max_context_bytes = int(spec.get("max_context_bytes", 200_000))
    results: list[EvalResult] = []
    for case in spec["queries"]:
        subtree = f"viking://resources/{safe_namespace(str(case.get('namespace', 'brain')))}"
        cmd = [ov_binary, "find", str(case["query"]), "-u", subtree, "--limit", str(k), "-L", "0,1,2", "-o", "json"]
        proc = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        hits = _parse_hits(proc.stdout)
        expected = [str(x) for x in case.get("expected_source_ids", [])]
        expected_any = [str(x) for x in case.get("expected_any_source_ids", [])]
        if not expected and not expected_any:
            raise ValueError("recall eval case requires expected_source_ids or expected_any_source_ids")
        expected_set = set(expected)
        found_expected = {
            expected_id
            for expected_id in expected_set
            if any(_matches_expected(hit, expected_id) for hit in hits)
        }
        required_recall = len(found_expected) / max(1, len(expected_set)) if expected_set else 1.0
        any_recall = 1.0 if not expected_any or any(
            _matches_expected(hit, expected_id)
            for expected_id in expected_any
            for hit in hits
        ) else 0.0
        recall = min(required_recall, any_recall)
        mrr = 0.0
        for idx, hit in enumerate(hits, start=1):
            if any(_matches_expected(hit, expected_id) for expected_id in (*expected, *expected_any)):
                mrr = 1.0 / idx
                break
        results.append(EvalResult(str(case["query"]), recall, mrr, len(proc.stdout.encode("utf-8")), hits))
    avg_recall = sum(r.recall_at_k for r in results) / max(1, len(results))
    avg_mrr = sum(r.mrr for r in results) / max(1, len(results))
    max_observed_context_bytes = max((r.context_bytes for r in results), default=0)
    report: dict[str, Any] = {
        "threshold": threshold,
        "max_context_bytes": max_context_bytes,
        "max_observed_context_bytes": max_observed_context_bytes,
        "context_within_budget": max_observed_context_bytes <= max_context_bytes,
        "passed": avg_recall >= threshold and max_observed_context_bytes <= max_context_bytes,
        "avg_recall_at_k": avg_recall,
        "avg_mrr": avg_mrr,
        "results": [r.__dict__ for r in results],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 2


def _parse_hits(stdout: str) -> list[str]:
    data = _load_json_output(stdout)
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, dict):
            data = [
                item
                for key in ("memories", "resources", "skills")
                for item in result.get(key, [])
                if isinstance(item, dict)
            ]
        else:
            data = data.get("results") or data.get("hits") or data.get("items") or []
    hits = []
    for item in data:
        if isinstance(item, dict):
            value = _source_id_from_item(item)
            if value:
                hits.append(str(value))
    return hits


def _load_json_output(stdout: str) -> Any:
    """Parse OV JSON even when the CLI prefixes machine output with a `cmd:` line."""
    if not stdout.strip():
        return []
    decoder = json.JSONDecoder()
    for index, character in enumerate(stdout):
        if character not in "[{":
            continue
        try:
            value, consumed = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if not stdout[index + consumed :].strip():
            return value
    raise ValueError("OpenViking output did not contain a valid JSON payload")


def _source_id_from_item(item: dict[str, Any]) -> str | None:
    value = item.get("source_id")
    if value:
        return str(value)
    tags = item.get("tags")
    if isinstance(tags, dict):
        value = tags.get("source_id")
        if value:
            return str(value)
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str) and tag.startswith("source_id="):
                return tag.split("=", 1)[1]
            if isinstance(tag, dict) and tag.get("key") == "source_id" and tag.get("value"):
                return str(tag["value"])
    metadata = item.get("metadata")
    if isinstance(metadata, dict) and metadata.get("source_id"):
        return str(metadata["source_id"])
    value = item.get("uri") or item.get("path") or item.get("id")
    return str(value) if value else None


def _matches_expected(hit: str, expected: str) -> bool:
    if hit == expected:
        return True
    return expected.startswith("viking://") and hit.startswith(expected.rstrip("/") + "/")
