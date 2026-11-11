from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .ids import stable_id


REQUIRED_SUMMARY_COLUMNS = {
    "node_id",
    "session_id",
    "depth",
    "summary",
    "token_count",
    "source_token_count",
    "source_ids",
    "source_type",
    "created_at",
    "earliest_at",
    "latest_at",
    "expand_hint",
}


@dataclass(frozen=True)
class LcmSummaryRecord:
    id: str
    source_db: str
    source_db_id: str
    node_id: int
    session_id: str
    depth: int
    summary: str
    token_count: int | None
    source_token_count: int | None
    source_ids: str | None
    source_type: str | None
    created_at: float | None
    earliest_at: float | None
    latest_at: float | None
    expand_hint: str | None

    @property
    def filename(self) -> str:
        return f"lcm-{self.id}.md"

    def as_markdown(self) -> str:
        metadata = {
            "id": self.id,
            "source_db": self.source_db,
            "source_db_id": self.source_db_id,
            "session_id": self.session_id,
            "node_id": self.node_id,
            "depth": self.depth,
            "created_at": self.created_at,
            "earliest_at": self.earliest_at,
            "latest_at": self.latest_at,
            "token_count": self.token_count,
            "source_token_count": self.source_token_count,
            "source_ids": self.source_ids,
            "source_type": self.source_type,
            "expand_hint": self.expand_hint,
        }
        lines = ["---"]
        for key, value in metadata.items():
            lines.append(f"{key}: {json.dumps(value, sort_keys=True)}")
        lines.extend(["---", "", self.summary.rstrip(), ""])
        return "\n".join(lines)


def export_lcm_summaries(db_paths: list[Path], output_dir: Path) -> int:
    safe_dir = _prepare_output_dir(output_dir)
    count = 0
    for db_path in db_paths:
        for record in read_lcm_summaries(db_path):
            _write_atomic_if_changed(safe_dir / record.filename, record.as_markdown())
            count += 1
    return count


def read_lcm_summaries(db_path: Path) -> list[LcmSummaryRecord]:
    if not db_path.exists():
        raise FileNotFoundError(f"LCM database not found: {db_path}")
    source_db = str(db_path)
    source_db_id = stable_id("lcm-db", str(db_path.resolve()))
    conn = sqlite3.connect(_readonly_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "summary_nodes" not in tables:
            raise ValueError(f"LCM database has no summary_nodes table: {db_path}")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(summary_nodes)")}
        missing = sorted(REQUIRED_SUMMARY_COLUMNS - cols)
        if missing:
            raise ValueError(f"summary_nodes missing required columns in {db_path}: {', '.join(missing)}")
        rows = conn.execute(
            """
            SELECT node_id, session_id, depth, summary, token_count, source_token_count,
                   source_ids, source_type, created_at, earliest_at, latest_at, expand_hint
            FROM summary_nodes
            ORDER BY session_id, node_id
            """
        )
        records = []
        for row in rows:
            session_id = str(row["session_id"])
            node_id = int(row["node_id"])
            record_id = stable_id("lcm-summary", source_db_id, session_id, str(node_id))
            records.append(
                LcmSummaryRecord(
                    id=record_id,
                    source_db=source_db,
                    source_db_id=source_db_id,
                    node_id=node_id,
                    session_id=session_id,
                    depth=int(row["depth"]),
                    summary=str(row["summary"]),
                    token_count=_int_or_none(row["token_count"]),
                    source_token_count=_int_or_none(row["source_token_count"]),
                    source_ids=_str_or_none(row["source_ids"]),
                    source_type=_str_or_none(row["source_type"]),
                    created_at=_float_or_none(row["created_at"]),
                    earliest_at=_float_or_none(row["earliest_at"]),
                    latest_at=_float_or_none(row["latest_at"]),
                    expand_hint=_str_or_none(row["expand_hint"]),
                )
            )
        return records
    finally:
        conn.close()


def _readonly_uri(path: Path) -> str:
    return f"file:{quote(path.resolve().as_posix())}?mode=ro"


def _prepare_output_dir(output_dir: Path) -> Path:
    if output_dir.exists() and output_dir.is_symlink():
        raise ValueError(f"output directory must not be a symlink: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise ValueError(f"output path is not a directory: {output_dir}")
    return output_dir.resolve()


def _write_atomic_if_changed(target: Path, content: str) -> bool:
    if target.exists():
        if target.is_symlink():
            raise ValueError(f"refusing to overwrite symlink: {target}")
        if target.read_text(encoding="utf-8") == content:
            return False
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return True


def _str_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def _int_or_none(value: object) -> int | None:
    return None if value is None else int(value)


def _float_or_none(value: object) -> float | None:
    return None if value is None else float(value)

