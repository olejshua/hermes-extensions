#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-__PROJECT_DIR__}"
APP="${APP:-hermes_second_brain}"
PYTHON="${PYTHON:-python3.11}"
MANIFEST="${MANIFEST:-$PROJECT_DIR/config/manifest.json}"
OV_BINARY="${OV_BINARY:-$HOME/.openviking/venv/bin/ov}"
MAIL_CONTEXT_TIMEOUT="${MAIL_CONTEXT_TIMEOUT:-120}"
MAIL_CONTEXT_WORKERS="${MAIL_CONTEXT_WORKERS:-4}"
MAIL_CONTEXT_RETRIES="${MAIL_CONTEXT_RETRIES:-3}"

cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

"$PYTHON" -m "$APP" mail-context-collect \
  --full \
  --account icloud \
  --account gmail \
  --account opencompany \
  --timeout "$MAIL_CONTEXT_TIMEOUT" \
  --workers "$MAIL_CONTEXT_WORKERS" \
  --retries "$MAIL_CONTEXT_RETRIES" \
  --json

"$PYTHON" -m "$APP" mail-context-bundle --json

OV_BINARY="$OV_BINARY" "$PYTHON" -m "$APP" sync --manifest "$MANIFEST"
