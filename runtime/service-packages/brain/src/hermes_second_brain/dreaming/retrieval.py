"""Bounded, read-only OpenViking HTTP retrieval between Light and REM."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..ov_adapter import safe_namespace
from .config import DreamingConfig
from .redaction import is_placeholder_only, redact
from .provenance import evidence_role, source_provenance
from .retrieval_support import diverse_merge, keyword_pattern, lexical_rows, verified_local_evidence

LOG = logging.getLogger(__name__)
_WS = re.compile(r"\s+")
MAX_QUERY_CHARS = 160


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _open_request(request: Request, timeout: float):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


@dataclass(frozen=True)
class RetrievedItem:
    ref: str
    namespace: str
    query_hash: str
    snippet: str
    role: str = "assistant"
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchHit:
    source_ref: str
    snippet: str
    provenance: dict[str, Any]


@dataclass
class RetrievalOutcome:
    items: list[RetrievedItem] = field(default_factory=list)
    queried: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.failed > 0


def sanitize_queries(queries: Sequence[str], *, limit: int) -> list[str]:
    """Redact, normalise, validate and deduplicate model-proposed queries."""

    result: list[str] = []
    seen: set[str] = set()
    for raw in queries:
        value = redact(str(raw))
        value = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in value)
        value = _WS.sub(" ", value).strip()[:MAX_QUERY_CHARS].strip()
        key = value.casefold()
        if len(value) < 3 or not any(ch.isalnum() for ch in value) or is_placeholder_only(value):
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8", "replace")).hexdigest()


def retrieve_context(
    queries: Sequence[str],
    config: DreamingConfig,
    *,
    record=None,
    deadline_monotonic: float | None = None,
) -> RetrievalOutcome:
    """POST bounded JSON search requests; failures degrade without raw text."""

    outcome = RetrievalOutcome()
    retrieval = config.retrieval
    if not retrieval.enabled or not queries:
        return outcome
    bounded = sanitize_queries(queries, limit=config.budgets.max_retrieval_queries)
    seen_refs: set[str] = set()
    for query in bounded:
        digest = query_hash(query)
        for namespace in retrieval.namespaces:
            remaining = _remaining(deadline_monotonic)
            if remaining is not None and remaining <= 0:
                outcome.failed += 1
                outcome.errors.append(f"{namespace}: deadline")
                return outcome
            timeout = retrieval.timeout_seconds if remaining is None else min(retrieval.timeout_seconds, remaining)
            if timeout <= 0:
                return outcome
            outcome.queried += 1
            try:
                hits = _find(query, namespace, config, timeout=timeout,
                             deadline_monotonic=deadline_monotonic)
            except Exception as exc:  # degrade; never leak query or response
                outcome.failed += 1
                message = _safe_error(exc)
                outcome.errors.append(f"{namespace}: {message}")
                LOG.info("dreaming retrieval failed", extra={"fields": {"namespace": namespace, "error": message}})
                if record is not None:
                    record(query_hash=digest, query_length=len(query), namespace=namespace, status="failed", hits=0, error=message)
                continue
            kept = 0
            for hit in hits:
                source_ref, snippet = hit.source_ref, hit.snippet
                clean = redact(snippet)[: config.budgets.max_retrieval_snippet_characters]
                if not clean.strip():
                    continue
                identity = f"{namespace}\0{source_ref}"
                if hit.provenance.get("source_version"):
                    identity += "\0" + hit.provenance["source_version"]
                if hit.provenance.get("evidence_origin") == "verified_local_source":
                    identity += "\0" + hashlib.sha256(clean.encode("utf-8")).hexdigest()
                ref = "ov:" + hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:24]
                if ref in seen_refs:
                    continue
                seen_refs.add(ref)
                outcome.items.append(
                    RetrievedItem(
                        ref=ref,
                        namespace=namespace,
                        query_hash=digest,
                        snippet=clean,
                        role=evidence_role(retrieval.is_canonical(namespace), hit.provenance),
                        provenance=hit.provenance,
                    )
                )
                kept += 1
                if kept >= config.budgets.max_retrieval_hits_per_query:
                    break
            if record is not None:
                record(query_hash=digest, query_length=len(query), namespace=namespace, status="ok", hits=kept, error=None)
    return outcome


def _find(
    query: str, namespace: str, config: DreamingConfig, *, timeout: float,
    deadline_monotonic: float | None,
) -> list[SearchHit]:
    retrieval = config.retrieval
    deadline = time.monotonic() + timeout
    if deadline_monotonic is not None:
        deadline = min(deadline, deadline_monotonic)
    hybrid = getattr(retrieval, "keyword_enabled", False)
    candidates = min(30, retrieval.limit * 2) if hybrid else retrieval.limit
    body = {"query": query, "target_uri": f"viking://resources/{safe_namespace(namespace)}",
            "limit": candidates, "level": list(getattr(retrieval, "levels", (0, 1))),
            "include_provenance": True}
    data = _post_json(retrieval.endpoint_url, body, retrieval.max_response_bytes,
                      timeout=timeout, deadline_monotonic=deadline)
    expected_prefix = "viking://resources/" + safe_namespace(namespace) + "/"
    semantic = [hit for hit in _parse_hits(data) if hit.source_ref.startswith(expected_prefix)]
    lexical = []
    pattern = keyword_pattern(query) if hybrid else ""
    remaining = _remaining(deadline) or 0
    if pattern and remaining > .05 and retrieval.endpoint_url.endswith("/search/find"):
        try:
            payload = _post_json(retrieval.endpoint_url.removesuffix("find") + "grep",
                {"uri": body["target_uri"], "pattern": pattern, "case_insensitive": True,
                 "node_limit": 256, "level_limit": 8}, retrieval.max_response_bytes,
                timeout=min(2.0, remaining), deadline_monotonic=min(deadline, time.monotonic() + 2.0))
            lexical = _parse_hits(lexical_rows(payload, query, namespace, candidates))
        except Exception:
            LOG.info("dreaming keyword supplement unavailable")
    hits = diverse_merge(semantic, lexical, retrieval.limit) if hybrid else semantic
    result = []
    for hit in hits:
        if not hit.source_ref.startswith(expected_prefix):
            continue
        if (_remaining(deadline) or 0) <= 0:
            continue
        if getattr(retrieval, "source_registry", None) is None:
            # Upstream generic mode: native excerpts remain contextual evidence.
            # Alex's configured production path below always requires registry
            # bytes plus a current matching source approval receipt.
            result.append(hit)
            continue
        verified = verified_local_evidence(hit, query, namespace,
            retrieval.source_registry, config.budgets.max_retrieval_snippet_characters)
        if verified.provenance.get("evidence_origin") != "verified_local_source":
            continue
        receipt_path = getattr(retrieval, "approval_receipts", {}).get(namespace)
        if receipt_path is not None:
            from ..ole_policy import verified_record
            try:
                record = verified_record(receipt_path, verified.provenance.get("source_record_key"),
                                         accepted_statuses=("confirmed", "hypothesis", "superseded", "unknown"))
            except (OSError, ValueError, KeyError, TypeError):
                record = None
            if not record or record.get("export_sha256", record["sha256"]) != verified.provenance.get("source_version"):
                continue
            verified.provenance.update(evidence_status=record["status"], approval_verified=True)
        else:
            # Registry sync and HTTP metadata alone never grant user authority.
            verified.provenance.update(evidence_status="unknown", approval_verified=False)
        result.append(verified)
    return result


def _post_json(endpoint: str, body: dict[str, Any], max_response_bytes: int, *,
               timeout: float, deadline_monotonic: float | None) -> Any:
    request = Request(endpoint, data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                      method="POST", headers={"Content-Type": "application/json", "Accept": "application/json"})
    remaining = _remaining(deadline_monotonic)
    if remaining is not None and remaining <= 0:
        raise TimeoutError("retrieval deadline")
    timeout = timeout if remaining is None else min(timeout, remaining)
    try:
        with _open_request(request, timeout=max(.001, timeout)) as response:
            chunks: list[bytes] = []
            total = 0
            while True:
                remaining = _remaining(deadline_monotonic)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("retrieval deadline")
                _set_response_timeout(response, timeout if remaining is None else min(timeout, remaining))
                chunk = response.read(min(65_536, max_response_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_response_bytes:
                    raise RuntimeError("local retrieval response exceeded configured bound")
            raw = b"".join(chunks)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("local retrieval request failed") from exc
    if len(raw) > max_response_bytes:
        raise RuntimeError("local retrieval response exceeded configured bound")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("local retrieval returned invalid JSON") from exc
    if isinstance(data, dict) and "status" in data and data.get("status") != "ok":
        raise RuntimeError("local retrieval returned a non-success status")
    return data


def _set_response_timeout(response: Any, seconds: float) -> None:
    """Best-effort tightening of urllib's socket timeout between body chunks."""

    try:
        response.fp.raw._sock.settimeout(max(0.001, seconds))
    except (AttributeError, OSError):
        pass


def _hits_with_snippets(data: Any) -> list[tuple[str, str]]:
    """Compatibility view for existing probes; Dreaming retains full metadata."""
    return [(hit.source_ref, hit.snippet) for hit in _parse_hits(data)]


def _parse_hits(data: Any) -> list[SearchHit]:
    items: list[dict[str, Any]] = []
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, dict):
            for key in ("memories", "resources", "skills"):
                value = result.get(key)
                if isinstance(value, list):
                    items.extend(item for item in value if isinstance(item, dict))
        for key in ("results", "hits", "items"):
            value = data.get(key)
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
    elif isinstance(data, list):
        items = [item for item in data if isinstance(item, dict)]
    result_rows: list[SearchHit] = []
    for index, item in enumerate(items):
        ref = item.get("source_id") or item.get("uri") or item.get("path") or item.get("id") or f"item-{index}"
        snippet = ""
        # OpenViking FindResult serializes its searchable text as `abstract`.
        # Keep explicit excerpts first for compatible alternative responses.
        for key in ("snippet", "summary", "text", "content", "excerpt", "abstract"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                snippet = value.strip()
                break
        result_rows.append(SearchHit(str(ref), snippet, source_provenance(item)))
    return result_rows


def _safe_error(exc: BaseException) -> str:
    # Exception strings from urllib may contain the endpoint but never the JSON
    # body.  Still map them to a fixed category to keep diagnostics content-free.
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "unavailable"


def _remaining(deadline_monotonic: float | None) -> float | None:
    return None if deadline_monotonic is None else deadline_monotonic - time.monotonic()
