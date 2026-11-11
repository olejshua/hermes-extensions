#!/usr/bin/env python3
"""Quietly acknowledge local Dream outbox rows after verified generic sync."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hermes_second_brain.dreaming.config import load_dreaming_config  # noqa: E402
from hermes_second_brain.dreaming.store import DreamStore  # noqa: E402
from hermes_second_brain.ids import source_id  # noqa: E402
from hermes_second_brain.manifest import load_manifest  # noqa: E402
from hermes_second_brain.scanner import sha256_file  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config_path = args.config.expanduser().absolute()
        config = load_dreaming_config(config_path)
        if not config.dream_state_db.is_file() or config.dream_state_db.is_symlink():
            return 0
        store = DreamStore(config.dream_state_db)
        publications = store.locally_published_publications()
        if not publications:
            return 0
        manifest = load_manifest(config_path)
        sources = [
            source
            for source in manifest.sources
            if source.namespace == "dreams"
            and source.root.absolute() == config.import_dir.absolute()
        ]
        if len(sources) != 1:
            return 1
        source = sources[0]
        if (
            not source.root.is_dir()
            or source.root.is_symlink()
            or not manifest.state_db.is_file()
            or manifest.state_db.is_symlink()
        ):
            return 1
        failed = 0
        for row in publications:
            try:
                path = Path(row["final_import_path"]).absolute()
                if (
                    path.parent != source.root.absolute()
                    or path.is_symlink()
                    or not path.is_file()
                    or sha256_file(path) != row["content_hash"]
                ):
                    raise ValueError("committed Dream import is unavailable or changed")
                exact_source_id = source_id(source.namespace, path, source.root)
                store.mark_publication_synced(
                    row["publication_id"],
                    state_db=manifest.state_db,
                    source_id=exact_source_id,
                )
            except Exception:
                failed += 1
        return 1 if failed else 0
    except Exception:
        # The outbox remains locally_published and retryable. Never print raw
        # paths or database/model diagnostics from a scheduled acknowledgement.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
