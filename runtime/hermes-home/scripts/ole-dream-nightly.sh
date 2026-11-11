#!/bin/sh
set -eu
/home/hermes/hermes-extensions-staging/brain/venv/bin/python -B /home/hermes/hermes-acribic-daily-20261102/update_dream_status.py >/dev/null
exec env -i HOME=/home/hermes USER=hermes HERMES_HOME=/home/hermes/.hermes \
  LANG=C.UTF-8 PATH=/usr/bin:/bin \
  HERMES_DISABLE_LAZY_INSTALLS=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHON=/home/hermes/hermes-extensions-staging/brain/venv/bin/python \
  PROJECT_DIR=/home/hermes/hermes-extensions-staging/brain/package \
  DREAM_CONFIG=/home/hermes/hermes-extensions-staging/brain/manifest.json \
  MANIFEST=/home/hermes/hermes-extensions-staging/brain/manifest.json \
  /home/hermes/hermes-extensions-staging/brain/venv/bin/python \
  /home/hermes/hermes-extensions-staging/brain/package/scripts/dream_launcher.py \
  --project-dir /home/hermes/hermes-extensions-staging/brain/package \
  --config /home/hermes/hermes-extensions-staging/brain/manifest.json \
  --state-dir /home/hermes/hermes-extensions-staging/brain/dreaming/runtime \
  --interval-minutes 0
