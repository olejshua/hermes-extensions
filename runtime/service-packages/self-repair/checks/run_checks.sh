#!/bin/sh
# Runs the runtime/calendar check suites against the published layout.
# Several older tests resolve source relative to a flatter private layout
# (gtm/calendar/runtime, plugins, second-brain/src). This builds that view in a
# temp directory from symlinks; it changes nothing in the repository.
# Needs python3 with pytest. Hermes-home scripts win over the gtm copies where
# both exist (they are different files; see the note in the checkpoint report).
set -u
status=0
HERE=$(cd "$(dirname "$0")" && pwd)
RT=$(cd "$HERE/../../.." && pwd)            # .../runtime
L=$(mktemp -d)
trap 'rm -rf "$L"' EXIT
mkdir -p "$L/gtm/calendar/runtime" "$L/second-brain" "$L/tests"
for f in "$RT"/hermes-home/scripts/*.py "$RT"/service-packages/gtm/gtm/calendar/runtime/*.py; do
  [ -e "$L/gtm/calendar/runtime/$(basename "$f")" ] || ln -s "$f" "$L/gtm/calendar/runtime/$(basename "$f")"
done
ln -s "$RT/hermes-home/plugins" "$L/plugins"
ln -s "$RT/service-packages/brain/src" "$L/second-brain/src"
ln -s "$RT/service-packages/self-repair/self_repair" "$L/self_repair"
mkdir -p "$L/tests/runtime"
cp -r "$HERE/tests/calendar" "$L/tests/"
# Only tests that expect the flat layout (parents[2]) are copied; the rest run in place below.
cp $(grep -l 'parents\[2\]' "$HERE"/tests/runtime/*.py) "$L/tests/runtime/"
cd "$L/tests/runtime"
PYTHONPATH="$RT/service-packages/gtm:$RT/service-packages/brain/src:$L/tests/calendar:$L/gtm/calendar/runtime" \
  python3 -m pytest -q -p no:cacheprovider "$@" . || status=1
cd "$HERE/tests/runtime"
PYTHONPATH="$RT/service-packages/gtm:$RT/service-packages/brain/src:$RT/service-packages/self-repair" \
  python3 -m pytest -q -p no:cacheprovider $(grep -L 'parents\[2\]' *.py) || status=1
exit $status
