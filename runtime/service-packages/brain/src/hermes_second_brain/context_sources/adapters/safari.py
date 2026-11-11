from __future__ import annotations

import datetime as dt
import hashlib
import plistlib
import sqlite3
from pathlib import Path
from typing import Any

from ..models import Derivative, RawItem, ScanBatch, ScanRequest, Sensitivity, SourceCursor, SourceHealth, SourceStatus
from ..redaction import REDACTION_VERSION, redact_text, stable_hash, strip_url_secrets
from ..sqlite_snapshot import sqlite_snapshot, validate_regular_source

CORE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)


class SafariAdapter:
    source_name = "safari"
    sensitivity = Sensitivity.SENSITIVE
    retention = (30, 90)

    def __init__(self, history_path: Path | str | None = None, bookmarks_path: Path | str | None = None):
        home = Path.home()
        self.history_path = Path(history_path or home / "Library/Safari/History.db").expanduser()
        self.bookmarks_path = Path(bookmarks_path or home / "Library/Safari/Bookmarks.plist").expanduser()

    def health(self, request: ScanRequest) -> SourceHealth:
        available = False
        for path in (self.history_path, self.bookmarks_path):
            try:
                validate_regular_source(path, max_bytes=1024 * 1024 * 1024)
                available = True
            except ValueError:
                continue
        if not available:
            return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, "safari_permission_or_data_unavailable")
        return SourceHealth(self.source_name, SourceStatus.HEALTHY, "ok", "safari-history-bookmarks-v1")

    def scan(self, request: ScanRequest, cursor: SourceCursor | None) -> ScanBatch:
        raw: list[RawItem] = []
        derivatives: list[Derivative] = []
        skipped = 0
        continuing_full = bool(cursor and cursor.value.get("reconciling") and cursor.value.get("mode") == "full")
        reconciliation = bool(request.full or continuing_full)
        starting_full = bool(request.full and not continuing_full)
        previous = int(cursor.value.get("visit_id", 0)) if cursor and not starting_full else 0
        highwater = previous
        limited = False
        if self.history_path.exists():
            with sqlite_snapshot(self.history_path, max_bytes=1024 * 1024 * 1024) as snapshot:
                conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"history_items", "history_visits"}.issubset(tables):
                    conn.close()
                    raise ValueError("safari_schema_unsupported")
                columns = {row[1] for row in conn.execute("PRAGMA table_info(history_visits)")}
                if not {"id", "history_item", "visit_time"}.issubset(columns):
                    conn.close()
                    raise ValueError("safari_schema_unsupported")
                # Safari stores the page title on the visit, not the history
                # item. Older/minimal schemas can omit the optional title.
                title_expr = "v.title" if "title" in columns else "''"
                rows = conn.execute(
                    f"""SELECT v.id AS visit_id,v.visit_time,i.url,{title_expr} AS title
                       FROM history_visits v JOIN history_items i ON i.id=v.history_item
                       WHERE v.id>? ORDER BY v.id LIMIT ?""",
                    (previous, request.limit + 1),
                ).fetchall()
                conn.close()
            if len(rows) >= request.limit:
                limited = True
            rows = rows[: request.limit]
            for row in rows:
                visit_id = int(row["visit_id"])
                highwater = max(highwater, visit_id)
                url = strip_url_secrets(str(row["url"] or ""))
                if not url:
                    skipped += 1
                    continue
                when = _safari_time(row["visit_time"])
                external_id = f"visit:{visit_id}"
                title = redact_text(str(row["title"] or ""), limit=300)
                payload = {"url": url, "title": title, "visited_at": when, "url_hash": stable_hash(url, namespace="safari-url")}
                raw.append(RawItem(external_id, payload, when))
                derivatives.append(Derivative(external_id, _event(external_id, payload, "history"), export_allowed=False))
        bookmark_state = cursor.value.get("bookmarks") if cursor and isinstance(cursor.value.get("bookmarks"), dict) and not starting_full else {}
        bookmark_marker = str(bookmark_state.get("content_hash") or (cursor.value.get("bookmarks_hash", "") if cursor and not starting_full else ""))
        bookmark_offset = int(bookmark_state.get("record_index", 0) or 0)
        bookmark_complete = bool(bookmark_state.get("complete", bool(bookmark_marker and not bookmark_state)))
        next_bookmark_state: dict[str, Any] = dict(bookmark_state)
        if self.bookmarks_path.exists() and not limited:
            path = validate_regular_source(self.bookmarks_path, max_bytes=50 * 1024 * 1024)
            data = path.read_bytes()
            next_bookmark_marker = hashlib.sha256(data).hexdigest()
            if starting_full or next_bookmark_marker != bookmark_marker:
                bookmark_offset = 0
                bookmark_complete = False
            if not bookmark_complete:
                try:
                    plist = plistlib.loads(data)
                except Exception as exc:
                    raise ValueError("safari_bookmarks_invalid") from exc
                bookmarks = _bookmarks(plist)
                remaining = request.limit - len(raw)
                page = bookmarks[bookmark_offset : bookmark_offset + remaining]
                bookmark_limited = bool(remaining <= 0 or len(page) >= remaining)
                for bookmark in page:
                    url = strip_url_secrets(str(bookmark.get("URLString") or ""))
                    if not url:
                        skipped += 1
                        continue
                    external_id = "bookmark:" + stable_hash(url, namespace="safari-bookmark")
                    title = redact_text(str((bookmark.get("URIDictionary") or {}).get("title") or bookmark.get("Title") or ""), limit=300)
                    payload = {"url": url, "title": title, "bookmarked": True, "url_hash": stable_hash(url, namespace="safari-url")}
                    raw.append(RawItem(external_id, payload, ""))
                    derivatives.append(Derivative(external_id, _event(external_id, payload, "bookmark")))
                next_offset = bookmark_offset + len(page)
                limited = limited or bookmark_limited
                next_bookmark_state = {
                    "content_hash": next_bookmark_marker,
                    "record_index": next_offset if bookmark_limited else 0,
                    "complete": not bookmark_limited,
                }
            else:
                next_bookmark_state = {"content_hash": next_bookmark_marker, "record_index": 0, "complete": True}
        cursor_value: dict[str, Any] = {
            "visit_id": highwater,
            "bookmarks": next_bookmark_state,
            "bookmarks_hash": str(next_bookmark_state.get("content_hash") or bookmark_marker),
            "reconciling": bool(reconciliation and limited),
        }
        if reconciliation and limited:
            cursor_value["mode"] = "full"
        return ScanBatch(
            tuple(raw), tuple(derivatives), SourceCursor(cursor_value), "safari-history-bookmarks-v1", skipped,
            "partial" if limited else "ok", full_reconciliation=reconciliation, scan_complete=not limited,
        )


def _safari_time(value: Any) -> str:
    try:
        return (CORE_EPOCH + dt.timedelta(seconds=float(value))).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return ""


def _bookmarks(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("WebBookmarkType") == "WebBookmarkTypeLeaf" and value.get("URLString"):
            result.append(value)
        for child in value.get("Children", []):
            result.extend(_bookmarks(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_bookmarks(child))
    return result


def _event(external_id: str, payload: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "platform": "safari",
        "conversation_id": kind,
        "conversation_name": "Safari",
        "conversation_type": kind,
        "body": f"{payload.get('title') or 'Web page'} — {payload['url']}",
        "message_ts": payload.get("visited_at") or "",
        "source_message_id": external_id,
        "direction": "local",
        "raw": {"source": "safari", "kind": kind, "redaction_version": REDACTION_VERSION},
    }
