#!/bin/sh
set -eu
exec env HERMES_HOME=/home/hermes/.hermes HERMES_DISABLE_LAZY_INSTALLS=1 PYTHONDONTWRITEBYTECODE=1 /usr/bin/timeout 110s /home/hermes/.hermes/hermes-agent/venv/bin/python -B /home/hermes/hermes-acribic-pilot-20261102/final-1901/candidate/launch.py --root /home/hermes/hermes-acribic-pilot-20261102/final-1901 tick --slot 4
