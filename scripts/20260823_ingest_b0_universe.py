"""Fetch daily bars for the B0 pairs-trading universe into the standard tree.

Responsibility: read the frozen B0 universe, fetch each name's full daily
history and write it into the same partition layout every other equity dataset
uses, so B0 reads through common/paths.py like A0 does. Idempotent: a name
already stored up to the last close is skipped, so the script may be rerun and
interrupted freely.

Out of scope: the fetch and write logic itself, which belongs to
trading212/ingest/yahoo_bars.py and is the single shared implementation (this
script imports it and adds no second copy); partition paths, which belong to
common/paths.py; the universe definition, which is frozen in
data/reference/b0_universe_20260823.json and in
research/prereg/20260823_b0_statarb_prereg.md section 2.

Public functions:
    load_universe()   Tickers of the frozen B0 universe.
    main()            Fetch what is missing and print a report.

Constants:
    UNIVERSE_JSON  Path  Frozen universe, 502 names. Source: S&P 500 constituents
                         intersected with Trading 212 tradeable US equities.
    GROUP          str   "us_equity", the partition group these names belong to.
    STALE_DAYS     int   A name whose newest stored bar is younger than this many
                         calendar days is treated as current and skipped.

Inputs:
    data/reference/b0_universe_20260823.json
    Yahoo through trading212/ingest/yahoo_bars.py
Outputs:
    data/t212/curated/us_equity/<ticker>/1d/<ticker>_<year>.parquet

Change log:
    2026-08-23  Created for the B0 statistical-arbitrage study.
"""

from __future__ import annotations

__all__ = ["load_universe", "main"]

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trading212.ingest import yahoo_bars as yb                    # noqa: E402

UNIVERSE_JSON = ROOT / "data" / "reference" / "b0_universe_20260823.json"
GROUP = "us_equity"
STALE_DAYS = 4


def load_universe() -> list[str]:
    """Tickers of the frozen B0 universe, ascending."""
    payload = json.loads(UNIVERSE_JSON.read_text())
    return sorted(m["ticker"] for m in payload["members"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None,
                        help="fetch at most this many missing names")
    parser.add_argument("--force", action="store_true",
                        help="refetch even names that look current")
    args = parser.parse_args()

    tickers = load_universe()
    cutoff = pd.Timestamp(date.today() - timedelta(days=STALE_DAYS), tz="UTC")
    todo = []
    for ticker in tickers:
        newest = yb.latest_stored(GROUP, ticker, "1d")
        if args.force or newest is None or newest < cutoff:
            todo.append(ticker)
    if args.limit:
        todo = todo[:args.limit]
    print(f"universe {len(tickers)}, to fetch {len(todo)}", flush=True)

    ok = failed = 0
    bytes_written = 0
    errors: list[tuple[str, str]] = []
    for i, ticker in enumerate(todo, 1):
        try:
            frame = yb.fetch_interval(ticker, "1d", None, None)
            if frame.empty:
                raise ValueError("empty frame")
            _, size = yb.write_daily(GROUP, ticker, frame)
            bytes_written += size
            ok += 1
            if i % 25 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}  ok={ok} failed={failed} "
                      f"{bytes_written/1e6:.1f} MB", flush=True)
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            errors.append((ticker, str(exc)[:90]))
        time.sleep(yb.PACE_SEC)

    print(f"\ndone: ok={ok} failed={failed} written={bytes_written/1e6:.1f} MB")
    if errors:
        print(f"failures ({len(errors)}):")
        for ticker, msg in errors[:30]:
            print(f"  {ticker}: {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
