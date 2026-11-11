#!/bin/sh
set -eu

PROJECT_DIR="${PROJECT_DIR:-__PROJECT_DIR__}"
CONFIG="${DREAM_CONFIG:-$PROJECT_DIR/config/manifest.json}"
PYTHON="${PYTHON:-python3.11}"

cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

# SQLite's owner/PID/generation-aware Dream lease is authoritative. A separate
# mkdir lock can go stale and disagree with the resumable publication outbox.
"$PYTHON" -m hermes_second_brain dream --config "$CONFIG" "$@"
