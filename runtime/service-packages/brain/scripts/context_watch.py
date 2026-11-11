#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def bootstrap_source_path() -> Path:
    candidates: list[Path] = []
    project_dir = os.environ.get("PROJECT_DIR")
    if project_dir:
        candidates.append(Path(project_dir).expanduser() / "src")
    candidates.append(Path.home() / ".hermes" / "second-brain" / "app" / "src")
    candidates.append(Path(__file__).resolve().parents[1] / "src")
    for candidate in candidates:
        if candidate.is_dir():
            sys.path.insert(0, str(candidate))
            return candidate
    fallback = candidates[-1]
    sys.path.insert(0, str(fallback))
    return fallback


bootstrap_source_path()

from hermes_second_brain import queue_spool

import_jsonl_spool_helper = queue_spool.import_jsonl_spool


HOME = Path.home()
INSTALLED_PROJECT_DIR = HOME / ".hermes" / "second-brain" / "app"
DEFAULT_PROJECT_DIR = INSTALLED_PROJECT_DIR if INSTALLED_PROJECT_DIR.is_dir() else Path(__file__).resolve().parents[1]
DEFAULT_IMPORT_DIR = HOME / ".hermes" / "second-brain" / "import"
DEFAULT_CONTEXT_DB = HOME / ".hermes" / "second-brain" / "context-inbox.sqlite3"
DEFAULT_SOURCE_DB = HOME / ".hermes" / "second-brain" / "context-sources.sqlite3"
DEFAULT_CONTEXT_EXPORT = HOME / ".hermes" / "second-brain" / "import" / "context" / "context-inbox.txt"
DEFAULT_GATEWAY_SPOOL = HOME / ".hermes" / "second-brain" / "context-inbox-spool.jsonl"
DEFAULT_WHATSAPP = HOME / "Library" / "Group Containers" / "group.net.whatsapp.WhatsApp.shared" / "ChatStorage.sqlite"
DEFAULT_WATCH_STATE = HOME / ".hermes" / "second-brain" / "context-watch-state.json"


def main() -> int:
    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        print("usage: context_watch.py [--help]\n\nEnvironment: PROJECT_DIR CONTEXT_IMPORT_DIR CONTEXT_DB CONTEXT_EXPORT CONTEXT_INGRESS_DIR HERMES_CONTEXT_INBOX_SPOOL WHATSAPP_IMPORT_PATH PYTHON HERMES_SECOND_BRAIN_MODULE")
        return 0
    project_dir = Path(os.environ.get("PROJECT_DIR", DEFAULT_PROJECT_DIR))
    import_dir = Path(os.environ.get("CONTEXT_IMPORT_DIR", DEFAULT_IMPORT_DIR)).expanduser()
    db = Path(os.environ.get("CONTEXT_DB", DEFAULT_CONTEXT_DB)).expanduser()
    source_db = Path(os.environ.get("CONTEXT_SOURCE_DB", db.with_name("context-sources.sqlite3"))).expanduser()
    export_path = Path(os.environ.get("CONTEXT_EXPORT", DEFAULT_CONTEXT_EXPORT)).expanduser()
    module = os.environ.get("HERMES_SECOND_BRAIN_MODULE", "hermes_second_brain")
    python = os.environ.get("PYTHON", sys.executable)
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(project_dir / "src") + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    watch_state_path = Path(os.environ.get("CONTEXT_WATCH_STATE", DEFAULT_WATCH_STATE)).expanduser()
    watch_state = load_watch_state(watch_state_path)

    try:
        quarantined = 0
        quarantined += import_jsonl_spool(python, module, env, db, Path(os.environ.get("HERMES_CONTEXT_INBOX_SPOOL", DEFAULT_GATEWAY_SPOOL)).expanduser())
        import_if_exists(python, module, env, db, "jsonl", import_dir / "context-inbox-spool.jsonl")
        import_if_exists(python, module, env, db, "jsonl", import_dir / "canonical.jsonl")
        slack_dir = import_dir / "slack"
        slack_backfill = slack_dir / "slack-backfill-complete.jsonl.gz"
        if slack_backfill.exists():
            # A completed Slack backfill is immutable in normal operation. Replaying
            # it every five minutes re-writes hundreds of already-ranked rows and can
            # consume the entire scheduler budget. Bootstrap from an existing
            # materialization once, then re-import only if the artifact changes.
            if should_import_artifact(slack_backfill, watch_state):
                import_if_exists(python, module, env, db, "slack", slack_backfill)
                record_artifact_signature(watch_state_path, watch_state, slack_backfill)
        elif slack_dir.exists():
            import_if_exists(python, module, env, db, "slack", slack_dir)
        import_slack_live_if_exists(python, module, env, db, slack_dir)
        import_if_exists(python, module, env, db, "signal", import_dir / "signal.sqlite")
        whatsapp = Path(os.environ.get("WHATSAPP_IMPORT_PATH", DEFAULT_WHATSAPP)).expanduser()
        if not whatsapp.exists() and (import_dir / "ChatStorage.sqlite").exists():
            env["WHATSAPP_IMPORT_PATH"] = str(import_dir / "ChatStorage.sqlite")
        # The multi-source runner owns native-source cursors and isolates source
        # failures. pending_permission is expected and does not block ranking.
        try:
            run_cli(python, module, env, ["context-scan", "--all", "--db", str(db), "--source-db", str(source_db)], timeout_seconds=55)
        except (RuntimeError, subprocess.TimeoutExpired):
            # Per-source state already records failures. Ranking and alerts must
            # still run when any optional source is unavailable or busy.
            pass
        # Export refreshes ranks and reminders; reuse that preparation for alerts.
        run_cli(python, module, env, ["context-export-openviking", "--db", str(db), "--output", str(export_path)])
        result = subprocess.run(
            [python, "-m", module, "context-alerts", "--db", str(db), "--prepared"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            return result.returncode
        sys.stdout.write(result.stdout)
        if quarantined:
            print(f"context_watch: quarantined {quarantined} invalid JSONL queue file(s)", file=sys.stderr)
            return 1
        return 0
    except Exception as exc:
        print(f"context_watch: {exc}", file=sys.stderr)
        return 1


def import_jsonl_spool(python: str, module: str, env: dict[str, str], db: Path, spool: Path) -> int:
    stale_seconds = float(os.environ.get("HERMES_QUEUE_PROCESSING_STALE_SECONDS", queue_spool.DEFAULT_PROCESSING_STALE_SECONDS))
    result = import_jsonl_spool_helper(python, module, env, db, spool, run_cli, processing_stale_seconds=stale_seconds)
    return result.quarantined


def import_if_exists(
    python: str,
    module: str,
    env: dict[str, str],
    db: Path,
    source: str,
    path: Path,
    *,
    timeout_seconds: float | None = None,
) -> None:
    if path.exists():
        args = ["context-import", "--source", source, "--path", str(path), "--db", str(db)]
        if timeout_seconds is None:
            run_cli(python, module, env, args)
        else:
            run_cli(python, module, env, args, timeout_seconds=timeout_seconds)


def import_slack_live_if_exists(python: str, module: str, env: dict[str, str], db: Path, slack_dir: Path) -> None:
    gzip_live = slack_dir / "slack-live.jsonl.gz"
    legacy_live = slack_dir / "slack-live.jsonl"
    if gzip_live.exists():
        import_if_exists(python, module, env, db, "slack", gzip_live)
    elif legacy_live.exists():
        import_if_exists(python, module, env, db, "slack", legacy_live)


def run_cli(
    python: str,
    module: str,
    env: dict[str, str],
    args: list[str],
    *,
    timeout_seconds: float | None = None,
) -> None:
    result = subprocess.run(
        [python, "-m", module, *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
    if result.returncode != 0:
        if result.stderr:
            sys.stderr.write(result.stderr)
        raise RuntimeError(f"{' '.join(args)} failed with {result.returncode}")


def source_signature(path: Path) -> list[int]:
    if not path.exists():
        return []
    signature: list[int] = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            stat = candidate.stat()
        except FileNotFoundError:
            signature.extend((0, 0))
        else:
            signature.extend((stat.st_mtime_ns, stat.st_size))
    return signature


def should_import_artifact(path: Path, watch_state: dict[str, list[int]]) -> bool:
    """Return whether an immutable materialized artifact needs importing.

    A completed backfill is replayed only when it has not yet been accepted by
    this watcher or its on-disk signature changed. The state entry is written
    only after ``context-import`` succeeds, so a failed import is retried.
    """
    signature = source_signature(path)
    return bool(signature) and watch_state.get(str(path)) != signature


def record_artifact_signature(path_to_state: Path, watch_state: dict[str, list[int]], artifact: Path) -> None:
    """Persist the successfully imported artifact signature atomically."""
    signature = source_signature(artifact)
    if not signature:
        return
    watch_state[str(artifact)] = signature
    save_watch_state(path_to_state, watch_state)


def load_watch_state(path: Path) -> dict[str, list[int]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_watch_state(path: Path, value: dict[str, list[int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    fsync_dir(path.parent)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
