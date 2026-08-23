#!/bin/bash
# Double-click to bring every dataset up to date.
#
# Safe to run repeatedly and safe to interrupt: only the gap between what is
# stored and what is available gets fetched, and partitions are written
# atomically so a stopped run leaves nothing half-written.
#
# A lock prevents a second copy from starting. Two copies share one link and each
# halves the other's throughput; on an earlier run that contention was what turned
# a clean download into thirteen timeouts.
cd "$(dirname "$0")" || exit 1

LOCK="/tmp/quant_update_data.lock"
exec 9>"$LOCK"
if ! flock -n 9 2>/dev/null; then
    # macOS ships no flock; fall back to a pid file.
    if [ -f "$LOCK.pid" ] && kill -0 "$(cat "$LOCK.pid" 2>/dev/null)" 2>/dev/null; then
        echo "An update is already running (PID $(cat "$LOCK.pid"))."
        echo "Wait for it to finish, or stop it first. Press any key to close."
        read -n 1 -s
        exit 1
    fi
fi
echo $$ > "$LOCK.pid"
trap 'rm -f "$LOCK.pid"' EXIT

caffeinate -i ./.venv/bin/python -u scripts/update_data.py
status=$?
echo
echo "-------------------------------------------------------------"
[ $status -eq 0 ] && echo "Update finished." || echo "Update exited with status $status."
echo "Press any key to close this window."
read -n 1 -s
