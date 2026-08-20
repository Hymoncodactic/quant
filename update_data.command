#!/bin/bash
# Double-click to bring every dataset up to date.
#
# Safe to run repeatedly and safe to interrupt: only the gap between what is
# stored and what is available gets fetched, and partitions are written
# atomically so a stopped run leaves nothing half-written. caffeinate holds off
# idle sleep while it runs.
cd "$(dirname "$0")" || exit 1
caffeinate -i ./.venv/bin/python -u scripts/update_data.py
status=$?
echo
echo "-------------------------------------------------------------"
[ $status -eq 0 ] && echo "Update finished." || echo "Update exited with status $status."
echo "Press any key to close this window."
read -n 1 -s
