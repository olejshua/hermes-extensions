from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .ids import stable_id


@dataclass(frozen=True)
class MigrationRecord:
    id: str
    source: str
    category: str
    text: str
    tags: tuple[str, ...]
    trust: str
    created_at: str | None
    updated_at: str | None
    fact_id: str | None = None
    trust_score: str | None = None

    def as_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "source": self.source,
                "category": self.category,
                "text": self.text,
                "tags": list(self.tags),
                "trust": self.trust,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "fact_id": self.fact_id,
                "trust_score": self.trust_score,
            },
            sort_keys=True,
        )

    def as_markdown(self) -> str:
        metadata = {
            "id": self.id,
            "source": self.source,
            "category": self.category,
            "tags": list(self.tags),
            "trust": self.trust,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "fact_id": self.fact_id,
            "trust_score": self.trust_score,
        }
        lines = ["---"]
        for key, value in metadata.items():
            lines.append(f"{key}: {json.dumps(value, sort_keys=True)}")
        lines.extend(["---", "", self.text.rstrip(), ""])
        return "\n".join(lines)


def migrate_markdown(path: Path, source_name: str) -> list[MigrationRecord]:
    text = path.read_text(encoding="utf-8")
    records: list[MigrationRecord] = []
    category = path.stem.upper()
    for block in _blocks(text):
        rid = stable_id(source_name, category, block)
        records.append(MigrationRecord(rid, str(path), category, block, (category.lower(), "builtin-memory"), "user-authored", None, None))
    return _dedupe(records)


def migrate_holographic_db(path: Path) -> list[MigrationRecord]:
    if not path.exists():
        return []
    rows: list[MigrationRecord] = []
    conn = sqlite3.connect(_readonly_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        table = "facts" if "facts" in tables else next(iter(tables), None)
        if table is None:
            return []
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if table == "facts" and {"fact_id", "content", "category", "tags", "trust_score", "created_at", "updated_at"}.issubset(cols):
            for row in conn.execute("SELECT fact_id, content, category, tags, trust_score, created_at, updated_at FROM facts"):
                text = str(row["content"]).strip() if row["content"] is not None else ""
                if not text:
                    continue
                fact_id = str(row["fact_id"])
                trust_score = _nullable(row, "trust_score")
                rows.append(
                    MigrationRecord(
                        stable_id("holographic", fact_id, text),
                        str(path),
                        str(row["category"]) if row["category"] is not None else "fact",
                        text,
                        tuple(_parse_tags(str(row["tags"]))) if row["tags"] else ("holographic",),
                        trust_score or "imported",
                        _nullable(row, "created_at"),
                        _nullable(row, "updated_at"),
                        fact_id=fact_id,
                        trust_score=trust_score,
                    )
                )
            return _dedupe(rows)

        text_col = _first(cols, "text", "fact", "content", "value")
        if text_col is None:
            return []
        id_col = _first(cols, "id", "uuid", "key")
        cat_col = _first(cols, "category", "type")
        tags_col = _first(cols, "tags")
        trust_col = _first(cols, "trust", "confidence")
        created_col = _first(cols, "created_at", "created", "timestamp")
        updated_col = _first(cols, "updated_at", "updated")
        for row in conn.execute(f"SELECT * FROM {table}"):
            text = str(row[text_col]).strip()
            if not text:
                continue
            original_id = str(row[id_col]) if id_col else stable_id("holographic", text)
            category = str(row[cat_col]) if cat_col else "fact"
            tags = tuple(_parse_tags(str(row[tags_col]))) if tags_col and row[tags_col] else ("holographic",)
            trust = str(row[trust_col]) if trust_col and row[trust_col] else "imported"
            rows.append(MigrationRecord(stable_id("holographic", original_id, text), str(path), category, text, tags, trust, _nullable(row, created_col), _nullable(row, updated_col)))
    finally:
        conn.close()
    return _dedupe(rows)


def write_jsonl(records: list[MigrationRecord], output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    count = 0
    with output.open("w", encoding="utf-8") as handle:
        for record in records:
            if record.id in seen:
                continue
            seen.add(record.id)
            handle.write(record.as_json() + "\n")
            count += 1
    return count


def write_markdown_records(records: list[MigrationRecord], output_dir: Path) -> int:
    safe_dir = _prepare_output_dir(output_dir)
    seen: set[str] = set()
    count = 0
    for record in records:
        if record.id in seen:
            continue
        seen.add(record.id)
        target = safe_dir / f"migration-{record.id}.md"
        _write_atomic_if_changed(target, record.as_markdown())
        count += 1
    return count


def _blocks(text: str) -> list[str]:
    parts = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n")]
    return [p for p in parts if p and not p.lstrip().startswith("#")]


def _dedupe(records: list[MigrationRecord]) -> list[MigrationRecord]:
    out: dict[str, MigrationRecord] = {}
    for record in records:
        out.setdefault(record.id, record)
    return list(out.values())


def _first(cols: set[str], *names: str) -> str | None:
    for name in names:
        if name in cols:
            return name
    return None


def _parse_tags(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(parsed, list):
        return [str(p) for p in parsed]
    return [str(parsed)]


def _nullable(row: sqlite3.Row, col: str | None) -> str | None:
    return str(row[col]) if col and row[col] is not None else None


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
