#!/bin/bash
# Double-click launcher for the A0 trading dashboard.
#
# Responsibility: locate the repository from this file's own position, pick
# the project interpreter, and start the local dashboard server. Finder
# starts a double-clicked script with the home directory as the working
# directory and a minimal PATH, so neither may be assumed here.
#
# The dashboard only reads and charts. It starts no strategy and stops no
# strategy: the scheduled run_a0 process is entirely separate, and closing
# this window leaves it running.

set -u
cd "$(dirname "$0")" || exit 1
REPO="$(pwd)"

PY="$REPO/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[dashboard] no interpreter at $PY"
  echo "[dashboard] create it first, then re-run this file."
  read -r -n 1 -p "press any key to close"
  exit 1
fi

: "${QUANT_ENV:=live}"
export QUANT_ENV

echo "[dashboard] repository : $REPO"
echo "[dashboard] environment: $QUANT_ENV"
echo "[dashboard] starting the local server; close this window to stop it."
echo

"$PY" -m trading212.dashboard.server "$@"
STATUS=$?

echo
echo "[dashboard] stopped with status $STATUS"
read -r -n 1 -p "press any key to close"
exit $STATUS
