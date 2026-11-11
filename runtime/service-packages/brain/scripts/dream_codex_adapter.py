#!/usr/bin/env python3
"""Claude-JSON-contract adapter for a sandboxed Codex Dreaming fallback.

It accepts the narrow argv contract issued by ``dreaming.model.ModelAdapter``
and turns Codex's schema-constrained final message into that contract's JSON
envelope.  The model sees only the redacted stdin prompt; it gets an empty
read-only work directory and no Hermes/user configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


MAX_PROMPT_BYTES = 1_000_000
MAX_OUTPUT_BYTES = 2_000_000


def _strict_response_schema(value: object) -> object:
    """Return a Codex-compatible strict copy without mutating the shared contract."""
    if isinstance(value, list):
        return [_strict_response_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    strict = {key: _strict_response_schema(item) for key, item in value.items()}
    properties = strict.get("properties")
    if isinstance(properties, dict):
        strict["required"] = list(properties)
        strict["additionalProperties"] = False
    return strict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", action="store_true", required=True)
    parser.add_argument("--output-format", choices=("json",), required=True)
    parser.add_argument("--json-schema", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--safe-mode", action="store_true", required=True)
    parser.add_argument("--no-session-persistence", action="store_true", required=True)
    parser.add_argument("--tools", required=True)
    parser.add_argument("--runner", default="/opt/homebrew/bin/codex")
    args = parser.parse_args(argv)
    if args.tools or not args.safe_mode or not args.no_session_persistence:
        return 2
    raw_prompt = sys.stdin.buffer.read(MAX_PROMPT_BYTES + 1)
    if len(raw_prompt) > MAX_PROMPT_BYTES:
        return 2
    try:
        schema = json.loads(args.json_schema)
        if not isinstance(schema, dict):
            return 2
    except json.JSONDecodeError:
        return 2

    with tempfile.TemporaryDirectory(prefix="dream-codex-") as temporary:
        root = Path(temporary)
        os.chmod(root, 0o700)
        workdir = root / "work"
        workdir.mkdir(mode=0o700)
        schema_path = root / "schema.json"
        result_path = root / "result.json"
        # Codex's response-format endpoint requires strict object schemas:
        # every declared property must be required and objects must reject
        # undeclared keys.  The shared Claude contract intentionally leaves
        # several fields optional, so normalize a private copy for Codex only.
        schema_path.write_text(json.dumps(_strict_response_schema(schema), separators=(",", ":")), encoding="utf-8")
        os.chmod(schema_path, 0o600)
        command = [
            args.runner,
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
            "-C",
            str(workdir),
            "-",
        ]
        if args.model:
            command[2:2] = ["-m", args.model]
        try:
            completed = subprocess.run(
                command,
                input=raw_prompt,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=workdir,
                env={
                    "HOME": os.environ.get("HOME", str(root)),
                    "CODEX_HOME": os.environ.get(
                        "CODEX_HOME", str(Path.home() / ".codex")
                    ),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "NO_COLOR": "1",
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
                    "TMPDIR": str(root),
                },
                timeout=900,
            )
        except (OSError, subprocess.TimeoutExpired):
            return 1
        if completed.returncode != 0 or not result_path.is_file():
            diagnostic = completed.stderr.decode("utf-8", "replace").lower()
            if "quota" in diagnostic or "usage limit" in diagnostic or "rate limit" in diagnostic:
                sys.stderr.write("codex fallback unavailable: quota")
            elif "auth" in diagnostic or "login" in diagnostic or "unauthor" in diagnostic:
                sys.stderr.write("codex fallback unavailable: authentication")
            else:
                sys.stderr.write("codex fallback failed technically")
            return 1
        raw_result = result_path.read_bytes()
        if len(raw_result) > MAX_OUTPUT_BYTES:
            return 1
        try:
            structured = json.loads(raw_result.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 1
        if not isinstance(structured, dict):
            return 1
        sys.stdout.write(json.dumps({"subtype": "success", "structured_output": structured}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
