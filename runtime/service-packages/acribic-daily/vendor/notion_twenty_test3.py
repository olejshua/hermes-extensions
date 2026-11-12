#!/usr/bin/env python3
"""Three-record migration probe: two Notion pages on one existing company,
and one new company. Every created record has a deterministic UUID.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from notion_twenty_match import host, notion_value
from notion_twenty_migration import (
    BASE, TWENTY_URL, append_journal, graphql, request_json, token,
    write_json, write_once,
)


NAMESPACE = uuid.UUID("00000000-0000-4000-8000-000000000008")
BREDEX_ID = "00000000-0000-4000-8000-000000000009"
BREDEX_PAGES = {
    "00000000-0000-4000-8000-00000000000a",
    "00000000-0000-4000-8000-00000000000b",
}
CEQTOR_PAGE = "00000000-0000-4000-8000-00000000000c"
TEST_IDS = BREDEX_PAGES | {CEQTOR_PAGE}
NEW_COMPANY_ID = str(uuid.uuid5(NAMESPACE, "company:" + CEQTOR_PAGE))


def stable_id(kind: str, value: str) -> str:
    return str(uuid.uuid5(NAMESPACE, kind + ":" + value))


def sources() -> dict[str, dict]:
    found = {}
    for path in sorted(BASE.glob("notion-*.jsonl")):
        label = path.stem.removeprefix("notion-")
        with path.open(encoding="utf-8") as inp:
            for line in inp:
                page = json.loads(line)
                if page["id"] not in TEST_IDS:
                    continue
                found[page["id"]] = {
                    "page_id": page["id"], "liste": label, "url": page["url"],
                    "last_edited_time": page["last_edited_time"],
                    "properties": {k: notion_value(v) for k, v in page["properties"].items()},
                }
    if set(found) != TEST_IDS:
        raise RuntimeError(f"Missing test sources: {TEST_IDS - set(found)}")
    return found


def company_by_id(key: str, company_id: str) -> dict:
    data = request_json("GET", f"{TWENTY_URL}/rest/companies/{company_id}", key)
    return data["data"]["company"]


def merge_sources(existing: object, additions: list[dict]) -> list[dict]:
    if existing is None:
        existing = []
    if not isinstance(existing, list):
        raise RuntimeError("Existing notionQuellen is not an array")
    merged = {source["page_id"]: source for source in existing}
    for source in additions:
        old = merged.get(source["page_id"])
        if old and any(source.get(key) != value for key, value in old.items()):
            raise RuntimeError(f"Existing Notion source has changed: {source['page_id']}")
        merged[source["page_id"]] = source
    return sorted(merged.values(), key=lambda source: source["page_id"])


def company_data(company_id: str, name: str, sources_: list[dict],
                 existing: dict | None = None) -> dict:
    merged = merge_sources((existing or {}).get("notionQuellen"), sources_)
    emails = sorted({str(s["properties"].get("E-Mail") or "").strip()
                     for s in merged if s["properties"].get("E-Mail")})
    stages = sorted({str(s["properties"].get("Stage") or "") for s in merged
                     if s["properties"].get("Stage")})
    data = {
        "id": company_id, "name": name, "notionQuellen": merged,
        "notionSeitenIds": "; ".join(s["page_id"] for s in merged),
        "notionListen": "; ".join(sorted({s["liste"] for s in merged})),
        "notionStages": "; ".join(stages),
        "notionEmails": "; ".join(emails),
        "notionVersandbereit": False,
    }
    contacted = any(
        str(s["properties"].get("Stage") or "") not in {
            "Nicht kontaktiert", "Adresse pruefen", "Gesperrt", "Gesperrt (ICP)", ""
        } or bool(s["properties"].get("Gesprochen"))
        for s in merged
    )
    if contacted:
        data["kontaktiert"] = True
    sent_dates = []
    for source in merged:
        if source["properties"].get("Stage") != "E-Mail raus":
            continue
        note = str(source["properties"].get("Notizen") or "")
        match = re.search(r"(?:raus|Mail raus|E-Mail raus) am (\d{2})\.(\d{2})\.(\d{4})", note)
        if match:
            day, month, year = match.groups()
            sent_dates.append(f"{year}-{month}-{day}")
    if sent_dates and not (existing or {}).get("kontaktiertAm"):
        data["kontaktiertAm"] = min(sent_dates)
    if existing is None:
        source = sources_[0]["properties"]
        website = str(source.get("Website") or source.get("Domain") or "").strip()
        if website:
            link_url = website if "://" in website else "https://" + website
            data["domainName"] = {
                "primaryLinkUrl": link_url,
                "primaryLinkLabel": host(website),
            }
        city = str(source.get("Ort") or "").strip()
        if city:
            data["address"] = {"addressCity": city}
    return data


def note_markdown(source: dict) -> str:
    props = source["properties"]
    lines = [
        f"# {props.get('Firma') or 'Notion-Quelle'}", "",
        f"Notion-ID: {source['page_id']}",
        f"Ursprüngliche Liste: {source['liste']}",
        f"Notion-Seite: {source['url']}",
        f"Letzte Notion-Änderung: {source['last_edited_time']}", "",
        "## Originalfelder", "",
    ]
    for name, value in sorted(props.items()):
        lines.append(f"**{name}:** {value if value is not None else ''}")
    if source.get("links"):
        lines.extend(["", "## Verlinkungen aus Notion", ""])
        for field, entries in sorted(source["links"].items()):
            for entry in entries:
                lines.append(f"**{field}:** {entry['text']} — {entry['url']}")
    return "\n\n".join(lines) + "\n"


def graphql_mutation_once(key: str, label: str, query: str, variables: dict) -> dict:
    append_journal({"action": label, "state": "pending", "ids": variables.get("ids", [])})
    result = write_once("POST", f"{TWENTY_URL}/graphql", key,
                        {"query": query,
                         "variables": {k: v for k, v in variables.items() if k != "ids"}})
    if result.get("errors"):
        append_journal({"action": label, "state": "uncertain", "errors": result["errors"]})
        raise RuntimeError(f"GraphQL write {label} returned errors: {result['errors']}")
    append_journal({"action": label, "state": "returned"})
    return result["data"]


def main() -> None:
    os.umask(0o077)
    key = token("twenty-api-key")
    pages = sources()
    existing = company_by_id(key, BREDEX_ID)
    write_json(BASE / "test3-bredex-before.json", existing)
    if existing["name"] != "BREDEX GmbH":
        raise RuntimeError("BREDEX target changed")
    count_before = graphql(key, "query { companies(first: 1) { totalCount } people(first: 1) { totalCount } }")
    if count_before["companies"]["totalCount"] != 45881 or count_before["people"]["totalCount"] != 20:
        raise RuntimeError(f"Twenty counts changed since snapshot: {count_before}")
    duplicate_check = request_json(
        "POST", f"{TWENTY_URL}/rest/companies/duplicates", key,
        body={"data": [{"name": pages[CEQTOR_PAGE]["properties"]["Firma"]}]},
    )
    if duplicate_check["data"][0]["totalCount"] != 0:
        raise RuntimeError("Twenty detects a CEQTOR duplicate")

    bredex_sources = [pages[page_id] for page_id in sorted(BREDEX_PAGES)]
    bredex_data = company_data(BREDEX_ID, existing["name"], bredex_sources, existing)
    ceqtor_source = pages[CEQTOR_PAGE]
    ceqtor_data = company_data(NEW_COMPANY_ID, ceqtor_source["properties"]["Firma"],
                               [ceqtor_source])
    write_json(BASE / "test3-company-payloads.json", [bredex_data, ceqtor_data])

    company_mutation = """mutation($data: [CompanyCreateInput!]!) {
      createCompanies(data: $data, upsert: true) { id }
    }"""
    graphql_mutation_once(key, "test3-update-bredex", company_mutation,
                          {"data": [bredex_data], "ids": [BREDEX_ID]})
    updated = company_by_id(key, BREDEX_ID)
    if updated.get("notionQuellen") != bredex_data["notionQuellen"]:
        raise RuntimeError("BREDEX Notion sources did not verify")
    allowed_changes = set(bredex_data) | {"updatedAt", "updatedBy", "searchVector"}
    unexpected = {name: (value, updated.get(name)) for name, value in existing.items()
                  if name not in allowed_changes and updated.get(name) != value}
    if unexpected:
        raise RuntimeError(f"BREDEX unexpected changes: {unexpected}")
    append_journal({"action": "test3-update-bredex", "state": "verified", "id": BREDEX_ID})
    print("BREDEX: 2 Notion pages attached to existing company", flush=True)

    graphql_mutation_once(key, "test3-create-ceqtor", company_mutation,
                          {"data": [ceqtor_data], "ids": [NEW_COMPANY_ID]})
    created = company_by_id(key, NEW_COMPANY_ID)
    if created.get("notionQuellen") != ceqtor_data["notionQuellen"]:
        raise RuntimeError("CEQTOR Notion source did not verify")
    append_journal({"action": "test3-create-ceqtor", "state": "verified", "id": NEW_COMPANY_ID})
    print("CEQTOR: 1 company created", flush=True)

    note_data = []
    for page_id in sorted(TEST_IDS):
        source = pages[page_id]
        note_data.append({"id": stable_id("note", page_id),
                          "title": f"Notion {source['liste']} · {page_id}",
                          "bodyV2": {"markdown": note_markdown(source)}})
    write_json(BASE / "test3-note-payloads.json", note_data)
    note_mutation = """mutation($data: [NoteCreateInput!]!) {
      createNotes(data: $data, upsert: true) { id }
    }"""
    graphql_mutation_once(key, "test3-create-notes", note_mutation,
                          {"data": note_data, "ids": [x["id"] for x in note_data]})
    for note in note_data:
        actual = request_json("GET", f"{TWENTY_URL}/rest/notes/{note['id']}", key)["data"]["note"]
        if actual.get("bodyV2", {}).get("markdown") != note["bodyV2"]["markdown"]:
            raise RuntimeError(f"Note did not verify: {note['id']}")
    append_journal({"action": "test3-create-notes", "state": "verified"})
    print("3 source notes created and read back", flush=True)

    targets = [
        {"id": stable_id("target", page_id),
         "noteId": stable_id("note", page_id),
         "targetCompanyId": BREDEX_ID if page_id in BREDEX_PAGES else NEW_COMPANY_ID}
        for page_id in sorted(TEST_IDS)
    ]
    target_mutation = """mutation($data: [NoteTargetCreateInput!]!) {
      createNoteTargets(data: $data, upsert: true) { id }
    }"""
    graphql_mutation_once(key, "test3-link-notes", target_mutation,
                          {"data": targets, "ids": [x["id"] for x in targets]})
    for target in targets:
        actual = request_json("GET", f"{TWENTY_URL}/rest/noteTargets/{target['id']}", key)["data"]["noteTarget"]
        if actual.get("noteId") != target["noteId"] or actual.get("targetCompanyId") != target["targetCompanyId"]:
            raise RuntimeError(f"Note target did not verify: {target['id']}")
    append_journal({"action": "test3-link-notes", "state": "verified"})
    print("3 notes linked to their company and read back", flush=True)

    count_after = graphql(key, "query { companies(first: 1) { totalCount } people(first: 1) { totalCount } }")
    if count_after["companies"]["totalCount"] != 45882 or count_after["people"]["totalCount"] != 20:
        raise RuntimeError(f"Twenty counts after test unexpected: {count_after}")
    write_json(BASE / "test3-verified.json", {
        "companyCountBefore": 45881, "companyCountAfter": 45882,
        "peopleCount": 20, "sourcePageIds": sorted(TEST_IDS),
        "existingCompanyId": BREDEX_ID, "newCompanyId": NEW_COMPANY_ID,
        "noteIds": [note["id"] for note in note_data],
        "targetIds": [target["id"] for target in targets],
    })
    print("3-page migration test verified", flush=True)


if __name__ == "__main__":
    main()
