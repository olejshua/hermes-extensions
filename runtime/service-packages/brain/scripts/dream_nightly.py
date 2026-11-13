#!/usr/bin/env python3
"""Foreground Dream worker; generic deploy/sync follows only safe success."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_LOCAL_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def main(
    argv: list[str] | None = None,
    *,
    run=subprocess.run,
    now=None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path)
    parser.add_argument("--pid-token")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--until")
    parser.add_argument("--interval-minutes", type=float)
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument(
        "--manual-overnight",
        action="store_true",
        help="manual mode: a passed local cutoff may roll to the next day",
    )
    args = parser.parse_args(argv)
    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        while args.pid_file and args.pid_token and not stop_heartbeat.wait(30):
            try:
                data = json.loads(args.pid_file.read_text(encoding="utf-8"))
                if data.get("token") != args.pid_token or int(data.get("pid", 0)) != os.getpid():
                    return
                data["heartbeat"] = time.time()
                temp = args.pid_file.with_suffix(".heartbeat.tmp")
                temp.write_text(json.dumps(data), encoding="utf-8")
                os.chmod(temp, 0o600)
                os.replace(temp, args.pid_file)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return

    project = args.project_dir.absolute()
    try:
        manifest = json.loads(args.config.read_text(encoding="utf-8"))
        section = manifest.get("dreaming", {}) if isinstance(manifest, dict) else {}
        if not isinstance(section, dict):
            return 1
        configured_until = section.get("catchup_until_local", "06:30")
        configured_start = section.get("nightly_start_local", "03:00")
        timezone_name = section.get("timezone", "Europe/Berlin")
        configured_max_rounds = section.get("max_rounds", 64)
        if (
            not isinstance(configured_until, str)
            or not _LOCAL_TIME.fullmatch(configured_until)
            or not isinstance(configured_start, str)
            or not _LOCAL_TIME.fullmatch(configured_start)
            or timezone_name != "Europe/Berlin"
        ):
            return 1
        if (
            type(configured_max_rounds) is not int
            or not 1 <= configured_max_rounds <= 128
        ):
            return 1
        timezone = ZoneInfo("Europe/Berlin")
        current = now() if now is not None else dt.datetime.now(timezone)
        if not isinstance(current, dt.datetime) or current.tzinfo is None:
            return 1
        local_now = current.astimezone(timezone)
        if args.manual_overnight:
            effective_until = args.until or configured_until
            cutoff = _manual_cutoff(effective_until, local_now, timezone)
        else:
            # Scheduled mode is governed only by the configured same-day
            # window. CLI deadlines are a manual-mode feature and cannot
            # silently extend a delayed scheduled invocation.
            effective_until = configured_until
            window_start = _today_at(configured_start, local_now, timezone)
            cutoff = _today_at(effective_until, local_now, timezone)
            if cutoff <= window_start:
                return 1
            if not window_start <= local_now < cutoff:
                _release_owned_pid(args.pid_file, args.pid_token)
                return 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError, ZoneInfoNotFoundError):
        return 1
    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    env = {**os.environ, "PROJECT_DIR": str(project), "DREAM_CONFIG": str(args.config),
           "PYTHONPATH": str(project / "src")}
    command = [str(project / "scripts" / "dream_sweep.sh"), "--json"]
    if args.full:
        command.append("--full")
    effective_max_rounds = (
        args.max_rounds if args.max_rounds is not None else configured_max_rounds
    )
    for flag, value in (("--until", cutoff.isoformat()), ("--interval-minutes", args.interval_minutes), ("--max-rounds", effective_max_rounds)):
        if value is not None:
            command.extend([flag, str(value)])
    try:
        # stdout is one bounded CLI JSON outcome; stderr streams directly to
        # the launcher's private log instead of accumulating for a long run.
        result = run(command, cwd=project, env=env, text=True,
                     stdout=subprocess.PIPE, stderr=None, check=False)
        if result.returncode != 0:
            return result.returncode or 1
        try:
            outcome = json.loads((result.stdout or "").strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return 1
        print(f"dream_health={outcome.get('health', 'unknown')}", file=sys.stderr, flush=True)
        if outcome.get("status") != "complete":
            return 0 if outcome.get("status") == "already_running" else 1
        sync_command = [str(project / "scripts" / "deploy_check.sh")]
        if section.get("sync_scope") == "dreams_only":
            current_sync = now() if now is not None else dt.datetime.now(timezone)
            remaining_sync = max(0, (cutoff - current_sync).total_seconds())
            if remaining_sync <= 0:
                return 0
            sync_command = [sys.executable, str(project / "scripts" / "dream_sync.py"), "--config", str(args.config),
                            "--budget-seconds", str(min(120, remaining_sync))]
        synced = run(
            sync_command,
            cwd=project,
            env=env,
            check=False,
        )
        if int(synced.returncode) == 0:
            # The acknowledgement is a separate idempotent metadata step. If
            # it fails, locally_published rows remain visible and retryable.
            run(
                [
                    sys.executable,
                    str(project / "scripts" / "dream_publication_ack.py"),
                    "--config",
                    str(args.config),
                ],
                cwd=project,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=None,
                check=False,
            )
            # Same native no-agent/local job; no second cron delivery. The
            # optional fixed-destination bridge defaults off. Native gateway
            # Python/bootstrap is reused from the reviewed model command,
            # never imported into the older Brain interpreter.
            delivery = section.get("reflection_delivery", {})
            if isinstance(delivery, dict) and delivery.get("enabled") is True:
                model_command = section.get("model", {}).get("command", [])
                adapter = str(project / "scripts" / "dream_native_adapter.py")
                script = project / "scripts" / "dream_reflection_send.py"
                if (not isinstance(model_command, list) or adapter not in model_command
                        or not script.is_file() or script.is_symlink()):
                    return 1
                position = model_command.index(adapter)
                if position != 4 or model_command[1:3] != ["-I", "-c"]:
                    return 1
                run([*model_command[:position], str(script), "--config", str(args.config)],
                    cwd=project, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=None, check=False, timeout=45)
        # Dream publication is an independent local outbox concern. A temporary
        # OpenViking/deploy outage leaves the committed round and its retryable
        # queue intact; morning health reporting exposes that queue.
        return 0
    finally:
        stop_heartbeat.set()
        if args.pid_file:
            try:
                data = json.loads(args.pid_file.read_text(encoding="utf-8"))
                if (int(data.get("pid", 0)) == os.getpid()
                        and data.get("token") == args.pid_token):
                    args.pid_file.unlink()
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass


def _today_at(value: str, local_now: dt.datetime, timezone: ZoneInfo) -> dt.datetime:
    hour, minute = (int(part) for part in value.split(":"))
    return dt.datetime(
        local_now.year, local_now.month, local_now.day, hour, minute, tzinfo=timezone
    )


def _manual_cutoff(
    value: str, local_now: dt.datetime, timezone: ZoneInfo
) -> dt.datetime:
    if _LOCAL_TIME.fullmatch(value):
        cutoff = _today_at(value, local_now, timezone)
        if cutoff <= local_now:
            tomorrow = local_now.date() + dt.timedelta(days=1)
            hour, minute = (int(part) for part in value.split(":"))
            cutoff = dt.datetime.combine(
                tomorrow, dt.time(hour, minute), tzinfo=timezone
            )
        return cutoff
    parsed = dt.datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed.astimezone(timezone)


def _release_owned_pid(path: Path | None, token: str | None) -> None:
    if path is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("token") == token:
            path.unlink()
    except (OSError, TypeError, json.JSONDecodeError):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
