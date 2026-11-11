#!/bin/sh
set -eu

PROJECT_DIR="${PROJECT_DIR:-__PROJECT_DIR__}"
MANIFEST="${MANIFEST:-$PROJECT_DIR/config/manifest.json}"
LOCK_DIR="${LOCK_DIR:-/tmp/hermes-second-brain.lock}"
PYTHON="${PYTHON:-python3.11}"
cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "hermes-second-brain: previous run still active" >&2
  # Distinct temporary-failure status: a skipped sync is never an acknowledgement.
  exit 75
fi
trap cleanup EXIT INT TERM

cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

"$PYTHON" -m compileall -q src

if [ -n "${IMPORT_DIR:-}" ]; then
  if [ -n "${MAIL_EVENTS:-}" ] && [ -f "$MAIL_EVENTS" ]; then
    "$PYTHON" -m hermes_second_brain mail-ingest --input "$MAIL_EVENTS" --output-dir "$IMPORT_DIR/mail"
  fi

  if [ -n "${LCM_DBS:-}" ]; then
    set -- "$PYTHON" -m hermes_second_brain lcm-export --output-dir "$IMPORT_DIR/lcm"
    old_ifs=$IFS
    IFS=:
    for db in $LCM_DBS; do
      [ -n "$db" ] && set -- "$@" --lcm-db "$db"
    done
    IFS=$old_ifs
    "$@"
  fi

  if [ -n "${HERMES_MEMORY_MD:-}" ] || [ -n "${HERMES_USER_MD:-}" ] || [ -n "${HOLOGRAPHIC_DB:-}" ]; then
    set -- "$PYTHON" -m hermes_second_brain migrate --output-dir "$IMPORT_DIR/migration"
    [ -n "${HERMES_MEMORY_MD:-}" ] && set -- "$@" --memory-md "$HERMES_MEMORY_MD"
    [ -n "${HERMES_USER_MD:-}" ] && set -- "$@" --user-md "$HERMES_USER_MD"
    [ -n "${HOLOGRAPHIC_DB:-}" ] && set -- "$@" --holographic-db "$HOLOGRAPHIC_DB"
    "$@"
  fi
elif [ -n "${MAIL_EVENTS:-}" ] || [ -n "${LCM_DBS:-}" ] || [ -n "${HERMES_MEMORY_MD:-}" ] || [ -n "${HERMES_USER_MD:-}" ] || [ -n "${HOLOGRAPHIC_DB:-}" ]; then
  echo "hermes-second-brain: IMPORT_DIR is required for scheduled exports" >&2
  exit 1
fi

CONTEXT_WATCHER="${CONTEXT_WATCHER:-$HOME/.hermes/profiles/general/scripts/context_watch.py}"
if [ -n "${CONTEXT_IMPORT_DIR:-}" ]; then
  # All scheduled Context Inbox writers must pass through the same non-blocking
  # wrapper as Hermes cron. A concurrent tick is intentionally silent and does
  # not contend on context-inbox.sqlite3.
  if [ ! -f "$CONTEXT_WATCHER" ]; then
    echo "hermes-second-brain: context watcher missing: $CONTEXT_WATCHER" >&2
    exit 1
  fi
  CONTEXT_IMPORT_DIR="$CONTEXT_IMPORT_DIR" \
    CONTEXT_EXPORT="${CONTEXT_EXPORT:-$CONTEXT_IMPORT_DIR/context/context-inbox.txt}" \
    CONTEXT_SPOOL="${CONTEXT_SPOOL:-$HOME/.hermes/second-brain/context-inbox-spool.jsonl}" \
    "$PYTHON" "$CONTEXT_WATCHER"
fi

"$PYTHON" -m hermes_second_brain sync --manifest "$MANIFEST"
