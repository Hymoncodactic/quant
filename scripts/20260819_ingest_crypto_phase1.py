"""One-off phase 1 crypto ingest: the cheap, high-value backbone of the Binance archive.

Responsibility: discover the archive's coverage for each configured job, download
every available file in parallel, convert it to the tuned parquet layout, and
write it under the curated layer. Every job is either the whole available history
or a deliberately bounded recent window; the heavy tick tapes are a later phase
and a separate decision. Failures are listed at the end rather than swallowed,
because a run that reports success while silently skipping days is worse than one
that fails loudly.

Instrument selection is by measured order-book depth within 10 basis points of
mid, not by headline 24-hour volume. The two rankings diverge violently: several
pairs in the volume top ten hold only single-digit thousands of dollars within
10bp, roughly a six-hundredth of BTCUSDT's depth.

Out of scope: incremental updates, which belong to scripts/update_data.py;
archive URL construction and download, which belong to
crypto_trading/ingest/binance_archive.py; atomic parquet writing, which belongs
to common/store.py; the bookTicker tape, which has its own loader in
scripts/20260819_ingest_crypto_bookticker.py.

Public functions:
    main()   Discover coverage per dataset, then fetch everything in parallel.

Constants:
    VENUE       str   Data-source slug written under data/, "binance".
    LAYER       str   Storage layer written, "curated".
    WORKERS     int   Concurrent download workers, 8.
    SPOT_PAIRS  list  Spot symbols: tiers 1 and 2 by measured 10bp depth, plus
                      tier 3 diversifier candidates. PAXGUSDT is the only
                      genuine cross-asset diversifier on the venue, and
                      USDCUSDT is kept as a peg-stress signal rather than as a
                      tradable instrument.
    PERP_PAIRS  list  Perpetual-futures symbols, which are the ones carrying the
                      derivatives datasets. Data only: UK retail is barred from
                      trading crypto derivatives by the FCA ban in force since
                      2021-01-06.
    JOBS        list  Tuples of (market, freq, dataset, period, symbols, start).
                      A start of None means the whole available history.

Inputs:
    Binance public archive, through binance_archive.available_dates() and
    binance_archive.fetch_to_frame().
Outputs:
    data/binance/curated/<market>/<dataset>/<symbol>/<leaf>/year=YYYY/<stamp>.parquet
    where <leaf> is the bar interval for the kline family and the dataset name
    otherwise.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["main"]

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pyarrow as pa

from common.paths import DIR_DATA
from common.store import write_table
from crypto_trading.ingest import binance_archive as archive

VENUE = "binance"
LAYER = "curated"
WORKERS = 8

# Tier 1 and 2 by measured depth, plus tier 3 diversifier candidates.
SPOT_PAIRS = [
    "BTCUSDT", "ETHUSDT",                          # tier 1, mandatory
    "SOLUSDT", "XRPUSDT", "BNBUSDT",               # tier 2
    "DOGEUSDT", "TRXUSDT",                         # tier 3, lowest major correlation
    "PAXGUSDT",                                    # gold token, the only real diversifier
    "USDCUSDT",                                    # peg-stress signal, not tradable
]

# Perpetual futures carry the derivatives datasets. Data only: UK retail is barred
# from trading crypto derivatives by the FCA ban in force since 6 January 2021.
PERP_PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT"]

# (market, freq, dataset, period, symbols, start) — start None means all history.
JOBS = [
    ("spot", "monthly", "klines", "1m", SPOT_PAIRS, None),
    ("spot", "monthly", "klines", "1d", SPOT_PAIRS, None),
    ("um", "monthly", "fundingRate", None, PERP_PAIRS, None),
    ("um", "daily", "metrics", None, PERP_PAIRS, "2024-01-01"),
    ("um", "daily", "bookDepth", None, ["BTCUSDT", "ETHUSDT"], "2026-01-01"),
]


def _out_path(market: str, dataset: str, symbol: str, period: str | None,
              date_str: str) -> Path:
    """Compose the Parquet destination, partitioned by year."""
    leaf = f"{period}" if period else dataset
    year = date_str[:4]
    return (DIR_DATA / VENUE / LAYER / market / dataset / symbol / leaf
            / f"year={year}" / f"{date_str}.parquet")


def _ingest_one(market, freq, dataset, symbol, period, date_str) -> dict:
    """Fetch, convert and write one archive file. Never raises: failures are reported."""
    out = _out_path(market, dataset, symbol, period, date_str)
    if out.exists():
        return {"status": "skip", "path": out, "rows": 0, "bytes": out.stat().st_size}
    try:
        frame = archive.fetch_to_frame(market, freq, dataset, symbol, date_str, period)
    except Exception as exc:
        return {"status": "error", "path": out, "detail": f"{type(exc).__name__}: {exc}"}
    if frame is None or frame.empty:
        return {"status": "absent", "path": out, "rows": 0, "bytes": 0}
    write_table(pa.Table.from_pandas(frame, preserve_index=False), out,
                sort_by=frame.columns[0])
    return {"status": "ok", "path": out, "rows": len(frame), "bytes": out.stat().st_size}


def main() -> None:
    """Discover coverage per dataset, then fetch everything in parallel."""
    started = time.time()
    tasks, summary = [], {}

    print("=" * 92)
    print("Discovering coverage (the archive's per-dataset, per-symbol coverage varies)")
    print("=" * 92)
    for market, freq, dataset, period, symbols, start in JOBS:
        for symbol in symbols:
            dates = archive.available_dates(market, freq, dataset, symbol, period)
            if start:
                dates = [d for d in dates if d >= start[:len(d)]]
            key = f"{market}/{dataset}" + (f"/{period}" if period else "")
            summary.setdefault(key, {"symbols": 0, "files": 0, "first": "", "last": ""})
            summary[key]["symbols"] += 1
            summary[key]["files"] += len(dates)
            if dates:
                first, last = dates[0], dates[-1]
                s = summary[key]
                s["first"] = min(s["first"], first) if s["first"] else first
                s["last"] = max(s["last"], last)
            print(f"  {market:<5} {dataset:<13} {period or '':<4} {symbol:<10} "
                  f"{len(dates):>5} files  {dates[0] if dates else '-'} .. {dates[-1] if dates else '-'}")
            for d in dates:
                tasks.append((market, freq, dataset, symbol, period, d))

    print(f"\nTotal {len(tasks):,} files queued across {len(summary)} datasets\n")
    print("=" * 92)
    print(f"Downloading with {WORKERS} workers")
    print("=" * 92)

    counts = {"ok": 0, "skip": 0, "absent": 0, "error": 0}
    rows_total = bytes_total = 0
    errors = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_ingest_one, *t): t for t in tasks}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            counts[r["status"]] += 1
            rows_total += r.get("rows", 0)
            bytes_total += r.get("bytes", 0)
            if r["status"] == "error":
                errors.append((futures[fut], r["detail"]))
            if i % 100 == 0 or i == len(tasks):
                rate = i / max(time.time() - started, 1)
                print(f"  {i:>6,}/{len(tasks):,}  ok={counts['ok']:,} skip={counts['skip']:,} "
                      f"absent={counts['absent']:,} err={counts['error']:,}  "
                      f"{bytes_total/1e6:>8,.0f} MB  {rate:.1f} files/s")

    print("\n" + "=" * 92)
    print("RESULT")
    print("=" * 92)
    print(f"  files written {counts['ok']:,}   skipped {counts['skip']:,}   "
          f"absent {counts['absent']:,}   errors {counts['error']:,}")
    print(f"  rows {rows_total:,}   parquet {bytes_total/1e9:.3f} GB   "
          f"elapsed {(time.time()-started)/60:.1f} min")
    if rows_total:
        print(f"  {bytes_total/rows_total:.2f} bytes per row")
    # Failures are listed rather than swallowed: a run that reports success while
    # silently skipping days is worse than one that fails loudly.
    for task, detail in errors[:20]:
        print(f"  ERROR {task}: {detail}")


if __name__ == "__main__":
    main()
