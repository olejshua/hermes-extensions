#!/bin/sh
set -eu

: "${PROJECT_DIR:?PROJECT_DIR is required}"
: "${MANIFEST:=$PROJECT_DIR/config/manifest.json}"
: "${PYTHON:=python3.11}"
: "${PYTHONPATH:=$PROJECT_DIR/src}"
: "${CONTEXT_IMPORT_DIR:?CONTEXT_IMPORT_DIR is required}"
: "${CONTEXT_EXPORT:=$HOME/.hermes/second-brain/import/context/context-inbox.txt}"
# Operational plugin default: ~/.hermes/second-brain/context-inbox-spool.jsonl
: "${CONTEXT_SPOOL:=$HOME/.hermes/second-brain/context-inbox-spool.jsonl}"
: "${HERMES_CONTEXT_INBOX_SPOOL:=$CONTEXT_SPOOL}"
: "${SLACK_IMPORT_PATH:=$CONTEXT_IMPORT_DIR/slack/slack-backfill-complete.jsonl.gz}"
: "${SLACK_LIVE_GZIP_IMPORT_PATH:=$CONTEXT_IMPORT_DIR/slack/slack-live.jsonl.gz}"
: "${SLACK_LIVE_IMPORT_PATH:=$CONTEXT_IMPORT_DIR/slack/slack-live.jsonl}"
: "${WHATSAPP_IMPORT_PATH:=$HOME/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite}"

export PYTHONPATH
quarantined=0

run_import() {
  source_name="$1"
  source_path="$2"
  if [ -e "$source_path" ]; then
    "$PYTHON" -m hermes_second_brain context-import --manifest "$MANIFEST" --source "$source_name" --path "$source_path" >/dev/null
  fi
}

run_spool() {
  set +e
  "$PYTHON" -m hermes_second_brain.queue_spool --spool "$CONTEXT_SPOOL" --python "$PYTHON" --module hermes_second_brain --manifest "$MANIFEST"
  status=$?
  set -e
  if [ "$status" -eq 2 ]; then
    quarantined=1
    return 0
  fi
  return "$status"
}

mkdir -p "$CONTEXT_IMPORT_DIR" "$(dirname "$CONTEXT_EXPORT")"

run_import jsonl "$CONTEXT_IMPORT_DIR/canonical.jsonl"
run_import jsonl "$CONTEXT_IMPORT_DIR/context-inbox-spool.jsonl"
run_spool
if [ "$HERMES_CONTEXT_INBOX_SPOOL" != "$CONTEXT_SPOOL" ]; then
  CONTEXT_SPOOL="$HERMES_CONTEXT_INBOX_SPOOL"
  run_spool
fi
if [ -e "$SLACK_IMPORT_PATH" ]; then
  run_import slack "$SLACK_IMPORT_PATH"
else
  run_import slack "$CONTEXT_IMPORT_DIR/slack"
fi
if [ -e "$SLACK_LIVE_GZIP_IMPORT_PATH" ]; then
  run_import slack "$SLACK_LIVE_GZIP_IMPORT_PATH"
else
  run_import slack "$SLACK_LIVE_IMPORT_PATH"
fi
run_import signal "$CONTEXT_IMPORT_DIR/signal.sqlite"
if [ -e "$WHATSAPP_IMPORT_PATH" ]; then
  run_import whatsapp "$WHATSAPP_IMPORT_PATH"
else
  run_import whatsapp "$CONTEXT_IMPORT_DIR/ChatStorage.sqlite"
fi

"$PYTHON" -m hermes_second_brain context-rank --manifest "$MANIFEST" >/dev/null
"$PYTHON" -m hermes_second_brain context-export-openviking --manifest "$MANIFEST" --output "$CONTEXT_EXPORT" >/dev/null

if [ "$quarantined" -ne 0 ]; then
  echo "context_cycle: quarantined invalid JSONL queue file(s)" >&2
  exit 1
fi
