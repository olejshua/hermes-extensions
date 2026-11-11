#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


def bootstrap_source_path() -> None:
    candidates: list[Path] = []
    project_dir = os.environ.get("PROJECT_DIR")
    if project_dir:
        candidates.append(Path(project_dir).expanduser() / "src")
    candidates.append(Path(__file__).resolve().parents[1] / "src")
    candidates.append(Path.home() / ".hermes" / "second-brain" / "app" / "src")
    for candidate in candidates:
        if candidate.is_dir():
            sys.path.insert(0, str(candidate))
            return
    sys.path.insert(0, str(candidates[-1]))


bootstrap_source_path()

from hermes_second_brain.signal_desktop import main


if __name__ == "__main__":
    raise SystemExit(main())
