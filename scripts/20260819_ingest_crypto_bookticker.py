"""One-off ingest of the discontinued Binance bookTicker archive over its fixed window.

Responsibility: fetch every published USD-M bookTicker day for the configured
symbols and store one parquet partition per symbol-day under the curated layer,
resuming from whatever is already on disk. bookTicker is event-driven best bid
and ask WITH quantities, roughly 300 updates per second, and it is the only free
tick-level top-of-book data that will ever exist for this source: publication
stopped on 2024-03-30 for USD-M and on 2024-10-14 for COIN-M, with no official
statement, so the window is fixed and will not grow. Because the dataset is
finite and already dead it is taken in full now; nothing about it improves by
waiting, and five other datasets in this archive have gone dark the same way
without notice.

Daily files are used rather than monthly. A monthly file is 4 to 6 GB compressed
and expands past 20 GB in memory, while a daily file peaks around 1 GB, which
matters on a machine with 8 GB of RAM.

The run is designed to be interrupted. At roughly 1.6 MB/s to this archive the
full window takes about 17 hours, so it will be stopped and restarted at least
once. Three mechanisms make that safe. First, partitions are written atomically
by common/store.py, so an interrupted write leaves no file rather than a
truncated one. Second, on start any temporary partitions are removed and every
existing output has its footer read, so a partition damaged by power loss is
deleted and re-fetched instead of passing an existence check forever and
silently poisoning the dataset. Third, SIGINT and SIGTERM set a stop flag, so
workers finish the file in flight and take no new work, and the process exits
within one file rather than being killed mid-write.

Out of scope: archive URL construction and download, which belong to
crypto_trading/ingest/binance_archive.py; atomic parquet writing and stale
temporary-partition cleanup, which belong to common/store.py; incremental
updates of the still-published datasets, which belong to
scripts/update_data.py. This dataset is never updated because publication has
ceased.

Public functions:
    main()   Fetch every published bookTicker day, resuming from what is on disk.

Constants:
    VENUE, LAYER, MARKET, DATASET  str  Partition coordinates: "binance",
                            "curated", "um", "bookTicker".
    WORKERS           int   Concurrent download workers, 4. Each holds about
                            1 GB while parsing and the job is bandwidth-bound
                            anyway, so more workers buy memory pressure, not
                            speed.
    SYMBOLS           list  Symbols fetched, BTCUSDT and ETHUSDT.
    FIRST_DAY         date  First published day, 2023-05-16. Source: listing of
                            the archive directory.
    LAST_DAY          date  Last published day, 2024-03-30, after which
                            publication ceased. Source: the same listing.
    _STOP             bool  Module-level stop flag set by the signal handler and
                            read by the workers before they take new work.

Inputs:
    Binance public archive, through binance_archive.fetch_to_frame().
Outputs:
    data/binance/curated/um/bookTicker/<symbol>/bookTicker/year=YYYY/YYYYMMDD.parquet

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["main"]

import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow as pa

from common.paths import DIR_DATA
from common.store import clear_stale_temps, is_readable_parquet, write_table
from crypto_trading.ingest import binance_archive as archive

# Set by the signal handler. Workers check it before starting new work so that an
# interrupted run stops at a file boundary rather than inside a write.
_STOP = False

VENUE, LAYER, MARKET, DATASET = "binance", "curated", "um", "bookTicker"

# Four workers rather than eight: each holds about 1 GB while parsing, and the
# job is bandwidth-bound anyway, so more workers buy memory pressure not speed.
WORKERS = 4

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
FIRST_DAY = date(2023, 5, 16)     # first published day, verified by listing
LAST_DAY = date(2024, 3, 30)      # publication ceased, verified by listing


def _install_signal_handlers() -> None:
    """Make SIGINT and SIGTERM request a clean stop instead of killing the process."""
    def handler(signum, _frame):
        global _STOP
        if _STOP:                      # further signals during the drain are noise
            return
        _STOP = True
        print(f"\n  signal {signum} received: finishing files in flight, starting "
              f"no new ones. Re-run this script to resume.", flush=True)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _out_path(symbol: str, day: str) -> Path:
    """Destination partition for one symbol-day."""
    return (DIR_DATA / VENUE / LAYER / MARKET / DATASET / symbol / DATASET
            / f"year={day[:4]}" / f"{day.replace('-', '')}.parquet")


def _prune_damaged(tasks: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], int, int]:
    """Split tasks into those still to do and those already complete.

    Every existing output has its footer read. A file that fails is deleted and
    returned to the work list: a partition damaged by power loss would otherwise
    pass an existence check forever and silently poison the dataset.
    """
    todo, done, repaired = [], 0, 0
    for symbol, day in tasks:
        out = _out_path(symbol, day)
        if not out.exists():
            todo.append((symbol, day))
        elif is_readable_parquet(out):
            done += 1
        else:
            out.unlink(missing_ok=True)
            repaired += 1
            todo.append((symbol, day))
    return todo, done, repaired


def _days() -> list[str]:
    """Every date in the published window, as YYYY-MM-DD."""
    span = (LAST_DAY - FIRST_DAY).days + 1
    return [(FIRST_DAY + timedelta(days=i)).isoformat() for i in range(span)]


def _ingest_one(symbol: str, day: str) -> dict:
    """Fetch and store one symbol-day. Reports failures rather than raising."""
    if _STOP:
        return {"status": "stopped", "rows": 0, "bytes": 0}
    out = _out_path(symbol, day)
    try:
        frame = archive.fetch_to_frame(MARKET, "daily", DATASET, symbol, day)
    except Exception as exc:
        return {"status": "error", "key": f"{symbol} {day}",
                "detail": f"{type(exc).__name__}: {exc}"}
    if frame is None or frame.empty:
        return {"status": "absent", "rows": 0, "bytes": 0}
    write_table(pa.Table.from_pandas(frame, preserve_index=False), out, sort_by="ts")
    return {"status": "ok", "rows": len(frame), "bytes": out.stat().st_size}


def main() -> None:
    """Fetch every published bookTicker day, resuming from what is already on disk."""
    started = time.time()
    _install_signal_handlers()

    root = DIR_DATA / VENUE / LAYER / MARKET / DATASET
    stale = clear_stale_temps(root) if root.exists() else []
    if stale:
        print(f"Removed {len(stale)} temporary partition(s) from an interrupted run")

    all_tasks = [(s, d) for s in SYMBOLS for d in _days()]
    tasks, done, repaired = _prune_damaged(all_tasks)

    print(f"bookTicker: {len(SYMBOLS)} symbols x {len(_days())} days = {len(all_tasks):,} files")
    print(f"Window {FIRST_DAY} to {LAST_DAY} (publication ceased; window is fixed)")
    print(f"Already complete {done:,}   damaged and requeued {repaired:,}   "
          f"remaining {len(tasks):,}")
    print(f"Workers {WORKERS}. Safe to interrupt: re-run to resume.\n", flush=True)

    if not tasks:
        print("Nothing to do.")
        return

    counts = {"ok": 0, "absent": 0, "error": 0, "stopped": 0}
    rows = written = 0
    errors = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_ingest_one, s, d): (s, d) for s, d in tasks}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            counts[r["status"]] += 1
            rows += r.get("rows", 0)
            written += r.get("bytes", 0)
            if r["status"] == "error":
                errors.append((futures[fut], r["detail"]))
            # Once stopping, the remaining futures return immediately and would
            # otherwise emit hundreds of identical progress lines while draining.
            if r["status"] == "stopped":
                continue
            if i % 5 == 0 or i == len(tasks):
                elapsed = time.time() - started
                finished = counts["ok"]
                eta = ((len(tasks) - i) / max(finished / elapsed, 1e-9) / 3600
                       if finished else float("nan"))
                print(f"  {i:>5,}/{len(tasks):,}  ok={counts['ok']:,} "
                      f"err={counts['error']:,} stopped={counts['stopped']:,}  "
                      f"{rows/1e6:>8,.1f}m rows  {written/1e9:>6.2f} GB  "
                      f"eta {eta:>5.1f}h", flush=True)

    total_done = done + counts["ok"]
    print(f"\nRESULT  this run wrote {counts['ok']:,}   absent {counts['absent']:,}   "
          f"errors {counts['error']:,}   skipped after stop {counts['stopped']:,}")
    print(f"  dataset now {total_done:,}/{len(all_tasks):,} files complete "
          f"({total_done/len(all_tasks):.1%})")
    print(f"  this run: {rows:,} rows, {written/1e9:.2f} GB, "
          f"{(time.time()-started)/3600:.2f} h")
    if total_done < len(all_tasks):
        print("  INCOMPLETE. Re-run this script to continue from here.")
    for key, detail in errors[:20]:
        print(f"  ERROR {key}: {detail}")


if __name__ == "__main__":
    main()
