#!/bin/bash
set -eu
exec /usr/bin/systemctl --user start --no-block ole-acribic-daily-dispatch.service
