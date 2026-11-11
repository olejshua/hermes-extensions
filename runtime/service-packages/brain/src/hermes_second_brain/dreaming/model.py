"""Structured model adapter backed by the Claude subscription CLI.

The adapter never uses a shell, never passes prompts as argv (they are far too
large and would leak into the process table), and never requires an API key: the
configured command is a wrapper that forces subscription OAuth. Output must
arrive as a valid ``structured_output`` object matching the phase schema; we
re-validate locally and reject anything unexpected rather than trusting the
model's own validation.
"""

from __future__ import annotations

import json
import logging
import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import ModelConfig
from .schemas import SchemaError, validate

LOG = logging.getLogger(__name__)

# Failures that mean "the model refused or the request was not allowed". These
# are never retried: retrying a policy decision just burns the user's quota.
_NON_RETRYABLE_MARKERS = (
    "not allowed",
    "not permitted",
    "permission denied",
    "policy",
    "refused",
    "unauthor",
    "forbidden",
    "invalid api key",
    "authentication",
    "credit balance",
    "quota",
    "trust",
    "allowlist",
    "allow list",
)


class ModelError(RuntimeError):
    """A model call failed with independent retry and provider-failover signals."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        fallback_eligible: bool | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.fallback_eligible = retryable if fallback_eligible is None else fallback_eligible


@dataclass(frozen=True)
class ModelResult:
    data: dict[str, Any]
    duration_seconds: float
    attempts: int


class ModelAdapter:
    def __init__(self, config: ModelConfig, *, fallbacks: tuple[ModelConfig, ...] = ()):
        self.config = config
        self.fallbacks = fallbacks

    def build_argv(self, *, phase: str, schema: dict[str, Any]) -> list[str]:
        cfg = self.config
        argv = [*cfg.command]
        argv.extend(["-p", "--output-format", "json"])
        argv.extend(["--json-schema", json.dumps(schema, sort_keys=True, separators=(",", ":"))])
        argv.extend(["--model", cfg.model, "--effort", cfg.effort])
        if cfg.safe_mode:
            argv.append("--safe-mode")
        if not cfg.session_persistence:
            argv.append("--no-session-persistence")
        if not cfg.allow_tools:
            # An empty allowlist leaves the model with no way to touch the
            # filesystem, network, or any external service.
            argv.extend(["--tools", ""])
        return argv

    def run(
        self,
        *,
        phase: str,
        prompt: str,
        schema: dict[str, Any],
        deadline_monotonic: float | None = None,
    ) -> ModelResult:
        """Try the primary then approved secondary providers on operational failure."""
        last_error: ModelError | None = None
        for config in (self.config, *self.fallbacks):
            try:
                return ModelAdapter(config)._run_single(
                    phase=phase,
                    prompt=prompt,
                    schema=schema,
                    deadline_monotonic=deadline_monotonic,
                )
            except ModelError as exc:
                last_error = exc
                if not exc.fallback_eligible:
                    raise
        raise last_error or ModelError("model call failed with no diagnostic")

    def _run_single(
        self,
        *,
        phase: str,
        prompt: str,
        schema: dict[str, Any],
        deadline_monotonic: float | None = None,
    ) -> ModelResult:
        argv = self.build_argv(phase=phase, schema=schema)
        timeout = self.config.timeout_for(phase)
        last_error: ModelError | None = None
        started = time.monotonic()
        for attempt in range(1, self.config.retry_attempts + 1):
            try:
                remaining = _remaining(deadline_monotonic)
                if remaining is not None:
                    if remaining <= 0:
                        raise ModelError("global deadline reached before model phase", retryable=False)
                    timeout = min(timeout, remaining)
                raw = self._invoke(argv, prompt, timeout)
                data = self._parse(raw)
                validate(data, schema)
                return ModelResult(data=data, duration_seconds=time.monotonic() - started, attempts=attempt)
            except ModelError as exc:
                last_error = exc
                # Never log the prompt or the raw packet: both contain the
                # user's conversations.
                LOG.warning(
                    "dreaming model call failed",
                    extra={"fields": {"phase": phase, "attempt": attempt, "retryable": exc.retryable, "error": str(exc)[:300]}},
                )
                if not exc.retryable or attempt >= self.config.retry_attempts:
                    break
                self._backoff(attempt, deadline_monotonic)
            except SchemaError as exc:
                # Schema violations are the model's fault but are technically
                # retryable: a second sample often complies.
                last_error = ModelError("schema validation failed", retryable=True)
                LOG.warning(
                    "dreaming model output rejected",
                    extra={"fields": {"phase": phase, "attempt": attempt, "error": "schema validation failed"}},
                )
                if attempt >= self.config.retry_attempts:
                    break
                self._backoff(attempt, deadline_monotonic)
        raise last_error or ModelError("model call failed with no diagnostic")

    def _backoff(self, attempt: int, deadline_monotonic: float | None) -> None:
        delay = self.config.retry_backoff_seconds * attempt
        remaining = _remaining(deadline_monotonic)
        if remaining is not None:
            delay = min(delay, max(0.0, remaining))
        if delay > 0:
            time.sleep(delay)

    def _invoke(self, argv: list[str], prompt: str, timeout: float) -> str:
        """Run the model in an isolated process group with streaming bounds.

        ``subprocess.run(capture_output=True)`` buffers unbounded output before
        the caller can inspect its size.  This loop drains both pipes as bytes,
        enforces the cap while the process is alive, and kills the whole group
        (including descendants) on timeout or overflow.
        """

        try:
            proc = subprocess.Popen(
                argv,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
        except FileNotFoundError as exc:
            raise ModelError(f"model command not found: {argv[0]}", retryable=False) from exc
        except OSError as exc:
            raise ModelError(f"model command failed to start: {exc}", retryable=True) from exc

        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        # start_new_session makes the leader the process-group leader, but keep
        # the id independently: the leader can exit while a descendant retains
        # our pipes and ignores SIGTERM.
        pgid = proc.pid
        write_error: list[BaseException] = []

        def write_prompt() -> None:
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                write_error.append(exc)

        writer = threading.Thread(target=write_prompt, name="dream-model-stdin", daemon=True)
        writer.start()
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        limit = self.config.max_output_bytes
        deadline = time.monotonic() + max(0.001, timeout)
        failure: ModelError | None = None
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = ModelError(f"model timed out after {timeout:.3f}s", retryable=True)
                    break
                events = selector.select(min(0.1, remaining))
                if not events and proc.poll() is not None:
                    # A descendant inherited one of our pipes.  The leader is
                    # already reaped, so waiting for it tells us nothing about
                    # whether the process group is still alive.
                    events = selector.select(0)
                    if not events:
                        if _group_exists(pgid):
                            _terminate_group(proc, pgid)
                        break
                for key, _ in events:
                    stream = key.fileobj
                    chunk = os.read(stream.fileno(), 65_536)
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    bucket = buffers[str(key.data)]
                    bucket.extend(chunk)
                    if len(bucket) > limit:
                        failure = ModelError(
                            f"model {key.data} exceeded {limit} bytes", retryable=False
                        )
                        break
                if failure is not None:
                    break
                if proc.poll() is not None and _group_exists(pgid):
                    # We have drained the leader's currently available bytes;
                    # do not let a chatty inherited pipe postpone cleanup until
                    # the output cap or timeout.
                    _terminate_group(proc, pgid)
            if failure is not None:
                _terminate_group(proc, pgid)
                raise failure
            try:
                returncode = proc.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                _terminate_group(proc, pgid)
                raise ModelError(f"model timed out after {timeout:.3f}s", retryable=True) from exc
        finally:
            selector.close()
            if proc.poll() is None or _group_exists(pgid):
                _terminate_group(proc, pgid)
            try:
                proc.stdin.close()
            except OSError:
                pass
            writer.join(timeout=0.2)
            proc.stdout.close()
            proc.stderr.close()

        stdout = bytes(buffers["stdout"]).decode("utf-8", "replace")
        stderr = bytes(buffers["stderr"]).decode("utf-8", "replace")
        if write_error and returncode == 0:
            raise ModelError("model prompt stream failed", retryable=True)
        if returncode != 0:
            message = (stderr or stdout or f"model exited {returncode}").strip()
            retryable = _is_retryable(message)
            safe_message = "model process failed technically" if retryable else "model request rejected by policy, trust, authentication, or allowlist"
            raise ModelError(
                safe_message,
                retryable=retryable,
                fallback_eligible=retryable or _is_auth_or_quota_failure(message),
            )

        return stdout

    def _parse(self, raw: str) -> dict[str, Any]:
        text = raw.strip()
        if not text:
            raise ModelError("model returned no output", retryable=True)
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelError(f"model output was not valid JSON: {exc.msg}", retryable=True) from exc
        if not isinstance(envelope, dict):
            raise ModelError("model envelope was not a JSON object", retryable=True)

        if envelope.get("is_error"):
            message = str(envelope.get("result") or envelope.get("error") or "model reported an error")
            raise ModelError(_summarize(message), retryable=_is_retryable(message))
        subtype = str(envelope.get("subtype") or "")
        if subtype and subtype != "success":
            message = str(envelope.get("result") or subtype)
            raise ModelError(_summarize(f"model subtype {subtype}: {message}"), retryable=_is_retryable(message))

        structured = envelope.get("structured_output")
        if structured is None:
            raise ModelError("model envelope had no structured_output", retryable=True)
        if not isinstance(structured, dict):
            raise ModelError("structured_output was not a JSON object", retryable=True)
        return structured


def _is_retryable(message: str) -> bool:
    lowered = message.lower()
    return not any(marker in lowered for marker in _NON_RETRYABLE_MARKERS)


def _is_auth_or_quota_failure(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in (
        "unauthor",
        "authentication",
        "invalid api key",
        "credit balance",
        "quota",
        "usage limit",
        "rate limit",
    ))


def _summarize(message: str) -> str:
    """Map model diagnostics to a content-free category."""

    if not _is_retryable(message):
        return "model request rejected by policy, trust, authentication, or allowlist"
    return "model reported a technical failure"


def _remaining(deadline_monotonic: float | None) -> float | None:
    return None if deadline_monotonic is None else deadline_monotonic - time.monotonic()


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_group(proc: subprocess.Popen[bytes], pgid: int | None = None) -> None:
    """Terminate, then kill, the process group without leaking descendants."""

    group = proc.pid if pgid is None else pgid
    try:
        os.killpg(group, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    grace_deadline = time.monotonic() + 0.5
    while _group_exists(group) and time.monotonic() < grace_deadline:
        # Reap the leader when it is ours, but never confuse that with the
        # independently tracked group having exited.
        try:
            proc.wait(timeout=0.02)
        except subprocess.TimeoutExpired:
            pass
        if _group_exists(group):
            time.sleep(0.01)
    if _group_exists(group):
        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
