from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Callable, Sequence

from .context_inbox import normalize_timestamp


DEFAULT_PROCESSING_STALE_SECONDS = 15 * 60


@dataclass(frozen=True)
class ValidationError:
    category: str
    line_number: int
    error_type: str


@dataclass(frozen=True)
class SpoolImportResult:
    imported: int = 0
    quarantined: int = 0


RunImport = Callable[[str, str, dict[str, str], list[str]], None]


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing symlinked {label}: {path}")


def import_jsonl_spool(
    python: str,
    module: str,
    env: dict[str, str],
    db: Path | None,
    spool: Path,
    run_import: RunImport,
    *,
    manifest: Path | None = None,
    processing_stale_seconds: float = DEFAULT_PROCESSING_STALE_SECONDS,
) -> SpoolImportResult:
    queue = spool.with_suffix(spool.suffix + ".d")
    imported = 0
    quarantined = 0

    _reject_symlink(spool, "spool")
    _reject_symlink(queue, "spool queue")

    if queue.is_dir():
        recover_processing(queue, stale_after_seconds=processing_stale_seconds)
        for entry in sorted(queue.glob("*.jsonl")):
            _reject_symlink(entry, "queued event")
            outcome = process_queue_file(python, module, env, db, entry, run_import, manifest=manifest)
            imported += outcome.imported
            quarantined += outcome.quarantined

    for pending in sorted(spool.parent.glob(f"{spool.name}.processing.*")):
        _reject_symlink(pending, "legacy processing file")
        if not processing_file_is_recoverable(pending, stale_after_seconds=processing_stale_seconds):
            continue
        outcome = process_legacy_processing_file(python, module, env, db, pending, spool, run_import, manifest=manifest)
        imported += outcome.imported
        quarantined += outcome.quarantined

    if spool.exists() and spool.stat().st_size > 0:
        processing = spool.with_name(f"{spool.name}.processing.{os.getpid()}")
        spool.rename(processing)
        spool.touch(mode=0o600, exist_ok=True)
        try:
            os.chmod(spool, 0o600)
        except OSError:
            pass
        fsync_dir_if_possible(spool.parent)
        outcome = process_legacy_processing_file(python, module, env, db, processing, spool, run_import, manifest=manifest)
        imported += outcome.imported
        quarantined += outcome.quarantined

    return SpoolImportResult(imported=imported, quarantined=quarantined)


def recover_processing(
    queue: Path,
    *,
    now: float | None = None,
    stale_after_seconds: float = DEFAULT_PROCESSING_STALE_SECONDS,
) -> None:
    _reject_symlink(queue, "spool queue")
    checked_at = time() if now is None else now
    for pending in sorted(queue.glob("*.processing.*")):
        _reject_symlink(pending, "processing file")
        if not processing_file_is_recoverable(pending, now=checked_at, stale_after_seconds=stale_after_seconds):
            continue
        retry = pending.with_name(pending.name.split(".processing.", 1)[0])
        try:
            pending.replace(retry)
            fsync_dir_if_possible(queue)
        except FileNotFoundError:
            pass


def processing_file_is_recoverable(
    path: Path,
    *,
    now: float | None = None,
    stale_after_seconds: float = DEFAULT_PROCESSING_STALE_SECONDS,
) -> bool:
    pid = processing_owner_pid(path)
    if pid is not None:
        return not pid_is_live(pid)
    checked_at = time() if now is None else now
    try:
        age = checked_at - path.stat().st_mtime
    except FileNotFoundError:
        return False
    return age >= stale_after_seconds


def processing_owner_pid(path: Path) -> int | None:
    marker = ".processing."
    if marker not in path.name:
        return None
    suffix = path.name.rsplit(marker, 1)[1]
    if not suffix.isdecimal():
        return None
    try:
        pid = int(suffix)
    except ValueError:
        return None
    return pid if pid > 0 else None


def pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_queue_file(
    python: str,
    module: str,
    env: dict[str, str],
    db: Path | None,
    entry: Path,
    run_import: RunImport,
    *,
    manifest: Path | None = None,
) -> SpoolImportResult:
    _reject_symlink(entry, "queued event")
    processing = entry.with_name(f"{entry.name}.processing.{os.getpid()}")
    try:
        entry.replace(processing)
        fsync_dir_if_possible(entry.parent)
    except FileNotFoundError:
        return SpoolImportResult()
    try:
        return process_processing_file(python, module, env, db, processing, entry, run_import, manifest=manifest)
    except Exception:
        try:
            processing.replace(entry)
            fsync_dir_if_possible(entry.parent)
        except FileNotFoundError:
            pass
        raise


def process_processing_file(
    python: str,
    module: str,
    env: dict[str, str],
    db: Path | None,
    processing: Path,
    original: Path,
    run_import: RunImport,
    *,
    manifest: Path | None = None,
) -> SpoolImportResult:
    validation_error = validate_canonical_jsonl(processing)
    if validation_error is not None:
        quarantine_file(processing, original, validation_error)
        return SpoolImportResult(quarantined=1)

    args = ["context-import", "--source", "jsonl", "--path", str(processing)]
    if manifest is not None:
        args.extend(["--manifest", str(manifest)])
    elif db is not None:
        args.extend(["--db", str(db)])
    run_import(python, module, env, args)
    processing.unlink(missing_ok=True)
    fsync_dir_if_possible(processing.parent)
    return SpoolImportResult(imported=1)


def process_legacy_processing_file(
    python: str,
    module: str,
    env: dict[str, str],
    db: Path | None,
    processing: Path,
    original: Path,
    run_import: RunImport,
    *,
    manifest: Path | None = None,
) -> SpoolImportResult:
    try:
        return process_processing_file(python, module, env, db, processing, original, run_import, manifest=manifest)
    except Exception:
        if not original.exists() or original.stat().st_size == 0:
            try:
                processing.replace(original)
                fsync_dir_if_possible(original.parent)
            except FileNotFoundError:
                pass
        raise


def validate_canonical_jsonl(path: Path) -> ValidationError | None:
    _reject_symlink(path, "queue payload")
    with path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                return ValidationError("malformed_json", line_number, type(exc).__name__)
            if not isinstance(value, dict):
                return ValidationError("non_object", line_number, type(value).__name__)
            timestamp = value.get("message_ts") or value.get("timestamp") or value.get("ts")
            if not normalize_timestamp(timestamp):
                return ValidationError("invalid_timestamp", line_number, type(timestamp).__name__)
    return None


def quarantine_file(path: Path, original: Path, error: ValidationError) -> Path:
    queue = original.parent if original.parent.name.endswith(".d") else original.with_suffix(original.suffix + ".d")
    quarantine = queue / "quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    os.chmod(quarantine, 0o700)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = quarantine / f"{original.name}.{timestamp}.jsonl"
    sidecar = target.with_suffix(target.suffix + ".error.json")
    path.replace(target)
    os.chmod(target, 0o600)
    fsync_file_if_possible(target)
    sidecar_payload = {
        "filename": original.name,
        "category": error.category,
        "line_number": error.line_number,
        "error_type": error.error_type,
        "timestamp": timestamp,
    }
    try:
        write_private_json(sidecar, sidecar_payload)
    except Exception as exc:
        print(f"context queue quarantine sidecar failed: {type(exc).__name__}", file=sys.stderr)
    else:
        fsync_file_if_possible(sidecar)
    fsync_dir_if_possible(quarantine)
    fsync_dir_if_possible(path.parent)
    return target


def write_private_json(path: Path, payload: dict[str, object]) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp.unlink(missing_ok=True)
        raise


def fsync_file_if_possible(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir_if_possible(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def subprocess_run_import(python: str, module: str, env: dict[str, str], args: list[str]) -> None:
    result = subprocess.run([python, "-m", module, *args], env=env, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        if result.stderr:
            sys.stderr.write(result.stderr)
        raise RuntimeError(f"{' '.join(args)} failed with {result.returncode}")


def cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hermes_second_brain.queue_spool")
    parser.add_argument("--spool", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--module", default="hermes_second_brain")
    parser.add_argument("--processing-stale-seconds", type=float, default=DEFAULT_PROCESSING_STALE_SECONDS)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--db", type=Path)
    group.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        result = import_jsonl_spool(
            args.python,
            args.module,
            os.environ.copy(),
            args.db,
            args.spool.expanduser(),
            subprocess_run_import,
            manifest=args.manifest,
            processing_stale_seconds=args.processing_stale_seconds,
        )
    except Exception as exc:
        print(f"context queue import failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if result.quarantined:
        print(f"context queue quarantined {result.quarantined} invalid JSONL file(s)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
