"""Phase 4: the discontinued bookTicker archive — the closest thing to free L2.

bookTicker is event-driven best bid and ask WITH quantities, roughly 300 updates
per second. It is the only free tick-level top-of-book data that will ever exist
for Binance: publication stopped on 2024-03-30 for USD-M and 2024-10-14 for
COIN-M, with no official statement, so the window is fixed and will not grow.

Because it is finite and already dead, it is worth taking in full now. Nothing
about it improves by waiting, and five other datasets in this archive have gone
dark the same way without notice.

Daily files are used rather than monthly. A monthly file is 4-6 GB compressed and
expands past 20 GB in memory; a daily file peaks around 1 GB, which one machine
can hold across several workers.

Public functions:
    main()   Run the bookTicker ingest
"""

from __future__ import annotations

__all__ = ["main"]

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow as pa

from common.paths import DIR_DATA
from common.store import write_table
from crypto_trading.ingest import binance_archive as archive

VENUE, LAYER, MARKET, DATASET = "binance", "curated", "um", "bookTicker"

# Four workers rather than eight: each holds about 1 GB while parsing, and the
# job is bandwidth-bound anyway, so more workers buy memory pressure not speed.
WORKERS = 4

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
FIRST_DAY = date(2023, 5, 16)     # first published day, verified by listing
LAST_DAY = date(2024, 3, 30)      # publication ceased, verified by listing


def _days() -> list[str]:
    """Every date in the published window, as YYYY-MM-DD."""
    span = (LAST_DAY - FIRST_DAY).days + 1
    return [(FIRST_DAY + timedelta(days=i)).isoformat() for i in range(span)]


def _ingest_one(symbol: str, day: str) -> dict:
    """Fetch and store one symbol-day. Reports failures rather than raising."""
    out = (DIR_DATA / VENUE / LAYER / MARKET / DATASET / symbol / DATASET
           / f"year={day[:4]}" / f"{day.replace('-', '')}.parquet")
    if out.exists():
        return {"status": "skip", "rows": 0, "bytes": out.stat().st_size}
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
    """Fetch every published bookTicker day for the configured symbols."""
    started = time.time()
    tasks = [(s, d) for s in SYMBOLS for d in _days()]
    print(f"bookTicker: {len(SYMBOLS)} symbols x {len(_days())} days = {len(tasks):,} files")
    print(f"Window {FIRST_DAY} to {LAST_DAY} (publication ceased; window is fixed)")
    print(f"Workers {WORKERS}\n", flush=True)

    counts = {"ok": 0, "skip": 0, "absent": 0, "error": 0}
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
            if i % 10 == 0 or i == len(tasks):
                elapsed = time.time() - started
                eta = (len(tasks) - i) / max(i / elapsed, 1e-9) / 3600
                print(f"  {i:>5,}/{len(tasks):,}  ok={counts['ok']:,} err={counts['error']:,}  "
                      f"{rows/1e9:>6.2f}bn rows  {written/1e9:>6.2f} GB  "
                      f"eta {eta:>4.1f}h", flush=True)

    print(f"\nRESULT  written {counts['ok']:,}  skipped {counts['skip']:,}  "
          f"absent {counts['absent']:,}  errors {counts['error']:,}")
    print(f"  {rows:,} rows   {written/1e9:.2f} GB parquet   "
          f"{(time.time()-started)/3600:.1f} h")
    for key, detail in errors[:20]:
        print(f"  ERROR {key}: {detail}")


if __name__ == "__main__":
    main()
