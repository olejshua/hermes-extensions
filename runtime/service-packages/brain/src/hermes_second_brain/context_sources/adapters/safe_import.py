from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..models import Derivative, RawItem, ScanBatch, ScanRequest, Sensitivity, SourceCursor, SourceHealth, SourceStatus
from ..redaction import REDACTION_VERSION, contains_financial_identifier, redact_text, stable_hash
from ..sqlite_snapshot import validate_regular_source

SAFE_IMPORT_SOURCES = frozenset({"health", "sleep", "workouts", "banking", "purchases", "screen-time"})
CONTINUOUS_INGRESS_SOURCES = frozenset({"health", "sleep", "workouts", "screen-time"})
FORMATS = frozenset({"json", "jsonl", "csv", "zip"})
DEFAULT_INGRESS_DIR = Path("~/.hermes/second-brain/context-ingress").expanduser()
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_INGRESS_FILES = 256
MAX_ZIP_MEMBERS = 100
MAX_RECORD_FIELDS = 32
MAX_TEXT_INPUT = 4096
MAX_TEXT_OUTPUT = 500

FORBIDDEN_KEYS = frozenset({
    "latitude", "longitude", "gps", "route", "coordinates", "location", "address", "diagnosis", "diagnoses", "medical_record",
    "iban", "bic", "swift", "pan", "card_number", "account_number", "routing_number", "access_token", "password",
})
_FORBIDDEN_VALUE = re.compile(
    r"(?i)(?:\bdiagnos(?:is|es|ed|tic)?\b|\bmedical[ _-]?record\b|\b(?:gps|latitude|longitude|coordinates?)\b|\b(?:home|work)?[ _-]?(?:location|address)\b)"
)
COMMON = frozenset({"id", "external_id", "date", "timestamp", "start", "end", "source", "unit"})
SOURCE_FIELDS = {
    "health": COMMON | {"metric", "value", "average", "minimum", "maximum", "count"},
    "sleep": COMMON | {"duration_minutes", "asleep_minutes", "awake_minutes", "quality", "bedtime", "wake_time"},
    "workouts": COMMON | {"workout_type", "duration_minutes", "distance_km", "energy_kcal", "average_heart_rate"},
    "banking": COMMON | {"amount", "currency", "category", "merchant", "merchant_category", "direction"},
    "purchases": COMMON | {"amount", "currency", "category", "merchant", "merchant_category", "quantity"},
    "screen-time": COMMON | {"total_minutes", "category", "minutes", "pickups", "notifications", "app_category"},
}
NUMERIC_FIELDS = frozenset({
    "value", "average", "minimum", "maximum", "count", "duration_minutes", "asleep_minutes", "awake_minutes",
    "distance_km", "energy_kcal", "average_heart_rate", "amount", "quantity", "total_minutes", "minutes", "pickups", "notifications",
})


def prepare_ingress_directory(root: Path | str = DEFAULT_INGRESS_DIR, *, create: bool = True) -> Path:
    """Return the private, scanner-owned ingress root.

    Exporters publish by atomically renaming complete files into a source
    subdirectory. The scanner never edits, moves, or deletes those files.
    """
    path = Path(root).expanduser()
    if path.is_symlink():
        raise ValueError("ingress_symlink_rejected")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.is_symlink():
        raise ValueError("ingress_unavailable")
    if create:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


class SafeImportAdapter:
    sensitivity = Sensitivity.RESTRICTED
    retention = (90, 365)

    def __init__(self, source_name: str, path: Path | str, format: str | None = None, *, ingress: bool = False):
        if source_name not in SAFE_IMPORT_SOURCES:
            raise ValueError("unsupported_safe_import_source")
        self.source_name = source_name
        self.path = Path(path).expanduser()
        self.ingress = bool(ingress)
        inferred = self.path.suffix.lower().lstrip(".")
        self.format = (format or inferred).lower()
        if not self.ingress and self.format not in FORMATS:
            raise ValueError("unsupported_import_format")
        if self.ingress and source_name not in CONTINUOUS_INGRESS_SOURCES:
            raise ValueError("unsupported_ingress_source")
        self.retention = (180, 365) if source_name == "screen-time" else (90, 365)

    @classmethod
    def ingress_adapter(cls, source_name: str, root: Path | str = DEFAULT_INGRESS_DIR) -> SafeImportAdapter:
        return cls(source_name, Path(root).expanduser() / source_name, ingress=True)

    def health(self, request: ScanRequest) -> SourceHealth:
        try:
            if self.ingress:
                self._ingress_path(create=not request.dry_run)
                self._discover_files(hash_content=False)
            else:
                validate_regular_source(self.path, max_bytes=MAX_TOTAL_BYTES)
        except (OSError, ValueError):
            reason = "ingress_unavailable" if self.ingress else "safe_import_unavailable"
            return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, reason, self._fingerprint)
        return SourceHealth(self.source_name, SourceStatus.HEALTHY, "ok", self._fingerprint)

    @property
    def _fingerprint(self) -> str:
        return f"safe-import-{self.source_name}-v2"

    def scan(self, request: ScanRequest, cursor: SourceCursor | None) -> ScanBatch:
        files = self._discover_files(hash_content=True) if self.ingress else [self._file_descriptor(self.path, self.format)]
        prior = dict(cursor.value) if cursor else {}
        continuing_reconciliation = bool(prior.get("reconciling"))
        start_reconciliation = bool(request.full and not continuing_reconciliation)
        if start_reconciliation:
            prior = {}
        reconciling = continuing_reconciliation or start_reconciliation
        completed = [str(value) for value in prior.get("completed_hashes", []) if isinstance(value, str)]
        completed_set = set(completed)
        active = prior.get("active") if isinstance(prior.get("active"), dict) else {}
        active_hash = str(active.get("content_hash") or "")
        resume_member = _cursor_int(active.get("member_index"), -1)
        resume_record = _cursor_int(active.get("record_index"), -1)
        raw: list[RawItem] = []
        derivatives: list[Derivative] = []
        skipped = 0
        limited = False
        next_active: dict[str, Any] | None = None

        if active_hash:
            active_files = [descriptor for descriptor in files if descriptor[2] == active_hash]
            if reconciling and not active_files:
                raise ValueError("active_ingress_file_missing")
            files = active_files + [descriptor for descriptor in files if descriptor[2] != active_hash]

        for path, file_format, digest in files:
            if digest in completed_set:
                continue
            member_cursor = resume_member if digest == active_hash else -1
            record_cursor = resume_record if digest == active_hash else -1
            iterator = self._records(path, file_format, digest)
            exhausted = True
            for member_index, record_index, record in iterator:
                if (member_index, record_index) <= (member_cursor, record_cursor):
                    continue
                next_active = {
                    "content_hash": digest,
                    "member_index": member_index,
                    "record_index": record_index,
                }
                try:
                    payload = validate_record(self.source_name, record)
                except ValueError:
                    skipped += 1
                    continue
                external_value = record.get("id") or record.get("external_id")
                external_id = str(external_value or stable_hash(json.dumps(payload, sort_keys=True, allow_nan=False), namespace=f"{self.source_name}-record"))
                if len(external_id.encode("utf-8")) > 1024:
                    skipped += 1
                    continue
                observed = str(payload.get("date") or payload.get("timestamp") or payload.get("start") or "")
                raw.append(RawItem(external_id, payload, observed))
                derivatives.append(Derivative(external_id, _event(self.source_name, external_id, payload)))
                if len(raw) >= request.limit:
                    exhausted = False
                    limited = True
                    break
            if not exhausted:
                break
            completed.append(digest)
            completed_set.add(digest)
            next_active = None
            active_hash, resume_member, resume_record = "", -1, -1

        if len(completed) > MAX_INGRESS_FILES:
            raise ValueError("ingress_cursor_too_large")
        scan_complete = not limited
        cursor_value: dict[str, Any] = {
            "version": 2,
            "completed_hashes": completed,
            "reconciling": bool(reconciling and not scan_complete),
        }
        if next_active is not None:
            cursor_value["active"] = next_active
        reason = "partial" if limited else "unchanged" if not raw and not skipped else "ok"
        return ScanBatch(
            tuple(raw),
            tuple(derivatives),
            SourceCursor(cursor_value),
            self._fingerprint,
            skipped,
            reason,
            full_reconciliation=reconciling,
            scan_complete=scan_complete,
        )

    def _ingress_path(self, *, create: bool) -> Path:
        root = prepare_ingress_directory(self.path.parent, create=create)
        path = root / self.source_name
        if path != self.path or path.is_symlink():
            raise ValueError("ingress_path_rejected")
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        if not path.is_dir() or path.is_symlink():
            raise ValueError("ingress_unavailable")
        return path

    def _discover_files(self, *, hash_content: bool) -> list[tuple[Path, str, str]]:
        root = self._ingress_path(create=False)
        entries: list[tuple[Path, str, str]] = []
        total = 0
        directory_entries = sorted(root.iterdir(), key=lambda item: item.name)
        if len(directory_entries) > MAX_INGRESS_FILES * 2:
            raise ValueError("ingress_entry_limit_exceeded")
        for path in directory_entries:
            if path.name.startswith(".") or path.suffix.lower().lstrip(".") not in FORMATS:
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError("unsafe_ingress_entry")
            info = path.stat()
            if info.st_size > MAX_FILE_BYTES:
                raise ValueError("ingress_file_too_large")
            total += info.st_size
            if total > MAX_TOTAL_BYTES or len(entries) >= MAX_INGRESS_FILES:
                raise ValueError("ingress_limits_exceeded")
            file_format = path.suffix.lower().lstrip(".")
            digest = self._content_hash(path) if hash_content else ""
            entries.append((path, file_format, digest))
        return entries

    def _file_descriptor(self, path: Path, file_format: str) -> tuple[Path, str, str]:
        source = validate_regular_source(path, max_bytes=MAX_TOTAL_BYTES)
        return source, file_format, self._content_hash(source)

    @staticmethod
    def _content_hash(path: Path) -> str:
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("ingress_file_not_atomic")
        return digest.hexdigest()

    def _records(self, path: Path, file_format: str, expected_digest: str) -> Iterator[tuple[int, int, dict[str, Any]]]:
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_digest:
            raise ValueError("ingress_file_changed_during_scan")
        if file_format != "zip":
            for record_index, record in enumerate(_decode_records(data, file_format)):
                yield 0, record_index, record
            return
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_MEMBERS or sum(info.file_size for info in infos) > MAX_TOTAL_BYTES:
                raise ValueError("zip_limits_exceeded")
            for member_index, info in enumerate(infos):
                member = Path(info.filename)
                if info.is_dir():
                    continue
                if member.is_absolute() or ".." in member.parts or info.file_size > MAX_FILE_BYTES:
                    raise ValueError("unsafe_zip_member")
                member_format = member.suffix.lower().lstrip(".")
                if member_format not in {"json", "jsonl", "csv"}:
                    continue
                for record_index, record in enumerate(_decode_records(archive.read(info), member_format)):
                    yield member_index, record_index, record


def _decode_records(data: bytes, file_format: str) -> Iterable[dict[str, Any]]:
    if file_format == "json":
        try:
            value = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_import_json") from exc
        records = value if isinstance(value, list) else value.get("records", []) if isinstance(value, dict) else []
        if not isinstance(records, list):
            raise ValueError("invalid_import_schema")
        for record in records:
            if isinstance(record, dict):
                yield record
        return
    if file_format == "jsonl":
        for line in data.decode("utf-8-sig").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record
        return
    if file_format == "csv":
        for record in csv.DictReader(io.StringIO(data.decode("utf-8-sig"))):
            yield dict(record)
        return
    raise ValueError("unsupported_import_format")


def validate_record(source: str, record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or not record or len(record) > MAX_RECORD_FIELDS:
        raise ValueError("invalid_record")
    lowered: dict[str, Any] = {}
    for original_key, value in record.items():
        key = str(original_key).strip().lower()
        if not key or len(key) > 64 or key in lowered:
            raise ValueError("invalid_record_key")
        if isinstance(value, (dict, list, tuple, set)) or not isinstance(value, (str, int, float, bool, type(None))):
            raise ValueError("non_primitive_value")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non_finite_number")
        if isinstance(value, str):
            if len(value) > MAX_TEXT_INPUT:
                raise ValueError("text_value_too_large")
            if _FORBIDDEN_VALUE.search(value) or contains_financial_identifier(value):
                raise ValueError("forbidden_sensitive_value")
        lowered[key] = value
    if any(key in FORBIDDEN_KEYS for key in lowered):
        raise ValueError("forbidden_sensitive_field")
    allowed = SOURCE_FIELDS[source]
    if set(lowered) - allowed:
        raise ValueError("unknown_field")

    payload: dict[str, Any] = {}
    for key, value in lowered.items():
        if value in (None, "") or key in {"id", "external_id"}:
            continue
        if key in NUMERIC_FIELDS:
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("invalid_numeric_value") from exc
            if not math.isfinite(number):
                raise ValueError("non_finite_number")
            value = int(number) if number.is_integer() else round(number, 6)
        elif isinstance(value, str) and key not in {"date", "timestamp", "start", "end", "bedtime", "wake_time"}:
            value = redact_text(value, limit=MAX_TEXT_OUTPUT)
        payload[key] = value

    if source in {"banking", "purchases"}:
        try:
            amount = round(float(payload["amount"]), 2)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid_amount") from exc
        currency = str(payload.get("currency") or "").upper()
        if not math.isfinite(amount) or not re.fullmatch(r"[A-Z]{3}", currency) or abs(amount) > 100_000_000:
            raise ValueError("invalid_finance_record")
        merchant = str(payload.pop("merchant", ""))
        payload["amount"] = amount
        payload["currency"] = currency
        if merchant:
            payload["merchant_hash"] = stable_hash(merchant.casefold(), namespace="merchant")
    timestamp = next((payload.get(key) for key in ("date", "timestamp", "start") if payload.get(key)), None)
    if timestamp is None or not _valid_date(timestamp):
        raise ValueError("invalid_date")
    if source == "health":
        if not payload.get("metric") or not _finite_number(payload.get("value", payload.get("average"))):
            raise ValueError("invalid_health_summary")
    elif source == "sleep":
        if not _bounded_number(payload.get("duration_minutes", payload.get("asleep_minutes")), 0, 24 * 60):
            raise ValueError("invalid_sleep_summary")
    elif source == "workouts":
        if not payload.get("workout_type") or not _bounded_number(payload.get("duration_minutes"), 0, 24 * 60):
            raise ValueError("invalid_workout_summary")
    elif source == "screen-time":
        if not _bounded_number(payload.get("total_minutes", payload.get("minutes")), 0, 24 * 60):
            raise ValueError("invalid_screen_time_aggregate")
    return payload


def _cursor_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if -1 <= number <= 10_000_000 else default


def _valid_date(value: Any) -> bool:
    text = str(value).strip()
    if len(text) > 64:
        return False
    try:
        dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        try:
            dt.date.fromisoformat(text)
            return True
        except ValueError:
            return False


def _finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _bounded_number(value: Any, low: float, high: float) -> bool:
    return _finite_number(value) and low <= float(value) <= high


def _event(source: str, external_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    timestamp = str(payload.get("date") or payload.get("timestamp") or payload.get("start") or "")
    if source in {"banking", "purchases"}:
        body = f"{source.title()} aggregate: {payload.get('amount')} {payload.get('currency')} / {payload.get('category') or payload.get('merchant_category') or 'uncategorized'}"
    elif source == "screen-time":
        body = f"Screen Time daily aggregate: {payload.get('total_minutes') or payload.get('minutes') or 0} minutes / {payload.get('category') or payload.get('app_category') or 'all'}"
    elif source == "sleep":
        body = f"Sleep summary: {payload.get('duration_minutes') or payload.get('asleep_minutes') or 0} minutes"
    elif source == "workouts":
        body = f"Workout summary: {payload.get('workout_type') or 'workout'} / {payload.get('duration_minutes') or 0} minutes"
    else:
        body = f"Health summary: {payload.get('metric') or 'aggregate'} {payload.get('value') or payload.get('average') or ''} {payload.get('unit') or ''}".strip()
    return {
        "platform": source,
        "conversation_id": "safe-import",
        "conversation_name": f"{source.title()} import",
        "conversation_type": "user_export",
        "body": redact_text(body, limit=1000),
        "message_ts": timestamp,
        "source_message_id": "import_" + stable_hash(external_id, namespace=f"{source}-item"),
        "direction": "local",
        "raw": {"source": source, "import": "schema_validated", "redaction_version": REDACTION_VERSION},
    }
