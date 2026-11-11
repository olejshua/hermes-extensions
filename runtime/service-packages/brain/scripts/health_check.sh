#!/bin/sh
set -eu

fail_config() {
    printf 'health check configuration (%s): %s\n' "$1" "$2" >&2
    exit 64
}

resolve_executable() {
    executable_spec=$1
    executable_stage=$2
    [ -n "$executable_spec" ] || fail_config "$executable_stage" "executable specification is empty"
    case "$executable_spec" in
        /*) resolved_executable=$executable_spec ;;
        */*) resolved_executable=$PROJECT_DIR/$executable_spec ;;
        *)
            resolved_executable=$(command -v "$executable_spec" 2>/dev/null) ||
                fail_config "$executable_stage" "command not found: $executable_spec"
            ;;
    esac
    [ -x "$resolved_executable" ] || fail_config "$executable_stage" "not executable: $executable_spec"
    printf '%s\n' "$resolved_executable"
}

SCRIPT_DIR=$(CDPATH='' cd "$(dirname "$0")" 2>/dev/null && pwd -P) || fail_config project_dir "cannot resolve script directory"
PROJECT_DIR="${PROJECT_DIR:-$(CDPATH='' cd "$SCRIPT_DIR/.." 2>/dev/null && pwd -P)}"
MANIFEST="${MANIFEST:-$PROJECT_DIR/config/manifest.json}"
READINESS_ATTEMPTS="${READINESS_ATTEMPTS-15}"
READINESS_TIMEOUT_SECONDS="${READINESS_TIMEOUT_SECONDS-10}"
READINESS_DELAYS="${READINESS_DELAYS-5 10 20 40 60}"

[ -d "$PROJECT_DIR" ] || fail_config project_dir "not a directory: $PROJECT_DIR"
[ -f "$MANIFEST" ] || fail_config manifest "not a file: $MANIFEST"

if [ "${PYTHON+x}" != x ]; then
    if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
        PYTHON="$PROJECT_DIR/.venv/bin/python"
    else
        fail_config python "project runtime is unavailable: $PROJECT_DIR/.venv/bin/python"
    fi
fi
PYTHON=$(resolve_executable "$PYTHON" python) || exit $?

case "$READINESS_ATTEMPTS" in ''|*[!0-9]*) fail_config readiness "attempts must be an integer within 1..60" ;; esac
if [ "$READINESS_ATTEMPTS" -lt 1 ] || [ "$READINESS_ATTEMPTS" -gt 60 ]; then
    fail_config readiness "attempts must be an integer within 1..60"
fi
case "$READINESS_TIMEOUT_SECONDS" in ''|*[!0-9]*) fail_config readiness "timeout must be an integer within 1..60" ;; esac
if [ "$READINESS_TIMEOUT_SECONDS" -lt 1 ] || [ "$READINESS_TIMEOUT_SECONDS" -gt 60 ]; then
    fail_config readiness "timeout must be an integer within 1..60"
fi

set -f
# Deliberate whitespace tokenization with pathname expansion disabled.
# shellcheck disable=SC2086
set -- $READINESS_DELAYS
set +f
[ "$#" -gt 0 ] || fail_config readiness "delays require at least one integer within 0..60"
for readiness_delay do
    case "$readiness_delay" in ''|*[!0-9]*) fail_config readiness "each delay must be an integer within 0..60" ;; esac
    if [ "$readiness_delay" -gt 60 ]; then
        fail_config readiness "each delay must be an integer within 0..60"
    fi
done

if [ "${OV_BINARY+x}" != x ]; then
    if ! OV_BINARY=$(
        "$PYTHON" - "$MANIFEST" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        value = json.load(handle).get("ov_binary")
except (OSError, json.JSONDecodeError, AttributeError):
    raise SystemExit(1)
if not isinstance(value, str) or not value.strip():
    raise SystemExit(1)
print(value)
PY
    ); then
        fail_config manifest "invalid or missing ov_binary"
    fi
fi
OV_BINARY=$(resolve_executable "$OV_BINARY" openviking) || exit $?

cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

attempt=1
ready=false
while [ "$attempt" -le "$READINESS_ATTEMPTS" ]; do
    if "$PYTHON" - "$OV_BINARY" "$READINESS_TIMEOUT_SECONDS" <<'PY' >/dev/null 2>&1
import subprocess
import sys

try:
    result = subprocess.run(
        [sys.argv[1], "health", "-o", "json"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=int(sys.argv[2]),
        check=False,
    )
except (OSError, subprocess.TimeoutExpired, ValueError):
    raise SystemExit(1)
raise SystemExit(result.returncode)
PY
    then
        ready=true
    fi
    [ "$ready" = true ] && break
    if [ "$attempt" -lt "$READINESS_ATTEMPTS" ]; then
        delay=60
        index=1
        for candidate do
            delay=$candidate
            [ "$index" -ge "$attempt" ] && break
            index=$((index + 1))
        done
        sleep "$delay"
    fi
    attempt=$((attempt + 1))
done

if [ "$ready" != true ]; then
    printf 'health check unavailable: OpenViking not ready after %s attempt(s)\n' "$READINESS_ATTEMPTS" >&2
    exit 69
fi

if "$PYTHON" -m hermes_second_brain sync --manifest "$MANIFEST" --dry-run >/dev/null; then
    :
else
    sync_status=$?
    printf 'health check sync failed with exit %s\n' "$sync_status" >&2
    exit "$sync_status"
fi
