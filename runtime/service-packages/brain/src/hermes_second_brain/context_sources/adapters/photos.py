from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from ..models import Derivative, RawItem, ScanBatch, ScanRequest, Sensitivity, SourceCursor, SourceHealth, SourceStatus
from ..redaction import REDACTION_VERSION, stable_hash
from ..sqlite_snapshot import sqlite_snapshot, validate_regular_source

CORE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)
TABLE_ALLOWLIST = ("ZASSET", "RKMaster")
COLUMN_ALIASES = {
    "id": ("ZUUID", "uuid", "Z_PK", "modelId"),
    "created": ("ZDATECREATED", "dateCreated", "datetime", "createDate"),
    "media": ("ZKIND", "ZMEDIATYPE", "kind", "type"),
    "favorite": ("ZFAVORITE", "favorite", "isFavorite"),
    "duration": ("ZDURATION", "duration"),
    "latitude": ("ZLATITUDE", "latitude"),
    "longitude": ("ZLONGITUDE", "longitude"),
}


class PhotosAdapter:
    source_name = "photos"
    sensitivity = Sensitivity.RESTRICTED
    retention = (30, 90)

    def __init__(self, library_path: Path | str | None = None, *, include_coarse_region: bool = False):
        self.library_path = Path(library_path or Path.home() / "Pictures/Photos Library.photoslibrary/database/Photos.sqlite").expanduser()
        self.include_coarse_region = include_coarse_region

    def health(self, request: ScanRequest) -> SourceHealth:
        try:
            validate_regular_source(self.library_path)
        except ValueError:
            return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, "photos_permission_or_data_unavailable")
        return SourceHealth(self.source_name, SourceStatus.HEALTHY, "ok", "photos-metadata-v1")

    def scan(self, request: ScanRequest, cursor: SourceCursor | None) -> ScanBatch:
        continuing_full = bool(cursor and cursor.value.get("reconciling") and cursor.value.get("mode") == "full")
        reconciliation = bool(request.full or continuing_full)
        previous = int(cursor.value.get("rowid", 0)) if cursor and (not request.full or continuing_full) else 0
        with sqlite_snapshot(self.library_path) as snapshot:
            conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            table = next((name for name in TABLE_ALLOWLIST if name in tables), None)
            if not table:
                conn.close()
                raise ValueError("photos_schema_unsupported")
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            selected = {key: next((column for column in aliases if column in columns), None) for key, aliases in COLUMN_ALIASES.items()}
            if not selected["id"] or not selected["created"]:
                conn.close()
                raise ValueError("photos_schema_unsupported")
            fields = ["rowid AS _rowid"] + [f'"{column}" AS "{key}"' for key, column in selected.items() if column]
            rows = conn.execute(f'SELECT {", ".join(fields)} FROM "{table}" WHERE rowid>? ORDER BY rowid LIMIT ?', (previous, request.limit + 1)).fetchall()
            fingerprint = hashlib.sha256((table + "|" + "|".join(sorted(columns & {c for v in COLUMN_ALIASES.values() for c in v}))).encode()).hexdigest()
            conn.close()
        has_more = len(rows) > request.limit
        rows = rows[: request.limit]
        raw: list[RawItem] = []
        derivatives: list[Derivative] = []
        highwater = previous
        for row in rows:
            data = dict(row)
            highwater = max(highwater, int(data["_rowid"]))
            asset_id = stable_hash(str(data.get("id")), namespace="photos-asset")
            created = _photo_time(data.get("created"))
            payload: dict[str, Any] = {
                "asset_hash": asset_id,
                "created_at": created,
                "media_type": _media_type(data.get("media")),
                "favorite": bool(data.get("favorite")),
                "duration_seconds": _bounded_float(data.get("duration"), 0, 86400),
            }
            if self.include_coarse_region:
                region = _coarse_region(data.get("latitude"), data.get("longitude"))
                if region:
                    payload["coarse_region"] = region
            external_id = f"asset:{asset_id}"
            raw.append(RawItem(external_id, payload, created))
            derivatives.append(
                Derivative(external_id, {
                    "platform": "photos",
                    "conversation_id": "library",
                    "conversation_name": "Photos metadata",
                    "conversation_type": "media_library",
                    "body": f"{payload['media_type']} captured" + (" (favorite)" if payload["favorite"] else ""),
                    "message_ts": created,
                    "source_message_id": external_id,
                    "direction": "local",
                    "raw": {"source": "photos", "redaction_version": REDACTION_VERSION},
                }, export_allowed=payload["favorite"])
            )
        cursor_value = {"rowid": highwater, "reconciling": bool(reconciliation and has_more)}
        if reconciliation and has_more:
            cursor_value["mode"] = "full"
        return ScanBatch(
            tuple(raw), tuple(derivatives), SourceCursor(cursor_value), fingerprint,
            reason_code="partial" if has_more else "ok",
            full_reconciliation=reconciliation,
            scan_complete=not has_more,
        )


def _photo_time(value: Any) -> str:
    if isinstance(value, str):
        return value[:40]
    try:
        number = float(value)
        epoch = CORE_EPOCH if number < 1_500_000_000 else dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        return (epoch + dt.timedelta(seconds=number)).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return ""


def _media_type(value: Any) -> str:
    return {0: "photo", 1: "video", 2: "audio"}.get(value, "media")


def _bounded_float(value: Any, low: float, high: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 3) if low <= number <= high else None


def _coarse_region(latitude: Any, longitude: Any) -> str:
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return ""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return ""
    return f"{round(lat):+03d},{round(lon):+04d}"
