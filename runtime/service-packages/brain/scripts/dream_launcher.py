#!/usr/bin/env python3
"""Fast, quiet detached launcher for Hermes' 120-second no-agent trigger."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _reject_unsafe_ancestors(path: Path) -> None:
    """Reject any existing ancestor that is a symlink or not a directory.

    Every path the launcher spawns from or writes to goes through here before
    ``Popen``. Checking only the leaf is not enough: a symlinked *ancestor* lets
    an attacker who controls that link redirect the worker, the config, or the
    private state directory without ever touching the final component.
    """

    for ancestor in reversed(path.absolute().parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("unsafe launcher path")


def _private_dir(path: Path) -> None:
    _reject_unsafe_ancestors(path)
    try:
        info = path.absolute().lstat()
    except FileNotFoundError:
        info = None
    if info is not None and (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)):
        raise RuntimeError("unsafe launcher path")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _read_pid(path: Path) -> tuple[int, str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (int(data.get("pid", 0)), str(data.get("token") or ""),
                float(data.get("heartbeat", data.get("at", 0))))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0, "", 0.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--until")
    parser.add_argument("--interval-minutes", type=float)
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument("--manual-overnight", action="store_true")
    parser.add_argument("--project-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state-dir", type=Path)
    return parser


def main(argv: list[str] | None = None, *, popen=subprocess.Popen) -> int:
    args = build_parser().parse_args(argv)
    project = args.project_dir.absolute()
    worker = project / "scripts" / "dream_nightly.py"
    sweep = project / "scripts" / "dream_sweep.sh"
    deploy = project / "scripts" / "deploy_check.sh"
    config = (args.config or project / "config" / "manifest.json").absolute()
    required = (worker, sweep, deploy, config)
    state_dir = (args.state_dir or project / ".dream-runtime").absolute()
    pid_path = state_dir / "nightly.pid.json"
    log_path = state_dir / "nightly.log"
    try:
        for candidate in (project, *required, state_dir, pid_path, log_path):
            _reject_unsafe_ancestors(candidate)
    except (OSError, RuntimeError):
        return 1
    if not project.is_dir() or project.is_symlink() or any(
        not path.is_file() or path.is_symlink() for path in required
    ):
        return 1
    try:
        _private_dir(state_dir)
    except (OSError, RuntimeError):
        return 1
    if pid_path.is_symlink():
        return 1
    if pid_path.exists():
        old_pid, old_token, heartbeat = _read_pid(pid_path)
        if old_token and _alive(old_pid) and heartbeat >= time.time() - 120:
            return 0
        try:
            pid_path.unlink()
        except OSError:
            return 1
    child = None
    token = uuid.uuid4().hex
    try:
        fd = os.open(pid_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return 0
    except OSError:
        return 1
    try:
        stamp = time.time()
        os.write(fd, json.dumps({"pid": os.getpid(), "token": token,
                                 "state": "spawning", "at": stamp,
                                 "heartbeat": stamp}).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    command = [sys.executable, str(worker), "--project-dir", str(project), "--config", str(config),
               "--pid-file", str(pid_path), "--pid-token", token]
    if args.full:
        command.append("--full")
    if args.until is not None:
        command.extend(["--until", args.until])
    if args.interval_minutes is not None:
        command.extend(["--interval-minutes", str(args.interval_minutes)])
    if args.max_rounds is not None:
        command.extend(["--max-rounds", str(args.max_rounds)])
    if args.manual_overnight:
        command.append("--manual-overnight")
    log_path = state_dir / "nightly.log"
    if log_path.is_symlink() or (log_path.exists() and not log_path.is_file()):
        try:
            pid_path.unlink()
        except OSError:
            pass
        return 1
    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | nofollow, 0o600)
        with os.fdopen(log_fd, "ab", closefd=True) as log:
            child = popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                          start_new_session=True, close_fds=True,
                          cwd=project, env={**os.environ, "PYTHONPATH": str(project / "src")})
        temp = pid_path.with_suffix(".tmp")
        temp_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        try:
            os.write(temp_fd, json.dumps({"pid": int(child.pid), "token": token,
                                          "state": "running", "at": time.time(),
                                          "heartbeat": time.time()}).encode())
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)
        os.replace(temp, pid_path)
    except (OSError, ValueError):
        if child is not None:
            try:
                os.killpg(int(child.pid), 15)
            except (OSError, ValueError):
                pass
        try:
            pid_path.unlink()
        except OSError:
            pass
        try:
            temp.unlink()
        except (NameError, OSError):
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
