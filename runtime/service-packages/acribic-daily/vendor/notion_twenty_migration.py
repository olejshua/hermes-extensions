#!/usr/bin/env python3
"""Read-only snapshots for the Acribic Notion -> Twenty CRM migration.

Credentials stay in ~/.config/acribic. Raw snapshots are written under
/private/tmp, never into the Vault. Import writes are intentionally absent
until matching and mapping have been audited.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BASE = Path(os.environ.get("ACRIBIC_MIGRATION_BASE", "/private/tmp/acribic-crm-migration-2026-09-29"))
TWENTY_URL = "https://crm.example.invalid"
NOTION_URL = "https://api.notion.com/v1"
SECRETS = Path.home() / ".config" / "acribic"
LISTS = {
    "it": "00000000-0000-4000-8000-000000000001",
    "ingenieur": "e0000000000000000000000000000004",
    "medizin": "e0000000000000000000000000000005",
    "beratung": "e0000000000000000000000000000006",
    "sicherheit": "00000000-0000-4000-8000-000000000002",
    "reinigung": "00000000-0000-4000-8000-000000000003",
    "icp": "e0000000000000000000000000000007",
}

COMPANIES_QUERY = """query($first: Int!, $after: String) {
  companies(first: $first, after: $after) {
    totalCount
    pageInfo { endCursor hasNextPage }
    edges { node {
      id name
      domainName { primaryLinkUrl primaryLinkLabel }
      address { addressCity addressPostcode addressCountry }
      kennungVergabedaten kontaktQuelle kontaktiert kontaktiertAm
      quelle art branche
      notionQuellen notionSeitenIds notionListen notionStages notionEmails
      notionVersandbereit
      outreachHistorie
    } }
  }
}"""

PEOPLE_QUERY = """query {
  people(first: 1000) {
    totalCount
    edges { node {
      id name { firstName lastName }
      emails { primaryEmail }
      company { id }
    } }
  }
}"""

COMPANY_FIELDS = {
    "notionQuellen": ("RAW_JSON", "Notion-Quellen", "Originaldaten aller zugeordneten Notion-Seiten"),
    "notionSeitenIds": ("TEXT", "Notion-Seiten-IDs", "IDs aller zugeordneten Notion-Seiten"),
    "notionListen": ("TEXT", "Notion-Listen", "Ursprüngliche Notion-Listen"),
    "notionStages": ("TEXT", "Notion-Stages", "Alle ursprünglichen Notion-Stages"),
    "notionEmails": ("TEXT", "Notion-E-Mails", "Ursprüngliche E-Mail-Adressen aus Notion"),
    "notionVersandbereit": ("BOOLEAN", "Notion: versandbereit", "Bleibt bis zur abschließenden Versandprüfung false"),
}


def token(name: str) -> str:
    return (SECRETS / name).read_text().strip()


TWENTY_REQUEST_INTERVAL = 0.0
_last_twenty_request = 0.0


def pace_twenty(url: str) -> None:
    """Optional per-process pacing shared by reads and non-retried writes."""
    global _last_twenty_request
    if TWENTY_REQUEST_INTERVAL <= 0 or not url.startswith(TWENTY_URL):
        return
    delay = TWENTY_REQUEST_INTERVAL - (time.monotonic() - _last_twenty_request)
    if delay > 0:
        time.sleep(delay)
    _last_twenty_request = time.monotonic()


def request_json(method: str, url: str, key: str, *, notion: bool = False,
                 body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if notion:
        headers["Notion-Version"] = "2022-06-28"
    for attempt in range(8):
        pace_twenty(url)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise RuntimeError(f"Expected JSON object from {url}")
            return result
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 7:
                detail = exc.read(600).decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {exc.code} at {url}: {detail}") from exc
            retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 30)
            time.sleep(delay)
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt == 7:
                raise RuntimeError(f"Network read failed at {url}: {exc}") from exc
            time.sleep(min(2 ** attempt, 30))
    raise AssertionError("unreachable")


def write_json(path: Path, obj: object) -> None:
    temp = path.with_suffix(path.suffix + ".partial")
    with temp.open("w", encoding="utf-8") as out:
        json.dump(obj, out, ensure_ascii=False, indent=2)
        out.write("\n")
    temp.replace(path)


def append_journal(entry: dict) -> None:
    entry = {"at": datetime.now(timezone.utc).isoformat(), **entry}
    with (BASE / "write-journal.jsonl").open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry, ensure_ascii=False) + "\n")
        out.flush()
        os.fsync(out.fileno())


def write_once(method: str, url: str, key: str, body: dict) -> dict:
    """No automatic retry for a write whose response may be uncertain."""
    pace_twenty(url)
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(600).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} on write to {url}: {detail}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Unclear write response from {url}")
    return result


def snapshot_notion() -> None:
    key = token("notion-token")
    counts = {}
    schemas = {}
    for label, database_id in LISTS.items():
        schema_url = f"{NOTION_URL}/databases/{database_id}"
        schemas[label] = request_json("GET", schema_url, key, notion=True)
        path = BASE / f"notion-{label}.jsonl"
        temp = path.with_suffix(".jsonl.partial")
        seen_ids = set()
        cursor = None
        count = 0
        with temp.open("w", encoding="utf-8") as out:
            while True:
                payload = {"page_size": 100}
                if cursor:
                    payload["start_cursor"] = cursor
                result = request_json("POST", f"{schema_url}/query", key,
                                      notion=True, body=payload)
                pages = result.get("results")
                if not isinstance(pages, list):
                    raise RuntimeError(f"No results in Notion list {label}")
                for page in pages:
                    page_id = page["id"]
                    if page_id in seen_ids:
                        raise RuntimeError(f"Duplicate Notion page ID in {label}: {page_id}")
                    seen_ids.add(page_id)
                    out.write(json.dumps(page, ensure_ascii=False) + "\n")
                count += len(pages)
                if not result.get("has_more"):
                    break
                next_cursor = result.get("next_cursor")
                if not next_cursor or next_cursor == cursor:
                    raise RuntimeError(f"Broken Notion cursor in {label}")
                cursor = next_cursor
                time.sleep(0.38)
        temp.replace(path)
        counts[label] = count
        print(f"Notion {label}: {count}", flush=True)
    write_json(BASE / "notion-schemas.json", schemas)
    write_json(BASE / "notion-counts.json", counts)
    print(f"Notion total: {sum(counts.values())}", flush=True)


def graphql(key: str, query: str, variables: dict | None = None) -> dict:
    body = {"query": query}
    if variables is not None:
        body["variables"] = variables
    result = request_json("POST", f"{TWENTY_URL}/graphql", key, body=body)
    if result.get("errors"):
        raise RuntimeError(f"Twenty GraphQL error: {result['errors']}")
    if not isinstance(result.get("data"), dict):
        raise RuntimeError("Twenty GraphQL response missing data")
    return result["data"]


def snapshot_twenty() -> None:
    key = token("twenty-api-key")
    people = graphql(key, PEOPLE_QUERY)["people"]
    if len(people["edges"]) != people["totalCount"]:
        raise RuntimeError("People page does not contain all Twenty contacts")
    write_json(BASE / "twenty-people.json", people)
    print(f"Twenty people: {people['totalCount']}", flush=True)

    path = BASE / "twenty-companies.jsonl"
    temp = path.with_suffix(".jsonl.partial")
    cursor = None
    seen_cursors = set()
    seen_ids = set()
    expected = None
    with temp.open("w", encoding="utf-8") as out:
        while True:
            data = graphql(key, COMPANIES_QUERY, {"first": 1000, "after": cursor})["companies"]
            if expected is None:
                expected = data["totalCount"]
            if data["totalCount"] != expected:
                raise RuntimeError("Twenty company count changed during snapshot")
            edges = data.get("edges")
            if not isinstance(edges, list) or not edges:
                raise RuntimeError("Empty company page before end of snapshot")
            for edge in edges:
                company = edge["node"]
                company_id = company["id"]
                if company_id in seen_ids:
                    raise RuntimeError(f"Duplicate Twenty company ID in pagination: {company_id}")
                seen_ids.add(company_id)
                out.write(json.dumps(company, ensure_ascii=False) + "\n")
            page_info = data["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info.get("endCursor")
            if not cursor or cursor in seen_cursors:
                raise RuntimeError("Broken Twenty company cursor")
            seen_cursors.add(cursor)
            if len(seen_ids) % 5000 == 0:
                print(f"Twenty companies read: {len(seen_ids)}", flush=True)
            time.sleep(0.65)
    if len(seen_ids) != expected:
        raise RuntimeError(f"Twenty company count mismatch: {len(seen_ids)} != {expected}")
    temp.replace(path)
    write_json(BASE / "twenty-counts.json", {"companies": len(seen_ids), "people": people["totalCount"]})
    print(f"Twenty companies: {len(seen_ids)}", flush=True)


def company_metadata(key: str) -> dict:
    result = request_json("GET", f"{TWENTY_URL}/rest/metadata/objects", key)
    matches = [obj for obj in result.get("data", []) if obj.get("nameSingular") == "company"]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one Company metadata object, got {len(matches)}")
    return matches[0]


def ensure_company_fields() -> None:
    key = token("twenty-api-key")
    for name, (kind, label, description) in COMPANY_FIELDS.items():
        company = company_metadata(key)
        matches = [field for field in company.get("fields", []) if field.get("name") == name]
        if len(matches) > 1:
            raise RuntimeError(f"Duplicate Company field name {name}")
        if matches:
            if matches[0].get("type") != kind:
                raise RuntimeError(f"Field {name} exists with wrong type {matches[0].get('type')}")
            print(f"Field verified: {name}", flush=True)
            continue
        body = {"type": kind, "objectMetadataId": company["id"], "name": name,
                "label": label, "description": description, "isNullable": True}
        append_journal({"action": "create-field", "name": name, "state": "pending"})
        try:
            result = write_once("POST", f"{TWENTY_URL}/rest/metadata/fields", key, body)
        except Exception:
            # A timeout can happen after the server committed the field. Read
            # state before any human decides whether to rerun this command.
            current = company_metadata(key)
            found = [x for x in current.get("fields", []) if x.get("name") == name]
            append_journal({"action": "create-field", "name": name,
                            "state": "uncertain", "found_after_error": len(found)})
            raise
        created = result.get("data", {}).get("createOneField")
        current = company_metadata(key)
        found = [x for x in current.get("fields", []) if x.get("name") == name]
        if len(found) != 1 or found[0].get("type") != kind:
            append_journal({"action": "create-field", "name": name, "state": "unverified"})
            raise RuntimeError(f"Created field {name} could not be verified")
        append_journal({"action": "create-field", "name": name, "state": "verified",
                        "id": found[0]["id"]})
        print(f"Field created and verified: {name}", flush=True)


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"snapshot-notion", "snapshot-twenty", "ensure-fields"}:
        raise SystemExit("Usage: notion_twenty_migration.py snapshot-notion|snapshot-twenty|ensure-fields")
    os.umask(0o077)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.argv[1] == "snapshot-notion":
        snapshot_notion()
    elif sys.argv[1] == "snapshot-twenty":
        snapshot_twenty()
    else:
        ensure_company_fields()


if __name__ == "__main__":
    main()
