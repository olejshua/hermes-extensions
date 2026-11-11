"""Bounded lexical retrieval, document diversity and verified local excerpts."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from .provenance import source_day
from .redaction import redact

_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_STOP = frozenset("a an the and or is are was were what which who how where when why for from to in of on at by with without currently provides suitable not validated welche welcher welches wer wie was wo wann warum und oder ist sind war waren fuer für von zu im in der die das den dem des ein eine einer eines bei gilt zentrale welche".split())


def query_terms(query: str) -> list[str]:
    return list(dict.fromkeys(word.casefold() for word in _WORDS.findall(query)
                              if len(word) >= 3 and word.casefold() not in _STOP))[:16]


def keyword_pattern(query: str) -> str:
    words = [word for word in _WORDS.findall(query)
             if len(word) >= 3 and word.casefold() not in _STOP]
    # Explicit names/acronyms are useful lexical anchors. No query syntax is accepted.
    names = [word for word in words if not word.islower()]
    chosen = list(dict.fromkeys(word.casefold() for word in (names or sorted(words, key=len, reverse=True))))[:3]
    return "|".join(re.escape(word) for word in chosen)


def document_uri(uri: str) -> str:
    match = re.match(r"^(viking://resources/[^/]+/[^/]+)(?:/|$)", uri)
    return match.group(1) if match else uri


def excerpt(text: str, query: str, limit: int) -> str:
    """Choose a contiguous source window, retaining exact wording and bounded size."""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if 0 < end < 16384:
            text = text[end + 4:].lstrip()
    terms = set(query_terms(query))
    matches = [match for match in _WORDS.finditer(text) if match.group().casefold() in terms]
    if not matches:
        return ""
    best = None
    for match in matches[:512]:
        start = max(0, match.start() - min(80, limit // 4))
        # Avoid cutting the first word, except when no separator exists nearby.
        boundary = text.find(" ", start, min(match.start(), start + 30)) if start else -1
        if boundary >= 0:
            start = boundary + 1
        window = text[start:start + limit]
        score = len(terms & set(word.casefold() for word in _WORDS.findall(window)))
        if best is None or score > best[0]:
            best = (score, window)
    return best[1].strip() if best else ""


def lexical_rows(payload: dict, query: str, namespace: str, limit: int) -> list[dict]:
    result = payload.get("result", {})
    matches = result.get("matches", []) if isinstance(result, dict) else []
    terms = set(query_terms(query))
    prefix = f"viking://resources/{namespace}/"
    ranked = []
    for row in matches[:256]:
        if not isinstance(row, dict) or not str(row.get("uri", "")).startswith(prefix):
            continue
        text = row.get("content")
        if not isinstance(text, str) or not text.strip():
            continue
        score = len(terms & set(word.casefold() for word in _WORDS.findall(text)))
        if score:
            ranked.append((score, {"uri": row["uri"], "snippet": text,
                                   "retrieval_method": "keyword", "source_line": row.get("line")}))
    ranked.sort(key=lambda item: -item[0])
    return [row for _, row in ranked[:limit]]


def diverse_merge(semantic, lexical, limit):
    """Reserve room for both independent ranks, deduplicating document summaries."""
    result, seen = [], set()
    for index in range(max(len(semantic), len(lexical))):
        for hits in (semantic, lexical):
            if index >= len(hits):
                continue
            hit = hits[index]
            key = document_uri(hit.provenance.get("source_uri") or hit.source_ref)
            if key in seen:
                continue
            seen.add(key)
            result.append(hit)
            if len(result) >= limit:
                return result
    return result


def verified_local_evidence(hit, query: str, namespace: str, registry: Path | None, limit: int):
    """Only replace a hit with locally read text when its registered hash matches.

    A sync receipt is not proof of a completed vector index. Version attribution
    here describes the exact local source used for this excerpt, not index freshness.
    """
    if registry is None or not registry.is_file() or registry.is_symlink():
        return hit
    uri = hit.provenance.get("source_uri") or hit.source_ref
    root = document_uri(uri)
    if not root.startswith(f"viking://resources/{namespace}/"):
        return hit
    try:
        with sqlite3.connect(registry.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT path,sha256,source_id FROM resources WHERE ov_resource_id=? AND namespace=? AND status='synced'", (root, namespace)).fetchone()
        if row is None:
            return hit
        path = Path(row["path"])
        if path.is_symlink() or path.suffix.lower() not in {".md", ".txt"} or path.stat().st_size > 1_000_000:
            return hit
        with path.open("rb") as stream:
            raw = stream.read(1_000_001)
        if len(raw) > 1_000_000 or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            return hit
        text = raw.decode("utf-8")
        snippet = excerpt(redact(text), query, limit)
        if not snippet:
            return hit
        day = basis = None
        if text.startswith("---\n"):
            header = text[4:].split("\n---", 1)[0][:16384]
            for key in ("source_date", "document_date", "date"):
                match = re.search(r"^" + key + r":\s*[\"']?([^\n\"']+)", header, re.MULTILINE)
                if match and source_day(match.group(1).strip()):
                    day, basis = source_day(match.group(1).strip()), "frontmatter." + key
                    break
        provenance = {**hit.provenance, "source_uri": root, "retrieval_uri": uri,
                      "source_id": row["source_id"], "source_version": row["sha256"],
                      "source_date": day, "source_date_basis": basis,
                      "source_record_key": path.stem,
                      "evidence_origin": "verified_local_source", "index_version_verified": False}
        # Knowledge status is not inferred from a folder, sync status, or a file date.
        return type(hit)(root, snippet, provenance)
    except (OSError, UnicodeError, sqlite3.Error):
        return hit
