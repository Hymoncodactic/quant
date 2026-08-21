"""Manual daily update: bring every dataset from its last stored point to current.

Run this as often as wanted. It is safe to interrupt and safe to repeat: each
dataset is examined for what it already holds and only the gap is fetched, and
partitions are written atomically so a stopped run leaves no half-written file.

Two update strategies are used, chosen by what the source guarantees.

    Equity   The available window is refetched in full for each interval.
             Adjusted prices are retroactive: a split rewrites every historical
             bar, so appending would leave the series inconsistent across the
             split date. A cheap probe first compares the latest stored daily
             bar against the market, and a ticker with nothing new is skipped
             entirely, so a run on a quiet day costs one request per ticker.

    Crypto   Only the missing days or months are fetched. The archive is
             immutable once published, so what is already stored never changes
             and refetching it would waste a link measured at 1.6 MB/s.

bookTicker is not updated. Binance stopped publishing it on 2024-03-30 for USD-M
and 2024-10-14 for COIN-M, so its window is fixed and complete.

Public functions:
    main()   Update everything and print a report
"""

from __future__ import annotations

__all__ = ["main"]

import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pyarrow as pa

from common.paths import DIR_DATA, binance_partition_dir, binance_partition_path
from common.store import clear_stale_temps, write_table
from crypto_trading.ingest import binance_archive as archive
from trading212.ingest import yahoo_bars as yb

# Crypto datasets that are still published. Each entry is
# (market, freq, dataset, period, symbols).
CRYPTO_JOBS = [
    ("spot", "monthly", "klines", "1m",
     ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
      "DOGEUSDT", "TRXUSDT", "PAXGUSDT", "USDCUSDT"]),
    ("spot", "monthly", "klines", "1d",
     ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
      "DOGEUSDT", "TRXUSDT", "PAXGUSDT", "USDCUSDT"]),
    ("um", "monthly", "fundingRate", None,
     ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT"]),
    ("um", "daily", "metrics", None,
     ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT"]),
    ("um", "daily", "bookDepth", None, ["BTCUSDT", "ETHUSDT"]),
]

VENUE = "binance"
LAYER = "curated"

# The archive publishes a day at T+1 and a month on the first Monday after it
# closes, and the watermark is not uniform across datasets, so a couple of days
# at the tail are expected to be absent rather than missing.
PUBLISH_LAG_DAYS = 2

# A month's file lands on the first Monday after it closes, so the previous month
# is worth re-requesting only during the first stretch of the current month.
PUBLISH_LAG_MONTH_DAYS = 10


def _crypto_out(market: str, dataset: str, symbol: str, period: str | None,
                date_str: str) -> Path:
    """Destination partition for one crypto archive file."""
    return binance_partition_path(market, dataset, symbol, period, date_str)


def _existing_stamps(market: str, dataset: str, symbol: str,
                     period: str | None) -> set[str]:
    """Return the date labels already stored for one crypto dataset."""
    folder = binance_partition_dir(market, dataset, symbol, period)
    if not folder.is_dir():
        return set()
    return {p.stem for p in folder.rglob("*.parquet")}


def _wanted_months(have: set[str]) -> list[str]:
    """Return the month labels to fetch for a monthly dataset.

    A monthly file is immutable once the month has closed and been published, so
    refetching it wastes a link measured at 1.6 MB/s. Only three cases are worth
    a request: a month that is missing outright, the current month, and the
    previous month while it is still early enough that its file may have been
    published late. The archive publishes a month on the first Monday after it
    closes.
    """
    today = datetime.now(timezone.utc).date()
    current = date(today.year, today.month, 1)
    previous = (current - timedelta(days=1)).replace(day=1)

    wanted = {current.strftime("%Y-%m")}
    if today.day <= PUBLISH_LAG_MONTH_DAYS:
        wanted.add(previous.strftime("%Y-%m"))

    # Any month between the oldest stored and now that is simply absent.
    if have:
        cursor = current
        oldest = min(have)
        while cursor.strftime("%Y-%m") >= oldest:
            label = cursor.strftime("%Y-%m")
            if label not in have:
                wanted.add(label)
            cursor = (cursor - timedelta(days=1)).replace(day=1)
    return sorted(wanted)


def _wanted_days(have: set[str], since: date | None = None) -> list[str]:
    """Return the day labels to fetch for a daily dataset."""
    today = datetime.now(timezone.utc).date()
    if since is not None:
        start = since
    else:
        daily = [h for h in have if len(h) == 10]
        start = (date.fromisoformat(max(daily)) + timedelta(days=1) if daily
                 else today - timedelta(days=30))
    end = today - timedelta(days=PUBLISH_LAG_DAYS)
    if start > end:
        return []
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def _current_month_days(have: set[str]) -> list[str]:
    """Return the daily labels needed to cover the period no monthly file spans yet.

    Monthly files appear only after a month closes, so between the last published
    month and today there is a stretch with no monthly coverage at all. Daily
    files exist for that stretch and are fetched to fill it. When the monthly
    file for that period eventually arrives, the dailies it supersedes are
    removed by the caller.
    """
    months = sorted(h for h in have if len(h) == 7)
    if not months:
        return []
    last = date.fromisoformat(months[-1] + "-01")
    first_uncovered = (last + timedelta(days=32)).replace(day=1)
    return _wanted_days(have, since=first_uncovered)


def _drop_superseded_days(market: str, dataset: str, symbol: str,
                          period: str | None, month: str) -> int:
    """Delete daily partitions now covered by a newly written monthly partition."""
    folder = binance_partition_dir(market, dataset, symbol, period) / f"year={month[:4]}"
    removed = 0
    if folder.is_dir():
        for path in folder.glob(f"{month}-*.parquet"):
            path.unlink()
            removed += 1
    return removed


def _update_crypto() -> dict:
    """Fetch the crypto days and months that are absent."""
    stats = {"fetched": 0, "absent": 0, "errors": 0, "bytes": 0, "superseded": 0}
    print("=" * 96)
    print("CRYPTO  Binance archive, incremental")
    print("=" * 96, flush=True)
    for market, freq, dataset, period, symbols in CRYPTO_JOBS:
        for symbol in symbols:
            have = _existing_stamps(market, dataset, symbol, period)
            if freq == "monthly":
                want = [(freq, m) for m in _wanted_months(have)]
                # Monthly coverage stops at the last closed month, so the tail is
                # filled from daily files until the monthly one is published.
                want += [("daily", d) for d in _current_month_days(have)]
            else:
                want = [(freq, d) for d in _wanted_days(have)]
            if not want:
                print(f"  {market}/{dataset}/{period or '':<3} {symbol:<10} up to date "
                      f"({len(have)} files)", flush=True)
                continue
            got = 0
            for use_freq, stamp in want:
                out = _crypto_out(market, dataset, symbol, period, stamp)
                try:
                    frame = archive.fetch_to_frame(market, use_freq, dataset, symbol,
                                                   stamp, period)
                except Exception as exc:
                    stats["errors"] += 1
                    print(f"    ERROR {symbol} {stamp}: {type(exc).__name__}: {exc}",
                          flush=True)
                    continue
                if frame is None or frame.empty:
                    stats["absent"] += 1
                    continue
                write_table(pa.Table.from_pandas(frame, preserve_index=False), out,
                            sort_by=frame.columns[0])
                stats["fetched"] += 1
                stats["bytes"] += out.stat().st_size
                got += 1
                # A newly arrived monthly file supersedes the dailies that covered
                # the same period; leaving both would double-count on read.
                if len(stamp) == 7:
                    stats["superseded"] += _drop_superseded_days(
                        market, dataset, symbol, period, stamp)
            print(f"  {market}/{dataset}/{period or '':<3} {symbol:<10} "
                  f"+{got} new (checked {len(want)})", flush=True)
    return stats


def _update_equity() -> dict:
    """Refresh every equity interval whose ticker has moved."""
    stats = {"tickers": 0, "skipped": 0, "files": 0, "rows": 0, "bytes": 0,
             "failed": []}
    print("\n" + "=" * 96)
    print("EQUITY  Yahoo, full-window refresh of tickers that moved")
    print("=" * 96, flush=True)
    for group, members in yb.UNIVERSE.items():
        print(f"\n[{group}]", flush=True)
        for ticker, name in members.items():
            stored = yb.latest_stored(group, ticker, "1d")
            probe = yb.fetch_interval(ticker, "1d", 5, None)
            time.sleep(yb.PACE_SEC)
            if probe.empty:
                stats["failed"].append(f"{ticker}/probe")
                print(f"  {ticker:<10} probe failed", flush=True)
                continue
            if stored is not None and probe["ts"].max() <= stored:
                stats["skipped"] += 1
                print(f"  {ticker:<10} {name[:24]:<25} up to date "
                      f"({str(stored)[:10]})", flush=True)
                continue

            parts = []
            for interval, lookback, chunk in yb.INTERVALS:
                frame = yb.fetch_interval(ticker, interval, lookback, chunk)
                time.sleep(yb.PACE_SEC)
                if frame.empty:
                    stats["failed"].append(f"{ticker}/{interval}")
                    parts.append(f"{interval}:-")
                    continue
                files, size = (yb.write_daily(group, ticker, frame) if interval == "1d"
                               else yb.write_intraday(group, ticker, interval, frame))
                stats["files"] += files
                stats["rows"] += len(frame)
                stats["bytes"] += size
                parts.append(f"{interval}:{len(frame):,}")
            stats["tickers"] += 1
            print(f"  {ticker:<10} {name[:24]:<25} updated -> "
                  f"{'  '.join(parts)}", flush=True)
    return stats


def main() -> None:
    """Update crypto and equity, then report."""
    started = time.time()
    removed = clear_stale_temps(DIR_DATA)
    if removed:
        print(f"Removed {len(removed)} temporary partition(s) from an interrupted run\n")

    crypto = _update_crypto()
    equity = _update_equity()

    print("\n" + "=" * 96)
    print("SUMMARY")
    print("=" * 96)
    print(f"  crypto  +{crypto['fetched']} files  "
          f"{crypto['bytes']/1e6:,.1f} MB  "
          f"absent {crypto['absent']}  errors {crypto['errors']}  "
          f"superseded dailies removed {crypto['superseded']}")
    print(f"  equity  {equity['tickers']} tickers updated, {equity['skipped']} already current  "
          f"{equity['files']} files  {equity['rows']:,} rows  {equity['bytes']/1e6:,.1f} MB")
    if equity["failed"]:
        print(f"  equity failures ({len(equity['failed'])}): {equity['failed']}")
        print("  Re-run to retry: these are usually Yahoo throttling, not missing data.")
    print(f"  elapsed {(time.time()-started)/60:.1f} min")


if __name__ == "__main__":
    main()
