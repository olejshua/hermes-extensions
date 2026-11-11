#!/usr/bin/env python3
"""Repository wrapper for claim/ack, at-least-once Dream morning delivery."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hermes_second_brain.context_inbox import DEFAULT_CONTEXT_DB  # noqa: E402
from hermes_second_brain.dreaming.config import load_dreaming_config  # noqa: E402
from hermes_second_brain.operations import emit_morning_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate configuration and paths without claiming or emitting a report",
    )
    args = parser.parse_args(argv)
    try:
        config_path = args.config.expanduser().absolute()
        config = load_dreaming_config(config_path)
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        context_raw = raw.get("context_inbox_db", str(DEFAULT_CONTEXT_DB))
        if not isinstance(context_raw, str) or not context_raw:
            return 1
        context_db = Path(context_raw).expanduser()
        if not context_db.is_absolute():
            context_db = config_path.parent / context_db
        if args.validate_only:
            return 0
        owner = f"morning-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        return emit_morning_report(
            config,
            context_db=context_db,
            stdout=sys.stdout,
            owner=owner,
        )
    except Exception:
        # Scheduled no-content/failure paths must not emit misleading stdout.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
