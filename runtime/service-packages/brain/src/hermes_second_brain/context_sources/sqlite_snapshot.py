from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024


def validate_regular_source(path: Path | str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Path:
    source = Path(path).expanduser()
    if source.is_symlink():
        raise ValueError("source_symlink_rejected")
    try:
        info = source.stat()
    except OSError as exc:
        raise ValueError("source_unavailable") from exc
    if not source.is_file():
        raise ValueError("source_not_regular_file")
    if info.st_size > max_bytes:
        raise ValueError("source_too_large")
    return source


def readonly_uri(path: Path | str) -> str:
    source = validate_regular_source(path)
    return f"file:{quote(str(source.resolve()), safe='/')}?mode=ro&immutable=0"


@contextmanager
def sqlite_snapshot(path: Path | str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Iterator[Path]:
    """Create a consistent private snapshot without ever opening the source writable."""
    source = validate_regular_source(path, max_bytes=max_bytes)
    with tempfile.TemporaryDirectory(prefix="hermes-context-snapshot-") as td:
        target = Path(td) / "snapshot.sqlite3"
        src: sqlite3.Connection | None = None
        dst: sqlite3.Connection | None = None
        try:
            uri = f"file:{quote(str(source.resolve()), safe='/')}?mode=ro"
            src = sqlite3.connect(uri, uri=True)
            src.execute("PRAGMA query_only=ON")
            src.execute("PRAGMA busy_timeout=3000")
            dst = sqlite3.connect(target)
            src.backup(dst)
            dst.close()
            dst = None
            os.chmod(target, 0o600)
            yield target
        except sqlite3.Error as exc:
            raise ValueError("sqlite_snapshot_failed") from exc
        finally:
            if dst is not None:
                dst.close()
            if src is not None:
                src.close()
