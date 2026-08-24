#!/bin/bash
#
# Double-click launcher for the daily sync to GitHub (macOS Finder opens
# .command files in Terminal).
#
# Two steps, in this order:
#
#   1. Rebuild the data manifests. The data tree is excluded from git in full,
#      so docs/data/<source>/MANIFEST.jsonl is the only committed record of what
#      the tree holds and which upstream object rebuilds each partition. It has
#      to be refreshed before the commit, not after, or the repository describes
#      yesterday's data. Needs .venv because it reads parquet footers; a run
#      over an unchanged tree reuses what it already recorded and costs seconds.
#
#   2. Stage, gate, commit and push. See scripts/sync_to_git.py.
#
# A manifest failure warns but does not stop the sync: a damaged partition is a
# data problem, and refusing to commit the day's source changes because of it
# would be the wrong trade. The warning is repeated in the closing summary.
#
# Behaviour:
#   Double-click  -> both steps.
#   Command line  -> arguments are forwarded to scripts/sync_to_git.py, e.g.
#                    ./sync_to_git.command --dry-run
#                    ./sync_to_git.command -m "message"
#                    ./sync_to_git.command --no-manifest      (skip step 1)
#
# Running scripts/sync_to_git.py directly performs step 2 only, and will commit
# whatever manifest is currently on disk.
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
MANIFEST_SCRIPT="${REPO_DIR}/scripts/build_data_manifest.py"
VENV_PYTHON="${REPO_DIR}/.venv/bin/python"

# Strip the launcher's own flag; everything else belongs to sync_to_git.py.
RUN_MANIFEST=1
SYNC_ARGS=()
for arg in "$@"; do
    if [ "${arg}" = "--no-manifest" ]; then
        RUN_MANIFEST=0
    else
        SYNC_ARGS+=("${arg}")
    fi
done

MANIFEST_NOTE=""

hold_window() {
    echo
    echo "----------------------------------------------------------------"
    # Only wait for a key when a person is actually there to press one. Finder
    # gives this script a terminal, so the pause happens on a double-click; a
    # pipe, a cron job or another script does not, and waiting there blocks
    # forever on input that will never arrive.
    if [ -t 0 ]; then
        read -n 1 -s -r -p "Press any key to close this window. "
        echo
    fi
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

# ---- Step 1: refresh the committed record of the excluded data tree ----
if [ "${RUN_MANIFEST}" -eq 1 ]; then
    if [ ! -d "${REPO_DIR}/data" ]; then
        echo "[1/2] No data/ directory here; nothing to catalogue."
    elif [ ! -x "${VENV_PYTHON}" ]; then
        MANIFEST_NOTE="manifests NOT refreshed: .venv/bin/python is missing"
        echo "[1/2] SKIPPED: ${VENV_PYTHON} not found."
        echo "      The manifest reads parquet footers and needs the project venv."
        echo "      The commit will carry whatever manifest is already on disk."
    else
        echo "[1/2] Rebuilding data manifests..."
        "${VENV_PYTHON}" "${MANIFEST_SCRIPT}"
        manifest_status=$?
        if [ ${manifest_status} -ne 0 ]; then
            MANIFEST_NOTE="manifest build exited ${manifest_status}: see the listing above"
            echo "      WARNING: continuing to the sync anyway."
        fi
    fi
else
    MANIFEST_NOTE="manifests skipped on request (--no-manifest)"
    echo "[1/2] Skipped on request."
fi
echo

# ---- Step 2: stage, gate, commit, push ----
echo "[2/2] Syncing to GitHub..."
"${PYTHON_BIN}" "${SYNC_SCRIPT}" "${SYNC_ARGS[@]+"${SYNC_ARGS[@]}"}"
STATUS=$?

echo
case "${STATUS}" in
    0) echo "RESULT: done. Working tree and origin/main agree." ;;
    2) echo "RESULT: BLOCKED by a gate. Nothing was committed, nothing pushed."
       echo "        Read the gate output above and clear every hit before"
       echo "        running this again." ;;
    *) echo "RESULT: failed (exit ${STATUS}). Nothing was pushed." ;;
esac
[ -n "${MANIFEST_NOTE}" ] && echo "NOTE:   ${MANIFEST_NOTE}"

hold_window "${STATUS}"
