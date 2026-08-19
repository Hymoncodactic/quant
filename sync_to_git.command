#!/bin/bash
#
# Double-click launcher for the daily git sync (macOS Finder opens .command
# files in Terminal). All the real work lives in scripts/sync_to_git.py; this
# file only fixes up the launch environment, reports the outcome in plain
# words, and holds the window open so a blocked gate cannot scroll past unseen.
#
# Behaviour:
#   Double-click  -> ordinary daily sync (stage, gate, commit, push).
#   Command line  -> arguments are forwarded, e.g.
#                    ./sync_to_git.command --dry-run
#                    ./sync_to_git.command -m "message"
#
# Exit codes are those of scripts/sync_to_git.py:
#   0  synced, or nothing had changed
#   1  environment or usage problem (nothing was committed or pushed)
#   2  a gate fired: suspected credential or oversized file (nothing pushed)
#
# Note: a force-overwrite of the remote is deliberately NOT reachable by
# double-click. Run the Python entry point directly with --overwrite-remote.

set -uo pipefail

# Finder launches this with a minimal environment, and git is a Homebrew build
# at /opt/homebrew/bin/git, so the default PATH alone is not enough.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

# Resolve the repository from this file's own location, never from the working
# directory: Finder starts double-clicked scripts in the user's home folder.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_SCRIPT="${REPO_DIR}/scripts/sync_to_git.py"

hold_window() {
    echo
    echo "----------------------------------------------------------------"
    read -n 1 -s -r -p "Press any key to close this window. "
    echo
    exit "${1}"
}

echo "================================================================"
echo " quant -> GitHub sync"
echo " repo : ${REPO_DIR}"
echo " time : $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================================"
echo

if ! command -v git >/dev/null 2>&1; then
    echo "ABORTED: git was not found on PATH."
    echo "Install the Xcode command line tools with: xcode-select --install"
    hold_window 1
fi

PYTHON_BIN="$(command -v python3 || true)"
if [ -z "${PYTHON_BIN}" ] && [ -x /usr/bin/python3 ]; then
    PYTHON_BIN="/usr/bin/python3"
fi
if [ -z "${PYTHON_BIN}" ]; then
    echo "ABORTED: python3 was not found on PATH."
    echo "Install the Xcode command line tools with: xcode-select --install"
    hold_window 1
fi

if [ ! -f "${SYNC_SCRIPT}" ]; then
    echo "ABORTED: ${SYNC_SCRIPT} is missing."
    echo "This launcher must sit in the repository root, beside scripts/."
    hold_window 1
fi

"${PYTHON_BIN}" "${SYNC_SCRIPT}" "$@"
STATUS=$?

echo
case "${STATUS}" in
    0) echo "RESULT: done. Working tree and origin/main agree." ;;
    2) echo "RESULT: BLOCKED by a gate. Nothing was committed, nothing pushed."
       echo "        Read the gate output above and clear every hit before"
       echo "        running this again." ;;
    *) echo "RESULT: failed (exit ${STATUS}). Nothing was pushed." ;;
esac

hold_window "${STATUS}"
