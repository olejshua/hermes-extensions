#!/usr/bin/env python3
"""Offline candidate matching for the Notion -> Twenty migration.

This script never calls an API and never changes CRM records. Its output is
an audit of possible matches, not permission to merge ambiguous companies.
"""

from __future__ import annotations

import collections
import csv
import difflib
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit


BASE = Path("/private/tmp/acribic-crm-migration-2026-09-29")
LEGAL = {
    "gmbh", "mbh", "ggmbh", "ag", "se", "kg", "co", "ohg", "ug", "ev",
    "e", "v", "eg", "gbr", "ek", "kgaa", "gag", "ltd", "inc", "llc",
}
STOP = LEGAL | {"und", "the", "der", "die", "das", "fur", "fuer", "of"}
FREE_MAIL = {
    "gmail.com", "googlemail.com", "gmx.de", "gmx.net", "web.de",
    "outlook.com", "hotmail.com", "hotmail.de", "yahoo.com", "yahoo.de",
    "icloud.com", "t-online.de", "aol.com", "mail.de", "freenet.de",
}


def norm(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    text = text.casefold()
    text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = text.replace("&", " und ").replace("+", " und ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def core(value: object) -> str:
    return " ".join(x for x in norm(value).split() if x not in LEGAL)


def host(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    else:
        text = urlsplit(text if "://" in text else "https://" + text).hostname or ""
    return text.removeprefix("www.")


def notion_value(prop: dict) -> object:
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in (value or []))
    if kind == "select":
        return (value or {}).get("name", "")
    if kind in ("number", "checkbox"):
        return value
    return value or ""


def load_notion() -> list[dict]:
    rows = []
    for path in sorted(BASE.glob("notion-*.jsonl")):
        label = path.stem.removeprefix("notion-")
        with path.open(encoding="utf-8") as source:
            for line in source:
                page = json.loads(line)
                values = {k: notion_value(v) for k, v in page["properties"].items()}
                values["page_id"] = page["id"]
                values["liste"] = label
                rows.append(values)
    return rows


def load_twenty() -> list[dict]:
    with (BASE / "twenty-companies.jsonl").open(encoding="utf-8") as source:
        return [json.loads(line) for line in source]


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def grams(value: str) -> set[str]:
    value = "  " + value + "  "
    return {value[i:i + 3] for i in range(len(value) - 2)}


def rough_similarity(a: set[str], b: set[str]) -> float:
    return (2 * len(a & b) / (len(a) + len(b))) if a and b else 0.0


def main() -> None:
    notion = load_notion()
    twenty = load_twenty()
    by_strict = collections.defaultdict(list)
    by_core = collections.defaultdict(list)
    by_domain = collections.defaultdict(list)
    by_token = collections.defaultdict(set)
    by_gram = collections.defaultdict(set)
    for index, company in enumerate(twenty):
        company["_strict"] = norm(company.get("name"))
        company["_core"] = core(company.get("name"))
        company["_city"] = norm((company.get("address") or {}).get("addressCity"))
        company["_postcode"] = norm((company.get("address") or {}).get("addressPostcode"))
        company["_domain"] = host((company.get("domainName") or {}).get("primaryLinkUrl"))
        company["_grams"] = grams(company["_core"])
        by_strict[company["_strict"]].append(index)
        by_core[company["_core"]].append(index)
        if company["_domain"]:
            by_domain[company["_domain"]].append(index)
        for token in set(company["_core"].split()) - STOP:
            if len(token) >= 4:
                by_token[token].add(index)
        for gram in company["_grams"]:
            by_gram[gram].add(index)

    results = []
    buckets = collections.Counter()
    for row in notion:
        strict = norm(row.get("Firma"))
        base = core(row.get("Firma"))
        city = norm(row.get("Ort"))
        postcode = norm(row.get("PLZ"))
        website = host(row.get("Website") or row.get("Domain"))
        email_domain = host(row.get("E-Mail"))
        if email_domain in FREE_MAIL:
            email_domain = ""

        exact = by_strict.get(strict, [])
        domains = set(by_domain.get(website, [])) | set(by_domain.get(email_domain, []))
        core_matches = by_core.get(base, []) if base else []
        candidates = set(exact) | domains | set(core_matches)
        if not exact:
            informative = [t for t in set(base.split()) - STOP if len(t) >= 3]
            for token in sorted(informative, key=lambda t: len(by_token.get(t, ())))[:3]:
                posting = by_token.get(token, set())
                if len(posting) <= 500:
                    candidates.update(posting)
            # Character trigrams recover abbreviated names and misspellings
            # when every complete token is too common to use as a block key.
            overlap = collections.Counter()
            for gram in grams(base):
                posting = by_gram.get(gram, set())
                if len(posting) <= 4000:
                    overlap.update(posting)
            candidates.update(i for i, _ in overlap.most_common(120))

        # Character trigrams cheaply prune broad token postings. The exact,
        # core, and domain candidates are always retained for the full score.
        protected = set(exact) | domains | set(core_matches)
        source_grams = grams(base)
        ranked = sorted(
            ((rough_similarity(source_grams, twenty[i]["_grams"]), i)
             for i in candidates - protected), reverse=True
        )
        candidates = protected | {i for _, i in ranked[:30]}

        scored = []
        for index in candidates:
            company = twenty[index]
            full_ratio = similarity(strict, company["_strict"])
            core_ratio = similarity(base, company["_core"]) if base else 0
            short_name_inside = (
                len(company["_core"]) >= 8 and
                (base.startswith(company["_core"] + " ") or
                 base.endswith(" " + company["_core"]))
            )
            if index not in exact and index not in domains and not short_name_inside and max(full_ratio, core_ratio) < 0.58:
                continue
            same_city = bool(city and company["_city"] and city == company["_city"])
            same_postcode = bool(postcode and company["_postcode"] and postcode == company["_postcode"])
            same_domain = bool(company["_domain"] and company["_domain"] in (website, email_domain))
            score = max(full_ratio, 0.55 * full_ratio + 0.45 * core_ratio)
            score += 0.06 * same_city + 0.08 * same_postcode + 0.13 * same_domain
            if short_name_inside:
                score = max(score, 0.82 + 0.06 * same_city + 0.08 * same_postcode)
            scored.append((score, index, full_ratio, core_ratio, same_city, same_postcode, same_domain))
        scored.sort(key=lambda x: (-x[0], twenty[x[1]]["id"]))
        best = []
        for score, index, full_ratio, core_ratio, same_city, same_postcode, same_domain in scored[:5]:
            company = twenty[index]
            best.append({
                "id": company["id"], "name": company["name"],
                "city": (company.get("address") or {}).get("addressCity"),
                "score": round(score, 3), "full_ratio": round(full_ratio, 3),
                "core_ratio": round(core_ratio, 3), "same_city": same_city,
                "same_postcode": same_postcode, "same_domain": same_domain,
            })

        if len(exact) == 1:
            category = "one_exact_name"
        elif len(exact) > 1:
            city_matches = [i for i in exact if city and twenty[i]["_city"] == city]
            postcode_matches = [i for i in exact if postcode and twenty[i]["_postcode"] == postcode]
            category = "multi_exact_resolved" if len(postcode_matches) == 1 or len(city_matches) == 1 else "multi_exact_review"
        elif len(core_matches) == 1 and (best and (best[0]["same_city"] or best[0]["same_domain"])):
            category = "one_core_corrob"
        elif not best or best[0]["score"] < 0.68:
            category = "likely_new"
        elif best[0]["score"] >= 0.94 and (best[0]["same_city"] or best[0]["same_postcode"] or best[0]["same_domain"]):
            category = "high_similarity_corrob"
        else:
            category = "review"
        buckets[category] += 1
        results.append({
            "page_id": row["page_id"], "liste": row["liste"],
            "firma": row.get("Firma"), "ort": row.get("Ort"), "plz": row.get("PLZ"),
            "website": row.get("Website") or row.get("Domain"), "email": row.get("E-Mail"),
            "stage": row.get("Stage"), "category": category,
            "exact_count": len(exact), "core_count": len(core_matches),
            "candidates": best,
        })

    with (BASE / "match-audit.jsonl").open("w", encoding="utf-8") as out:
        for result in results:
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
    with (BASE / "match-review.csv").open("w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=[
            "page_id", "liste", "firma", "ort", "plz", "website", "email",
            "stage", "category", "exact_count", "core_count", "top_name",
            "top_city", "top_score", "second_name", "second_score",
        ], delimiter=";")
        writer.writeheader()
        for result in results:
            top = result["candidates"]
            writer.writerow({**{k: result[k] for k in (
                "page_id", "liste", "firma", "ort", "plz", "website", "email",
                "stage", "category", "exact_count", "core_count")},
                "top_name": top[0]["name"] if top else "",
                "top_city": top[0]["city"] if top else "",
                "top_score": top[0]["score"] if top else "",
                "second_name": top[1]["name"] if len(top) > 1 else "",
                "second_score": top[1]["score"] if len(top) > 1 else ""})
    print(json.dumps(buckets, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
