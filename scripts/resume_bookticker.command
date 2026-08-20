#!/bin/bash
# Double-click to resume the bookTicker download.
#
# Safe to run at any time: completed partitions are skipped, damaged ones are
# detected and re-fetched, and the run can be interrupted again by closing this
# window or pressing Ctrl-C. caffeinate holds off idle sleep for the duration;
# closing the laptop lid still sleeps the machine and stops the download, which
# is harmless but pauses progress.
cd "$(dirname "$0")/.." || exit 1
exec caffeinate -i ./.venv/bin/python -u scripts/20260819_ingest_crypto_bookticker.py
