"""Bounded, in-memory Qwen embedding comparison; never touches OpenViking.

Uses already installed models only. Reports synthetic retrieval mechanics,
latency, token use and model residency, not personal answer quality.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from .quality_eval import percentile, recall, validate_spec, write_private

BASE = "http://127.0.0.1:11434"
INSTRUCTION = "Given a personal memory question, retrieve relevant passages that answer the question"
MODELS = ("qwen3-embedding:8b", "qwen3-embedding:4b", "qwen3-embedding:0.6b")


def request(path: str, body=None):
    req = Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=45) as response:
        raw = response.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError("embedding response exceeds bound")
    return json.loads(raw)


def unit(vector, dimensions):
    if len(vector) != dimensions or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector):
        raise ValueError("invalid embedding dimensions or values")
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        raise ValueError("zero embedding")
    return [x / norm for x in vector]


def embed(model, texts, dimensions):
    started = time.monotonic()
    result = request("/api/embed", {"model": model, "input": texts, "dimensions": dimensions,
                                     "truncate": False})
    vectors = result.get("embeddings", [])
    if len(vectors) != len(texts):
        raise ValueError("embedding response count mismatch")
    return [unit(vector, dimensions) for vector in vectors], {
        "wall_seconds": round(time.monotonic() - started, 4),
        "tokens": result.get("prompt_eval_count"), "load_ns": result.get("load_duration"),
        "server_total_ns": result.get("total_duration")}


def run_probe(spec, models, case_limit, document_limit, dimensions, deadline_seconds=240):
    validate_spec(spec)
    if spec["scope"] != "synthetic_regression":
        raise ValueError("this bounded probe accepts only the explicitly synthetic corpus")
    documents = spec["documents"][:document_limit]
    cases = spec["cases"][:case_limit]
    doc_ids = [doc["id"] for doc in documents]
    if len(set(doc_ids)) != len(doc_ids):
        raise ValueError("duplicate document IDs")
    for case in cases:
        if set(case.get("expected_all", []) + case.get("expected_any", [])) - set(doc_ids):
            raise ValueError("document limit excludes expected evidence; expand it")
    installed = {model["name"]: model for model in request("/api/tags")["models"]}
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "query_instruction": INSTRUCTION, "scope": "synthetic embedding probe; no answer-quality claim; no production index writes",
              "corpus_sha256": hashlib.sha256(json.dumps(documents, sort_keys=True).encode()).hexdigest(),
              "case_ids": [case["id"] for case in cases], "document_count": len(documents),
              "dimensions": dimensions, "k": 5, "variants": [], "skipped_models": []}
    deadline = time.monotonic() + deadline_seconds
    def guard():
        if time.monotonic() >= deadline:
            raise TimeoutError("bounded probe deadline reached")
    for model in models:
        if model not in installed:
            report["skipped_models"].append({"model": model, "reason": "not installed; no automatic download"})
            continue
        try:
            vectors, document_metrics = [], []
            for start in range(0, len(documents), 4):
                guard()
                batch, metric = embed(model, [doc["text"] for doc in documents[start:start + 4]], dimensions)
                vectors.extend(batch)
                document_metrics.append(metric)
            for style in ("plain", "instruction"):
                rows = []
                for case in cases:
                    guard()
                    query = case["query"] if style == "plain" else f"Instruct: {INSTRUCTION}\nQuery: {case['query']}"
                    query_vectors, metric = embed(model, [query], dimensions)
                    ranked = sorted(zip(doc_ids, vectors), key=lambda pair: sum(a * b for a, b in zip(query_vectors[0], pair[1])), reverse=True)
                    hits = [doc_id for doc_id, _ in ranked[:5]]
                    rows.append({"id": case["id"], "category": case["category"], "split": case["split"],
                                 "recall_at_5": recall(case, hits), "source_ids": hits, **metric})
                values = [row["recall_at_5"] for row in rows if row["recall_at_5"] is not None]
                loaded = next((m for m in request("/api/ps")["models"] if m["name"] == model), {})
                report["variants"].append({"model": model, "model_digest": installed[model].get("digest"),
                    "query_style": style, "document_embedding_metrics": document_metrics,
                    "model_size_vram_bytes": loaded.get("size_vram"),
                    "avg_recall_at_5": sum(values) / len(values) if values else None,
                    "query_p50_seconds": percentile([row["wall_seconds"] for row in rows], .5),
                    "query_p95_seconds": percentile([row["wall_seconds"] for row in rows], .95),
                    "query_sample_count": len(rows), "cases": rows})
        except (OSError, ValueError, KeyError, TimeoutError) as exc:
            report["skipped_models"].append({"model": model, "reason": type(exc).__name__})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--model", choices=MODELS, action="append")
    parser.add_argument("--cases", type=int, choices=range(1, 61), default=6)
    parser.add_argument("--documents", type=int, choices=range(1, 101), default=10)
    parser.add_argument("--dimensions", type=int, choices=(256, 512, 1024), default=1024)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_probe(json.loads(args.spec.read_text()), args.model or list(MODELS),
                       args.cases, args.documents, args.dimensions)
    write_private(args.output, report)
    print(json.dumps({"output": str(args.output), "completed_variants": len(report["variants"]),
                      "skipped_models": report["skipped_models"]}))
    return 0 if report["variants"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
