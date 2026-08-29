#!/bin/bash
# Double-click launcher for the DEMO (paper) trading dashboard.
#
# Responsibility: start the dashboard against the Trading 212 Practice
# environment (demo.trading212.com, practice money). Identical to
# dashboard.command except QUANT_ENV=paper; the paper dashboard keeps its
# own state directory, its own single-instance lock and its own port
# (8788), so it runs happily beside the live one.
#
# The dashboard only reads and charts. It starts no strategy and stops no
# strategy.

set -u
cd "$(dirname "$0")" || exit 1
REPO="$(pwd)"

PY="${QUANT_PYTHON:-}"
if [ -z "$PY" ]; then
  DIR="$REPO"
  for _ in 1 2 3 4 5 6; do
    if [ -x "$DIR/.venv/bin/python" ]; then PY="$DIR/.venv/bin/python"; break; fi
    DIR="$(dirname "$DIR")"
    [ "$DIR" = "/" ] && break
  done
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "[dashboard-demo] no project interpreter found at or above $REPO"
  read -r -n 1 -p "press any key to close"
  exit 1
fi

export QUANT_ENV=paper

echo "[dashboard-demo] repository : $REPO"
echo "[dashboard-demo] interpreter: $PY"
echo "[dashboard-demo] environment: paper (demo.trading212.com, practice money)"
echo "[dashboard-demo] starting the local server; close this window to stop it."
echo

PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m trading212.dashboard.server "$@"
STATUS=$?

echo
echo "[dashboard-demo] stopped with status $STATUS"
read -r -n 1 -p "press any key to close"
exit $STATUS
