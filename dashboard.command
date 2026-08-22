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

# Find the project interpreter. It normally sits at the repository root, but
# a git worktree has no .venv of its own, so the search walks upwards to the
# main working copy. QUANT_PYTHON overrides both.
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
  echo "[dashboard] no project interpreter found at or above $REPO"
  echo "[dashboard] create .venv there, or set QUANT_PYTHON, then re-run this file."
  read -r -n 1 -p "press any key to close"
  exit 1
fi

: "${QUANT_ENV:=live}"
export QUANT_ENV

echo "[dashboard] repository : $REPO"
echo "[dashboard] interpreter: $PY"
echo "[dashboard] environment: $QUANT_ENV"
echo "[dashboard] starting the local server; close this window to stop it."
echo

PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m trading212.dashboard.server "$@"
STATUS=$?

echo
echo "[dashboard] stopped with status $STATUS"
read -r -n 1 -p "press any key to close"
exit $STATUS
