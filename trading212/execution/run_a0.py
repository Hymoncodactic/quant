"""CLI entry for the A0 live-execution cycle.

Responsibility: argument parsing, configuration loading (once, at the
entry layer -- quant-code-standards section 4.8), and human-readable
output. All behavior lives in session_cycle.py.

Usage (from the repository root, with the venv python):
    python -m trading212.execution.run_a0 status
    python -m trading212.execution.run_a0 decide
    python -m trading212.execution.run_a0 decide --allow-orders
    python -m trading212.execution.run_a0 settle
    python -m trading212.execution.run_a0 init-ledger --cash-gbp 500
    python -m trading212.execution.run_a0 halt

Environment: QUANT_ENV selects t212.paper.yaml (demo host) or
t212.live.yaml (live host); the default is paper (CLAUDE.md section 3.3).
The stored API key authenticates against the LIVE host only (probed
2026-08-21), so read-only and dry-run work against live requires
QUANT_ENV=live with execution.dry_run left true.

Real submission requires ALL of: QUANT_ENV=live, `live: true` in the
configuration, execution.dry_run explicitly false, positive risk limits,
and the per-run --allow-orders flag. Missing any one downgrades to dry run
or refuses outright.

Public functions:
    main(argv=None)   CLI entry point
"""

from __future__ import annotations

__all__ = ["main"]

import argparse
import fcntl
import json
import os
import sys
import time
from decimal import Decimal

from common.config import load_config
from common.logging_setup import get_logger
from common.paths import execution_state_dir
from trading212.execution import session_cycle

log = get_logger("t212.execution")


def _acquire_instance_lock():
    """Take an exclusive non-blocking lock for the whole invocation.

    Two concurrent invocations racing the cycle state could double-submit
    (the QMT reference logged exactly this incident class). The lock file
    lives beside the ledger; the OS releases it on process exit, so a crash
    never leaves a stale lock.

    The file is opened in append mode and truncated only AFTER the flock
    succeeds: a failing second invocation must not wipe the holder's pid
    line, which is what the operations card tells the operator to read.
    A short retry rides out the dashboard's brief holds of the same lock
    (its ledger-writing routes take it for fractions of a second).
    """
    lock_dir = execution_state_dir("t212")
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = open(lock_dir / "run_a0.lock", "a+", encoding="utf-8")
    for attempt in range(4):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if attempt == 3:
                handle.seek(0)
                holder = handle.read().strip() or "unknown holder"
                handle.close()
                raise SystemExit(
                    f"the execution lock is held ({holder}); another run_a0 "
                    f"or a dashboard ledger operation is active -- refusing "
                    f"to run concurrently")
            time.sleep(0.5)
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return handle  # kept referenced by the caller for the process lifetime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_a0", description="A0 live execution cycle on Trading 212")
    sub = parser.add_subparsers(dest="command", required=True)

    p_decide = sub.add_parser("decide", help="compute targets on the 15:30 bar "
                              "and submit before the close (dry run by default)")
    p_decide.add_argument("--allow-orders", action="store_true",
                          help="arm THIS run for real submission; without it "
                               "the run is a dry run regardless of config")
    sub.add_parser("settle", help="poll submitted orders, harvest fills, reconcile")
    sub.add_parser("status", help="read-only account and book overview")
    p_init = sub.add_parser("init-ledger", help="create the strategy book")
    p_init.add_argument("--cash-gbp", required=True, type=Decimal,
                        help="explicit GBP cash allocation for the strategy")
    sub.add_parser("halt", help="set the halt flag (removal is manual only)")

    args = parser.parse_args(argv)
    lock_handle = _acquire_instance_lock()  # noqa: F841  held until exit
    cfg = load_config("t212")
    log.info("[cli] command=%s env=%s config=%s", args.command, cfg["_env"],
             cfg["_path"])

    if args.command == "decide":
        result = session_cycle.decide(cfg, armed=args.allow_orders)
    elif args.command == "settle":
        result = session_cycle.settle(cfg)
    elif args.command == "status":
        result = session_cycle.status(cfg)
    elif args.command == "init-ledger":
        result = session_cycle.init_ledger(cfg, args.cash_gbp)
    elif args.command == "halt":
        halt = execution_state_dir("t212") / "halt"
        halt.parent.mkdir(parents=True, exist_ok=True)
        halt.touch()
        result = {"phase": "halt", "halt_file": str(halt),
                  "note": "removal is a deliberate act: the dashboard clear "
                          "button (which re-checks the book first) or a "
                          "manual delete after ruling the situation safe"}
    else:  # pragma: no cover - argparse enforces the choices
        raise ValueError(args.command)

    print(json.dumps(result, indent=1, ensure_ascii=False, default=str))
    return 1 if result.get("aborted") else 0


if __name__ == "__main__":
    sys.exit(main())
