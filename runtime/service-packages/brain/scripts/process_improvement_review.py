#!/usr/bin/env python3
"""Import aggregate observatory metadata and run the gated B+ review pair."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR / "src"))

from hermes_second_brain.improvement import ImprovementStore  # noqa: E402
from hermes_second_brain.observatory import (  # noqa: E402
    DEFAULT_MAX_ROWS,
    ObservatoryStore,
    import_spool,
    observatory_report,
    validate_max_rows,
)
from hermes_second_brain.review_runner import (  # noqa: E402
    commands_from_manifest,
    run_review,
)


def _path(base: Path, value: object, default: str) -> Path:
    raw = value if isinstance(value, str) and value else default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (base / path).absolute()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate config/reviewer argv without creating stores or running reviewers",
    )
    args = parser.parse_args(argv)
    try:
        config_path = args.config.expanduser().absolute()
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        b_plus = raw.get("b_plus", {}) if isinstance(raw, dict) else {}
        if not isinstance(b_plus, dict):
            return 2
        base = config_path.parent
        observatory_db = _path(
            base, b_plus.get("observatory_db"),
            "~/.hermes/second-brain/process-observatory.sqlite3",
        )
        spool = _path(
            base, b_plus.get("observatory_spool"),
            "~/.hermes/second-brain/process-observatory-spool.d",
        )
        quarantine = _path(
            base, b_plus.get("observatory_quarantine"),
            "~/.hermes/second-brain/process-observatory-quarantine.d",
        )
        improvement_db = _path(
            base, b_plus.get("improvement_db"),
            "~/.hermes/second-brain/improvements.sqlite3",
        )
        retention = b_plus.get("observatory_retention_days", 90)
        if type(retention) is not int or not 1 <= retention <= 3650:
            return 2
        try:
            max_rows = validate_max_rows(
                b_plus.get("observatory_max_rows", DEFAULT_MAX_ROWS)
            )
        except ValueError:
            return 2
        if args.validate_only:
            commands_from_manifest(config_path)
            return 0
        observatory = ObservatoryStore(observatory_db)
        imported = import_spool(observatory, spool, quarantine_dir=quarantine)
        observatory.prune(retention_days=retention)
        # Absolute row cap runs after TTL pruning so the store, the report scan,
        # and the reviewer packet stay bounded no matter how large the spool was.
        observatory.enforce_row_cap(max_rows)
        report = observatory_report(observatory, max_rows=max_rows)
        if report["total"] == 0:
            return 0
        primary, secondary = commands_from_manifest(config_path)
        result = run_review(
            report,
            store=ImprovementStore(improvement_db),
            primary=primary,
            secondary=secondary,
        )
        result["imported"] = imported["imported"]
        result["quarantined"] = imported["quarantined"]
        if args.json or result.get("persisted", 0):
            sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
            sys.stdout.flush()
        return 0 if result.get("status") in {"ok", "noop"} else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
