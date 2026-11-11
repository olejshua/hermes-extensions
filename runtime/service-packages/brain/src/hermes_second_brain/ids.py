from __future__ import annotations

import hashlib
from pathlib import Path


def stable_id(*parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def source_id(namespace: str, path: Path, root: Path) -> str:
    rel = path.resolve().relative_to(root.resolve()).as_posix()
    return f"{namespace}:{stable_id(namespace, rel)}"
