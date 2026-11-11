"""Safe cross-vendor review of aggregate Process Observatory metadata.

Both reviewers are local subscription-backed command wrappers supplied as
argv.  Prompts travel over stdin, a shell is never used, output is bounded
while the process is alive, timeouts clean up the complete process group, and
no proposal is persisted until both outputs have passed deterministic local
validation.  The second reviewer may accept, reject, or lower risk; it can
never raise risk or approve application.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .improvement import (
    MAX_BATCH,
    RISK_CLASSES,
    ImprovementError,
    ImprovementStore,
)


MAX_INPUT_BYTES = 64_000
MAX_OUTPUT_BYTES = 256_000
MAX_TIMEOUT_SECONDS = 900.0
MAX_ANOMALY_ROWS = 20
_CLASS_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_RISK_RANK = {risk: index for index, risk in enumerate(RISK_CLASSES)}

PRIMARY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["proposals"],
    "properties": {
        "proposals": {
            "type": "array",
            "maxItems": MAX_BATCH,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title", "risk_class", "target_kind", "proposed_intervention",
                    "expected_metric", "observation_count", "evidence_count",
                    "counterevidence_count", "rollback_note",
                ],
                "properties": {
                    "title": {"type": "string", "maxLength": 180},
                    "risk_class": {"type": "string", "enum": list(RISK_CLASSES)},
                    "target_kind": {
                        "type": "string", "enum": ["skill", "config", "test", "workflow"]
                    },
                    "proposed_intervention": {"type": "string", "maxLength": 1200},
                    # Anchored to a machine-safe token (alphanumeric plus ._:-,
                    # no spaces) so generated values always satisfy the durable
                    # ImprovementStore._validate_proposal / _bounded_token
                    # contract.  Keep this stricter-or-equal to that validator.
                    "expected_metric": {
                        "type": "string",
                        "maxLength": 96,
                        "pattern": "^[A-Za-z0-9._:-]+$",
                    },
                    "observation_count": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
                    "evidence_count": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
                    "counterevidence_count": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
                    "rollback_note": {"type": "string", "maxLength": 600},
                },
            },
        }
    },
}

SECONDARY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "maxItems": MAX_BATCH,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "decision"],
                "properties": {
                    "index": {"type": "integer", "minimum": 0},
                    "decision": {
                        "type": "string", "enum": ["accept", "downgrade", "reject"]
                    },
                    "risk_class": {"type": "string", "enum": list(RISK_CLASSES)},
                },
            },
        }
    },
}


class ReviewError(ValueError):
    """A command, report, or reviewer response failed closed."""


@dataclass(frozen=True)
class ReviewerCommand:
    argv: Sequence[str]
    timeout_seconds: float = 300.0
    max_output_bytes: int = MAX_OUTPUT_BYTES

    def __post_init__(self) -> None:
        argv = tuple(self.argv)
        if not argv or len(argv) > 64:
            raise ReviewError("reviewer argv must contain 1 to 64 items")
        for item in argv:
            if not isinstance(item, str) or not item or "\0" in item or len(item) > 4096:
                raise ReviewError("reviewer argv contains an invalid item")
        timeout = self.timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ReviewError("reviewer timeout must be numeric")
        if not 0.1 <= float(timeout) <= MAX_TIMEOUT_SECONDS:
            raise ReviewError(
                f"reviewer timeout must be between 0.1 and {MAX_TIMEOUT_SECONDS:g} seconds"
            )
        if type(self.max_output_bytes) is not int or not 1024 <= self.max_output_bytes <= 2_000_000:
            raise ReviewError("reviewer max_output_bytes is out of bounds")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "timeout_seconds", float(timeout))


def commands_from_manifest(path: Path | str) -> tuple[ReviewerCommand, ReviewerCommand]:
    """Load configurable reviewer argv with subscription-only safe defaults."""

    manifest = Path(path).expanduser()
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 2_000_000:
        raise ReviewError("review manifest is not a safe bounded file")
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewError("review manifest is invalid") from exc
    if not isinstance(raw, dict):
        raise ReviewError("review manifest must be an object")
    b_plus = raw.get("b_plus", {})
    if not isinstance(b_plus, dict):
        raise ReviewError("b_plus configuration must be an object")
    review = b_plus.get("review", {})
    if not isinstance(review, dict):
        raise ReviewError("b_plus.review must be an object")
    allowed = {
        "primary_argv", "primary_timeout_seconds",
        "secondary_timeout_seconds", "primary_max_output_bytes",
        "secondary_max_output_bytes", "claude_wrapper", "codex_cli",
        "codex_auth_file", "sandbox_exec", "primary_model", "primary_effort",
    }
    if set(review) - allowed:
        raise ReviewError("b_plus.review contains unknown options")

    project = Path(__file__).resolve().parents[2]
    scripts = project / "scripts"
    primary_argv = review.get("primary_argv")
    if primary_argv is None:
        primary_argv = [
            sys.executable,
            str(scripts / "claude_improvement_reviewer.py"),
            "--wrapper",
            str(review.get("claude_wrapper", "claude-subscription")),
            "--model",
            str(review.get("primary_model", "sonnet")),
            "--effort",
            str(review.get("primary_effort", "high")),
        ]
    secondary_argv = [
        sys.executable,
        str(scripts / "hermes_coder_review_adapter.py"),
        "--runner",
        str(review.get("codex_cli", "/opt/homebrew/bin/codex")),
        "--auth-file",
        str(review.get("codex_auth_file", "~/.codex/auth.json")),
        "--sandbox-exec",
        str(review.get("sandbox_exec", "/usr/bin/sandbox-exec")),
    ]
    if isinstance(primary_argv, str) or not isinstance(primary_argv, list):
        raise ReviewError("primary_argv must be an argv list")
    return (
        ReviewerCommand(
            argv=primary_argv,
            timeout_seconds=review.get("primary_timeout_seconds", 420),
            max_output_bytes=review.get("primary_max_output_bytes", MAX_OUTPUT_BYTES),
        ),
        ReviewerCommand(
            argv=secondary_argv,
            timeout_seconds=review.get("secondary_timeout_seconds", 420),
            max_output_bytes=review.get("secondary_max_output_bytes", MAX_OUTPUT_BYTES),
        ),
    )


def run_review(
    report: Mapping[str, Any],
    *,
    store: ImprovementStore,
    primary: ReviewerCommand,
    secondary: ReviewerCommand,
    now: str | None = None,
) -> dict[str, Any]:
    """Review one aggregate report and persist only accepted proposals.

    Failures intentionally return a bounded classification rather than raw
    subprocess diagnostics, which could contain echoed model input.  Since the
    store is touched only after both reviewers validate, failures are safe to
    retry and cannot leave a half-reviewed batch.
    """

    try:
        clean_report = sanitize_aggregate_report(report)
    except ReviewError:
        return {"status": "error", "stage": "report", "error_class": "invalid_schema", "persisted": 0}
    if clean_report["total"] == 0:
        return {"status": "noop", "persisted": 0, "reviewed": 0}

    primary_packet = {
        "schema_version": 1,
        "mode": "aggregate_metadata_proposals_only",
        "constraints": {
            "no_actions": True,
            "requires_human_approval": True,
            "allowed_targets": ["skill", "config", "test", "workflow"],
            "allowed_risk": list(RISK_CLASSES),
            "max_proposals": MAX_BATCH,
        },
        "report": clean_report,
    }
    try:
        primary_raw = _invoke(primary, primary_packet)
        primary_data = _parse_object(primary_raw)
        proposals = _validate_primary(primary_data)
    except ReviewError:
        return {"status": "error", "stage": "primary", "error_class": "reviewer_failure", "persisted": 0}

    if not proposals:
        return {"status": "noop", "persisted": 0, "reviewed": 0}

    secondary_packet = {
        "schema_version": 1,
        "mode": "independent_read_only_review",
        "constraints": {
            "may_upgrade_risk": False,
            "may_approve_application": False,
            "allowed_decisions": ["accept", "downgrade", "reject"],
        },
        "report": clean_report,
        "proposals": proposals,
    }
    try:
        secondary_raw = _invoke(secondary, secondary_packet)
        secondary_data = _parse_object(secondary_raw)
        selected = _apply_secondary(proposals, secondary_data)
    except ReviewError:
        return {"status": "error", "stage": "secondary", "error_class": "reviewer_failure", "persisted": 0}

    if not selected:
        return {"status": "ok", "persisted": 0, "reviewed": len(proposals)}

    fingerprint = hashlib.sha256(
        json.dumps(
            {"report": clean_report, "proposals": selected},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:32]
    try:
        stored = store.persist_reviewed_batch(
            selected,
            review_fingerprint=fingerprint,
            now=now,
        )
    except (ImprovementError, OSError):
        return {"status": "error", "stage": "persist", "error_class": "storage_failure", "persisted": 0}
    return {
        "status": "ok",
        "persisted": len(stored),
        "created": sum(1 for item in stored if item["created"]),
        "reviewed": len(proposals),
    }


def sanitize_aggregate_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Project a report onto a small, content-free aggregate schema."""

    if not isinstance(report, Mapping) or any(not isinstance(key, str) for key in report):
        raise ReviewError("report must be an object")
    allowed = {
        "total",
        "small_sample",
        "sample_size",
        "by_kind",
        "by_status",
        "error_rate",
        "retry_rate",
        "stall_rate",
        "unverified_completion_rate",
        "duration_ms",
        "top_anomalous_tool_families",
        "top_anomalous_task_classes",
        "note",
    }
    if set(report) - allowed:
        raise ReviewError("report contains non-aggregate fields")
    total = _bounded_integer(report.get("total", 0), "total", 0, 10_000_000)
    clean: dict[str, Any] = {
        "total": total,
        "small_sample": _optional_bool(report.get("small_sample"), total < 20),
        "sample_size": _bounded_integer(report.get("sample_size", total), "sample_size", 0, 10_000_000),
        "error_rate": _rate(report.get("error_rate", 0.0), "error_rate"),
        "retry_rate": _rate(report.get("retry_rate", 0.0), "retry_rate"),
        "stall_rate": _rate(report.get("stall_rate", 0.0), "stall_rate"),
        "unverified_completion_rate": _rate(
            report.get("unverified_completion_rate", 0.0),
            "unverified_completion_rate",
        ),
        "duration_ms": _duration_summary(report.get("duration_ms", {})),
        "by_kind": _count_map(report.get("by_kind", {}), "by_kind"),
        "by_status": _count_map(report.get("by_status", {}), "by_status"),
        "top_anomalous_tool_families": _anomalies(
            report.get("top_anomalous_tool_families", []), "tool_family"
        ),
        "top_anomalous_task_classes": _anomalies(
            report.get("top_anomalous_task_classes", []), "task_class"
        ),
    }
    encoded = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_INPUT_BYTES:
        raise ReviewError("aggregate report is too large")
    return clean


def _validate_primary(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    if set(data) != {"proposals"} or not isinstance(data.get("proposals"), list):
        raise ReviewError("primary response schema is invalid")
    raw_items = data["proposals"]
    if len(raw_items) > MAX_BATCH:
        raise ReviewError("too many primary proposals")
    expected = {
        "title",
        "risk_class",
        "target_kind",
        "proposed_intervention",
        "expected_metric",
        "observation_count",
        "evidence_count",
        "counterevidence_count",
        "rollback_note",
    }
    proposals: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ReviewError("primary proposal schema is invalid")
        # Reuse the durable store validator without writing a database.  It is
        # intentionally private to keep one exact proposal contract.
        try:
            from .improvement import _validate_proposal

            proposals.append(_validate_proposal(raw))
        except ImprovementError as exc:
            raise ReviewError("primary proposal failed validation") from exc
    return proposals


def _apply_secondary(
    proposals: list[dict[str, Any]], data: Mapping[str, Any]
) -> list[dict[str, Any]]:
    if set(data) != {"verdicts"} or not isinstance(data.get("verdicts"), list):
        raise ReviewError("secondary response schema is invalid")
    verdicts = data["verdicts"]
    if len(verdicts) > len(proposals):
        raise ReviewError("too many secondary verdicts")
    by_index: dict[int, dict[str, Any]] = {}
    for raw in verdicts:
        if not isinstance(raw, dict):
            raise ReviewError("secondary verdict must be an object")
        if set(raw) - {"index", "decision", "risk_class"} or not {"index", "decision"}.issubset(raw):
            raise ReviewError("secondary verdict schema is invalid")
        index = raw.get("index")
        decision = raw.get("decision")
        if type(index) is not int or not 0 <= index < len(proposals) or index in by_index:
            raise ReviewError("secondary verdict index is invalid")
        if decision not in {"accept", "downgrade", "reject"}:
            raise ReviewError("secondary verdict decision is invalid")
        if decision != "downgrade" and "risk_class" in raw:
            raise ReviewError("risk_class is only valid for a downgrade")
        by_index[index] = raw

    selected: list[dict[str, Any]] = []
    for index, proposal in enumerate(proposals):
        verdict = by_index.get(index)
        if verdict is None or verdict["decision"] == "reject":
            continue
        item = dict(proposal)
        if verdict["decision"] == "downgrade":
            risk = verdict.get("risk_class")
            if risk not in _RISK_RANK:
                raise ReviewError("downgrade risk is invalid")
            if _RISK_RANK[str(risk)] >= _RISK_RANK[item["risk_class"]]:
                raise ReviewError("secondary reviewer attempted a risk upgrade")
            item["risk_class"] = str(risk)
        selected.append(item)
    return selected


def _invoke(command: ReviewerCommand, packet: Mapping[str, Any]) -> str:
    payload = json.dumps(packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_INPUT_BYTES:
        raise ReviewError("reviewer input exceeds the byte limit")
    try:
        proc = subprocess.Popen(
            list(command.argv),
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except (FileNotFoundError, OSError) as exc:
        raise ReviewError("reviewer could not start") from exc
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    pgid = proc.pid
    write_failed = threading.Event()

    def _write() -> None:
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            write_failed.set()

    writer = threading.Thread(target=_write, name="bplus-review-stdin", daemon=True)
    writer.start()
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + command.timeout_seconds
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReviewError("reviewer timed out")
            events = selector.select(min(0.1, remaining))
            if not events and proc.poll() is not None:
                events = selector.select(0)
                if not events:
                    if _group_exists(pgid):
                        _terminate_group(proc, pgid)
                    break
            for key, _ in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 65_536)
                except OSError as exc:
                    raise ReviewError("reviewer output failed") from exc
                if not chunk:
                    selector.unregister(stream)
                    continue
                bucket = buffers[str(key.data)]
                bucket.extend(chunk)
                if len(bucket) > command.max_output_bytes:
                    raise ReviewError("reviewer output exceeded the byte limit")
            if proc.poll() is not None and _group_exists(pgid):
                _terminate_group(proc, pgid)
        try:
            returncode = proc.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise ReviewError("reviewer timed out") from exc
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
    if returncode != 0 or write_failed.is_set():
        raise ReviewError("reviewer process failed")
    try:
        return bytes(buffers["stdout"]).decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ReviewError("reviewer output was not UTF-8") from exc


def _parse_object(raw: str) -> dict[str, Any]:
    if not raw.strip():
        raise ReviewError("reviewer returned no output")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewError("reviewer returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ReviewError("reviewer response must be an object")
    # Claude subscription wrappers commonly return a structured envelope;
    # fake/test wrappers may return the object directly.
    if "structured_output" in value:
        if set(value) - {
            "structured_output", "subtype", "is_error", "result", "error",
            "duration_ms", "duration_api_ms", "num_turns", "session_id", "total_cost_usd",
            "usage", "modelUsage", "permission_denials", "uuid",
        }:
            raise ReviewError("reviewer envelope contains unknown fields")
        if value.get("is_error") or value.get("subtype") not in (None, "", "success"):
            raise ReviewError("reviewer reported an error")
        value = value.get("structured_output")
        if not isinstance(value, dict):
            raise ReviewError("structured reviewer output must be an object")
    return value


def _duration_summary(value: Any) -> dict[str, int | float | None]:
    if not isinstance(value, Mapping) or set(value) - {"count", "median", "p90", "p95"}:
        raise ReviewError("duration_ms must be an aggregate object")
    output: dict[str, int | float | None] = {
        "count": _bounded_integer(value.get("count", 0), "duration count", 0, 10_000_000)
    }
    for key in ("median", "p90", "p95"):
        raw = value.get(key)
        if raw is None:
            output[key] = None
        elif isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= float(raw) <= 2_592_000_000:
            raise ReviewError(f"duration {key} is invalid")
        else:
            output[key] = round(float(raw), 6)
    return output


def _count_map(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or len(value) > 32:
        raise ReviewError(f"{label} must be a bounded object")
    output: dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(key, str) or not _CLASS_RE.fullmatch(key):
            raise ReviewError(f"{label} contains an unsafe class")
        output[key] = _bounded_integer(count, label, 0, 10_000_000)
    return dict(sorted(output.items()))


def _anomalies(value: Any, class_key: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_ANOMALY_ROWS:
        raise ReviewError("anomaly rows are invalid")
    output: list[dict[str, Any]] = []
    numeric = {"total", "errors", "stalls", "retries"}
    for row in value:
        if not isinstance(row, Mapping) or set(row) - ({class_key} | numeric) or class_key not in row:
            raise ReviewError("anomaly row schema is invalid")
        classification = row[class_key]
        if not isinstance(classification, str) or not _CLASS_RE.fullmatch(classification):
            raise ReviewError("anomaly class is unsafe")
        item: dict[str, Any] = {class_key: classification}
        for key in sorted(numeric):
            if key in row:
                item[key] = _bounded_integer(row[key], key, 0, 10_000_000)
        output.append(item)
    return output


def _optional_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ReviewError("small_sample must be a boolean")
    return value


def _rate(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
        raise ReviewError(f"{label} must be between zero and one")
    return round(float(value), 9)


def _bounded_integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReviewError(f"{label} is out of bounds")
    return value


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_group(proc: subprocess.Popen[bytes], pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + 0.5
    while _group_exists(pgid) and time.monotonic() < deadline:
        try:
            proc.wait(timeout=0.02)
        except subprocess.TimeoutExpired:
            pass
        if _group_exists(pgid):
            time.sleep(0.01)
    if _group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
