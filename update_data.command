#!/bin/bash
# Double-click to bring every dataset up to date.
#
# Safe to run repeatedly and safe to interrupt: only the gap between what is
# stored and what is available gets fetched, and partitions are written
# atomically so a stopped run leaves nothing half-written. caffeinate holds off
# idle sleep while it runs.
#
# Three passes run in order: crypto archives, the core equity universe at all
# five intervals, and the 502-name B0 pairs-trading universe at the daily
# interval only. The B0 pass is the long one on a first run and is skipped
# entirely on a quiet day, since each name is probed against what is already
# stored. Pass --no-b0 to skip it.
cd "$(dirname "$0")" || exit 1
caffeinate -i ./.venv/bin/python -u scripts/update_data.py
status=$?
echo
echo "-------------------------------------------------------------"
[ $status -eq 0 ] && echo "Update finished." || echo "Update exited with status $status."
echo "Press any key to close this window."
read -n 1 -s
