#!/bin/bash
# Double-click to resume the bookTicker download.
#
# Safe at any time: completed partitions are skipped, damaged ones are detected
# and re-fetched, and the run can be interrupted again by closing this window.
# A lock prevents a second copy from competing for the same link.
cd "$(dirname "$0")/.." || exit 1

LOCK="/tmp/quant_bookticker.lock"
if [ -f "$LOCK.pid" ] && kill -0 "$(cat "$LOCK.pid" 2>/dev/null)" 2>/dev/null; then
    echo "A bookTicker download is already running (PID $(cat "$LOCK.pid"))."
    echo "Press any key to close."
    read -n 1 -s
    exit 1
fi
echo $$ > "$LOCK.pid"
trap 'rm -f "$LOCK.pid"' EXIT

caffeinate -i ./.venv/bin/python -u scripts/20260819_ingest_crypto_bookticker.py
echo
echo "Press any key to close this window."
read -n 1 -s
