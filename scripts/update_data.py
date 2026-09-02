"""Manual daily update: bring every dataset from its last stored point to current.

Responsibility: refresh both data trees in one manually invoked pass and print a
report. The run may be repeated as often as wanted and interrupted at any point:
each dataset is examined for what it already holds and only the gap is fetched,
partitions are written atomically so a stopped run leaves no half-written file,
and any temporary partition left by a previous interruption is cleared before
work starts.

Two update strategies are used, chosen by what the source guarantees. Equity
refetches the available window in full for each interval, because adjusted
prices are retroactive: a split rewrites every historical bar, so appending
would leave the series inconsistent across the split date. A cheap probe first
compares the latest stored daily bar against the market and skips a ticker with
nothing new, so a run on a quiet day costs one request per ticker. Crypto
fetches only the missing days or months, because the archive is immutable once
published, so what is already stored never changes and refetching it would waste
a link measured at 1.6 MB/s.

Monthly coverage stops at the last closed month, so the tail is filled from daily
files until the monthly file is published. When that monthly file arrives, the
daily partitions it supersedes are deleted, because leaving both would
double-count on read.

bookTicker is not updated. Publication stopped on 2024-03-30 for USD-M and on
2024-10-14 for COIN-M, so its window is fixed and complete; its loader is
scripts/20260819_ingest_crypto_bookticker.py.

Out of scope: the equity fetch and write logic itself, which belongs to
trading212/ingest/yahoo_bars.py and is the single implementation shared with the
initial ingest; archive URL construction and download, which belong to
crypto_trading/ingest/binance_archive.py; partition path construction, which
belongs to common/paths.py; atomic writing and temporary-partition cleanup,
which belong to common/store.py; the committed rebuild manifest, which belongs
to scripts/build_data_manifest.py.

The B0 pairs-trading universe is verified in its own pass at the daily interval
only; pairs trading needs no intraday bar, so fetching the five-interval set for
it would multiply the request count for no research value. Its membership is
frozen in data/reference/b0_universe_1500_20260823.json and is never redefined
here; the initial load is scripts/20260823_ingest_b0_universe.py, which shares
this module's fetch path. Pass --no-b0 to skip that pass.

That pass is a completeness guarantee rather than a second download. B0 names
are stored in the same us_equity group the equity pass walks, and that pass now
discovers its symbols from disk, so a B0 name already on disk was refreshed
minutes earlier and is skipped here. What this pass adds is the names the disk
walk cannot see: a frozen member that has never been fetched, or one that has
gone stale. On a healthy lake it makes no requests at all and simply reports
that every frozen member is present.

Four passes run in order: crypto, equity, the B0 membership check, and the A1
ranking table. The last one ranks what the first three left on disk, so it must
run after them and it refuses to rank a session the pool does not fully cover.

Flags: --no-crypto, --no-equity, --no-b0 (skip the frozen-membership
verification pass) and --no-a1 (skip the ranking pass).

Public functions:
    main()   Update crypto, equity, the B0 universe and the A1 ranking table,
             then print the report.

Constants:
    B0_UNIVERSE_JSON  Path  Frozen B0 universe, 1500 names (S&P Composite 1500).
    B0_STALE_DAYS     int   A B0 name whose newest stored daily bar is younger
                            than this many calendar days is treated as current.
    DAILY_WINDOW_DAYS int   Days fetched by the daily probe, 730.
    FAILURE_CIRCUIT_BREAK int Consecutive failures that stop the equity pass.
    US_CLOSE_LOCAL_HOUR int Exchange-local hour of the regular close, 16.
    US_CLOSE_GRACE_MIN  int Minutes after it before a daily bar counts as
                            final, 20. Below it the day's row is dropped, so a
                            session in progress cannot be stored as a
                            finished bar.
    CRYPTO_JOBS             list Tuples of (market, freq, dataset, period,
                                 symbols) for the crypto datasets that are still
                                 published.
    VENUE                   str  Data-source slug written under data/,
                                 "binance".
    LAYER                   str  Storage layer written, "curated".
    PUBLISH_LAG_DAYS        int  Days held back from the daily tail, 2. The
                                 archive publishes a day at T+1 and the
                                 watermark is not uniform across datasets, so a
                                 couple of days at the tail are expected to be
                                 absent rather than missing.
    PUBLISH_LAG_MONTH_DAYS  int  Day of month up to which the previous month is
                                 still re-requested, 10. A month's file lands on
                                 the first Monday after it closes, so the
                                 previous month is worth re-requesting only
                                 during the first stretch of the current month.

Inputs:
    Binance public archive, through binance_archive.fetch_to_frame().
    Yahoo bars, through trading212.ingest.yahoo_bars.
    Existing partitions under data/binance/curated/ and data/t212/curated/, for
        the stored-stamp and latest-timestamp probes.
Outputs:
    data/binance/curated/<market>/<dataset>/<symbol>/<leaf>/year=YYYY/<stamp>.parquet
    data/t212/curated/<group>/<symbol>/1d/<symbol>_<year>.parquet
    data/t212/curated/<group>/<symbol>/<interval>/<symbol>_<start>_<end>_<interval>.parquet
    Daily crypto partitions superseded by a newly written monthly partition are
        deleted.
    stdout carries the per-dataset progress lines and the summary.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["main"]

import contextlib
import fcntl
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pyarrow as pa

from common.paths import (DIR_DATA, binance_partition_dir,
                          binance_partition_path, equity_curated_root)
from common.store import clear_stale_temps, write_table
from crypto_trading.ingest import binance_archive as archive
from trading212.ingest import a1_rank
from trading212.ingest import yahoo_bars as yb

# B0 pairs-trading universe: daily bars only. Frozen definition and provenance
# live in the JSON itself; this module never redefines the list. The JSON holds
# 1500 members; an earlier comment here said 502, which was the large-cap bucket
# alone and did not match either the file or load_universe().
B0_UNIVERSE_JSON = DIR_DATA / "reference" / "b0_universe_1500_20260823.json"
B0_STALE_DAYS = 4

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

# How much later a fetched history may start before it is judged truncated rather
# than merely different. A few days absorbs a genuine first-listing correction;
# anything beyond that is a partial response and the stored copy is kept.
TRUNCATION_TOLERANCE = pd.Timedelta(days=7)

# Window fetched for the daily pass. Two years covers the current calendar year
# in full, which is all that gets rewritten when prices were not restated, and
# still reaches far enough back to anchor the restatement check.
DAILY_WINDOW_DAYS = 730

# Consecutive symbol failures that mean the source has stopped serving rather
# than that one symbol is bad. Grinding on past this point wastes hours and
# hardens the throttle.
FAILURE_CIRCUIT_BREAK = 25

# US regular close, exchange-local, and how long after it a daily bar is
# treated as final. Yahoo answers a mid-session request with a row for the
# session in progress; stored, that half-formed bar is indistinguishable from a
# finished one and the skip rule then never replaces it. 1,475 symbols carried
# such a row from 2026-08-31. A half-day session closes at 13:00 and its bar is
# therefore final well before this cut-off too.
US_CLOSE_LOCAL_HOUR = 16
US_CLOSE_GRACE_MIN = 20
EXCHANGE_TZ = "America/New_York"


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
    """Return the day labels to fetch for a daily dataset, excluding what is stored.

    Days already on disk are dropped in both modes. An archive day is immutable
    once published, so refetching one buys nothing; the earlier version skipped
    that filter whenever `since` was given, which made every run re-download the
    whole current month. On a link measured at 1.6 MB/s that was roughly 1.3 GB
    and thirteen minutes thrown away per run.

    Args:
        have: Date labels already stored, in both "YYYY-MM-DD" and "YYYY-MM" form.
        since: Start the scan here instead of continuing from the newest stored
            day. Used to cover the stretch no monthly file spans yet, where the
            newest stored day is not the right anchor.
    """
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
    span = ((start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1))
    return [day for day in span if day not in have]


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


def _incomplete_from() -> date | None:
    """The first exchange-local date whose daily bar may still be forming.

    None means every date the source can return is final, so nothing is
    dropped. Otherwise it is today's exchange-local date: today's bar is the
    only one that can still be in progress, and the guard is a strict
    inequality, so yesterday and everything before it are kept.

    Deliberately conservative. Dropping a bar that was in fact final costs one
    day of latency and the next run picks it up; storing one that was not
    final silently corrupts every daily consumer of that symbol, and the skip
    rule means it is never overwritten.
    """
    now = pd.Timestamp.now(tz=EXCHANGE_TZ)
    final_at = now.normalize() + pd.Timedelta(hours=US_CLOSE_LOCAL_HOUR,
                                              minutes=US_CLOSE_GRACE_MIN)
    return None if now >= final_at else now.date()


def _us_session_open() -> bool:
    """Whether a regular US session is open right now, from the venue calendar.

    Read-only and best effort: a missing or stale calendar cache answers
    False, because the clock guard in _incomplete_from() already protects the
    data and refusing to update on an unreadable cache would be worse than the
    risk it removes.
    """
    try:
        from common.paths import execution_state_dir
        from trading212.execution import instruments as ins
        cache = execution_state_dir("t212", "live") / "exchange_calendar.json"
        if not cache.is_file():
            return False
        events = ins.session_events(ins.load_calendar(cache),
                                    ins.US_SCHEDULE_ID_NASDAQ)
        return ins.market_is_open(events, pd.Timestamp.now(tz="UTC"))
    except Exception:
        return False


@contextlib.contextmanager
def _store_lock():
    """Hold the curated-store lock for ONE write.

    trading212/execution/market_data.py refresh_bars takes the same lock and
    blocks on it. Holding it for a whole three-hour pass would park the live
    15:30 refresh until long after its submission instant and abort the
    session; holding it per write costs the live process at most one symbol's
    write while still serializing partition writes and stale-sibling deletes.
    """
    path = equity_curated_root() / ".refresh.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _update_a1_rank() -> dict:
    """Fourth pass: the A1 ranking table for the session that just closed.

    Runs after the daily bars are current, because it ranks what is on disk.
    It refuses to rank a session the pool does not cover
    (trading212/ingest/a1_rank.py MIN_SESSION_COVERAGE) rather than writing a
    table built from whichever names happened to be refreshed.
    """
    print("\n" + "=" * 96)
    print("A1 RANK  cross-sectional ranking of the frozen pool, one session")
    print("=" * 96, flush=True)
    try:
        out = a1_rank.run()
    except Exception as exc:
        print(f"  not written: {exc}", flush=True)
        return {"written": False, "error": str(exc)}
    if out.get("written"):
        print(f"  {out['session']}: {out['rows']} candidates, "
              f"{out['eligible']} eligible, {out.get('ranked')} ranked",
              flush=True)
    else:
        print(f"  {out['session']}: already present, nothing to do", flush=True)
    return out


def _update_equity() -> dict:
    """Refresh every stored equity symbol, maintaining only the intervals it has."""
    stats = {"updated": 0, "skipped": 0, "readjusted": 0, "files": 0, "rows": 0,
             "bytes": 0, "failed": [], "truncated": [], "full_fetch_failed": [],
             "aborted": False}
    consecutive_failures = 0
    drop_from = _incomplete_from()
    print("\n" + "=" * 96)
    print("EQUITY  Yahoo, symbols discovered from disk")
    print("=" * 96, flush=True)
    if _us_session_open():
        print("  A regular US session is open; the daily pass would fetch "
              "half-formed bars for every symbol. Run it after the close.",
              flush=True)
        stats["aborted"] = True
        return stats
    if drop_from is not None:
        print(f"  Today ({drop_from}) is not final yet; its rows are dropped "
              f"and picked up on the next run after the close.", flush=True)

    for group in yb.UNIVERSE:
        symbols = yb.discover_symbols(group)
        configured = len(yb.UNIVERSE[group])
        print(f"\n[{group}] {len(symbols)} symbols "
              f"({configured} configured, {len(symbols) - configured} found on disk)",
              flush=True)

        for index, ticker in enumerate(symbols, 1):
            intervals = yb.stored_intervals(group, ticker)

            # The daily pass uses a two-year window, not the full history.
            # period="max" measured 9.55 seconds and succeeded on one attempt in
            # three, against 0.57 seconds and three-for-three at two years. Using
            # it as the daily probe made a fifteen-hundred-symbol pass take four
            # hours in the best case, and when Yahoo throttled it turned into a
            # multi-day retry storm because every symbol burned its full back-off
            # ladder. Full history is now fetched only when it is actually needed:
            # for a symbol never stored, or one whose prices were restated.
            frame = yb.fetch_interval(ticker, "1d", DAILY_WINDOW_DAYS, None,
                                      drop_from=drop_from)
            time.sleep(yb.PACE_SEC)
            if frame.empty:
                stats["failed"].append(f"{ticker}/1d")
                consecutive_failures += 1
                if consecutive_failures >= FAILURE_CIRCUIT_BREAK:
                    print(f"\n  {consecutive_failures} symbols failed in a row; "
                          f"the source is refusing traffic. Stopping rather than "
                          f"grinding through the rest. Re-run later.", flush=True)
                    stats["aborted"] = True
                    return stats
                continue
            consecutive_failures = 0

            stored_max = yb.latest_stored(group, ticker, "1d")
            if (stored_max is not None and frame["ts"].max() <= stored_max
                    and intervals == ["1d"]):
                stats["skipped"] += 1
                if index % 100 == 0:
                    print(f"  ...{index}/{len(symbols)}  updated {stats['updated']}  "
                          f"skipped {stats['skipped']}  failed {len(stats['failed'])}",
                          flush=True)
                continue

            # Decide whether history was restated by comparing one bar from the
            # far end of the window against the stored copy of that same bar.
            anchor_ts = frame["ts"].min()
            fetched_anchor = float(frame.loc[frame["ts"] == anchor_ts, "close"].iloc[0])
            stored_anchor = yb.stored_close_at(group, ticker, anchor_ts)

            if stored_max is None:
                need_full, reason = True, "never stored"
            elif stored_anchor is None:
                # The anchor date is absent from storage, so the two copies cannot
                # be compared. Refetching in full is the safe reading.
                need_full, reason = True, "anchor absent"
            elif abs(fetched_anchor - stored_anchor) > max(
                    1e-6, abs(stored_anchor) * 1e-6):
                need_full, reason = True, "prices restated"
            else:
                need_full, reason = False, ""

            if need_full:
                full = yb.fetch_interval(ticker, "1d", None, None,
                                         drop_from=drop_from)
                time.sleep(yb.PACE_SEC)
                if full.empty:
                    # The window fetch succeeded, so the symbol is fine and only
                    # the heavy request failed. Write what is in hand rather than
                    # losing the day, and report it.
                    stats["full_fetch_failed"].append(f"{ticker} ({reason})")
                    years = {int(frame["ts"].max().year)}
                else:
                    frame = full
                    years = None
                    if reason == "prices restated":
                        stats["readjusted"] += 1
            else:
                years = {int(frame["ts"].max().year)}

            with _store_lock():
                files, size = yb.write_daily(group, ticker, frame, years=years)
            stats["files"] += files
            stats["rows"] += len(frame)
            stats["bytes"] += size

            for interval in intervals:
                if interval == "1d":
                    continue
                spec = next(((lb, ch) for iv, lb, ch in yb.INTERVALS if iv == interval),
                            None)
                if spec is None:
                    continue
                part = yb.fetch_interval(ticker, interval, spec[0], spec[1])
                time.sleep(yb.PACE_SEC)
                if part.empty:
                    stats["failed"].append(f"{ticker}/{interval}")
                    continue
                with _store_lock():
                    files, size = yb.write_intraday(group, ticker, interval,
                                                    part)
                stats["files"] += files
                stats["rows"] += len(part)
                stats["bytes"] += size

            stats["updated"] += 1
            if index % 100 == 0 or len(symbols) < 50:
                print(f"  ...{index}/{len(symbols)}  updated {stats['updated']}  "
                      f"skipped {stats['skipped']}  failed {len(stats['failed'])}",
                      flush=True)
    return stats


def _b0_tickers() -> list[str]:
    """Return the frozen B0 membership, normalized to the price source's spelling.

    The frozen list carries Wikipedia spellings, which write a share class with a
    dot: BRK.B, BF.B. Yahoo writes the same instruments with a hyphen. Passing the
    dotted form through fetches nothing, so those two names would fail on every
    run while appearing to be genuinely missing data. Normalizing here keeps the
    frozen file untouched, which matters because it is the preregistered universe
    definition and must not be edited to suit a downstream quirk.
    """
    payload = json.loads(B0_UNIVERSE_JSON.read_text())
    return sorted({m["ticker"].replace(".", "-") for m in payload["members"]})


def _update_b0_universe() -> dict:
    """Ensure every frozen B0 member has current daily bars.

    Runs after the equity pass, which walks the same us_equity group from disk and
    will already have refreshed any member stored there. This pass therefore
    fetches only what that walk could not see, and on a healthy lake makes no
    requests at all.

    Returns:
        Counts under the keys main() prints: tickers, skipped, files, rows, bytes,
        failed, plus missing_from_disk for members that had no stored bars.
    """
    stats = {"tickers": 0, "skipped": 0, "files": 0, "rows": 0, "bytes": 0,
             "failed": [], "missing_from_disk": []}
    print("\n" + "=" * 96)
    print("B0 UNIVERSE  frozen membership, daily only, verification pass")
    print("=" * 96, flush=True)

    if not B0_UNIVERSE_JSON.is_file():
        print(f"  {B0_UNIVERSE_JSON} not found; skipping.", flush=True)
        stats["failed"].append("universe json missing")
        return stats

    tickers = _b0_tickers()
    drop_from = _incomplete_from()
    cutoff = pd.Timestamp(date.today() - timedelta(days=B0_STALE_DAYS), tz="UTC")
    todo = []
    for ticker in tickers:
        newest = yb.latest_stored("us_equity", ticker, "1d")
        if newest is None:
            stats["missing_from_disk"].append(ticker)
            todo.append(ticker)
        elif newest < cutoff:
            todo.append(ticker)
        else:
            stats["skipped"] += 1

    print(f"  {len(tickers)} frozen members, {stats['skipped']} already current, "
          f"{len(todo)} to fetch "
          f"({len(stats['missing_from_disk'])} never stored)", flush=True)

    for index, ticker in enumerate(todo, 1):
        frame = yb.fetch_interval(ticker, "1d", None, None,
                                  drop_from=drop_from)
        time.sleep(yb.PACE_SEC)
        if frame.empty:
            stats["failed"].append(ticker)
            continue
        with _store_lock():
            files, size = yb.write_daily("us_equity", ticker, frame)
        stats["tickers"] += 1
        stats["files"] += files
        stats["rows"] += len(frame)
        stats["bytes"] += size
        if index % 25 == 0 or index == len(todo):
            print(f"  ...{index}/{len(todo)}  fetched {stats['tickers']}  "
                  f"failed {len(stats['failed'])}", flush=True)
    return stats


def main() -> None:
    """Update crypto and equity, then report."""
    started = time.time()
    removed = clear_stale_temps(DIR_DATA)
    if removed:
        print(f"Removed {len(removed)} temporary partition(s) from an interrupted run\n")

    crypto = {"fetched": 0, "bytes": 0, "absent": 0, "errors": 0,
              "superseded": 0} if "--no-crypto" in sys.argv else _update_crypto()
    equity = _update_equity() if "--no-equity" not in sys.argv else \
        {"updated": 0, "skipped": 0, "readjusted": 0, "files": 0, "rows": 0,
         "bytes": 0, "failed": [], "truncated": []}
    b0 = _update_b0_universe() if "--no-b0" not in sys.argv else \
        {"tickers": 0, "skipped": 0, "files": 0, "rows": 0, "bytes": 0,
         "failed": [], "note": "skipped by --no-b0"}
    a1 = _update_a1_rank() if "--no-a1" not in sys.argv else \
        {"written": False, "note": "skipped by --no-a1"}

    print("\n" + "=" * 96)
    print("SUMMARY")
    print("=" * 96)
    print(f"  crypto  +{crypto['fetched']} files  "
          f"{crypto['bytes']/1e6:,.1f} MB  "
          f"absent {crypto['absent']}  errors {crypto['errors']}  "
          f"superseded dailies removed {crypto['superseded']}")
    print(f"  equity  {equity['updated']} updated, {equity['skipped']} already current, "
          f"{equity['readjusted']} had a retroactive adjustment (full rewrite)")
    print(f"          {equity['files']} files  {equity['rows']:,} rows  "
          f"{equity['bytes']/1e6:,.1f} MB")
    if equity.get("aborted"):
        print("  equity ABORTED early: too many consecutive failures. Re-run later.")
    if equity.get("full_fetch_failed"):
        print(f"  equity full-history refetch failed for "
              f"{len(equity['full_fetch_failed'])} symbol(s); current year written "
              f"from the window instead: {equity['full_fetch_failed'][:10]}")
    if equity["truncated"]:
        print(f"  equity truncated fetches ({len(equity['truncated'])}) -- stored history "
              f"kept, only the current year refreshed:")
        for line in equity["truncated"][:10]:
            print(f"    {line}")
        if len(equity["truncated"]) > 10:
            print(f"    ... and {len(equity['truncated'])-10} more")
    if equity["failed"]:
        shown = equity["failed"][:20]
        print(f"  equity failures ({len(equity['failed'])}): {shown}"
              + (" ..." if len(equity["failed"]) > 20 else ""))
        print("  Re-run to retry: these are usually Yahoo throttling, not missing data.")
    print(f"  b0      {b0['tickers']} tickers updated, {b0['skipped']} already current  "
          f"{b0['files']} files  {b0['rows']:,} rows  {b0['bytes']/1e6:,.1f} MB")
    if b0.get("missing_from_disk"):
        print(f"  b0 members never stored before this run "
              f"({len(b0['missing_from_disk'])}): {b0['missing_from_disk'][:12]}")
    if b0.get("failed"):
        print(f"  b0 failures ({len(b0['failed'])}): {b0['failed'][:12]}")
    if a1.get("written"):
        print(f"  a1      ranking table for {a1['session']}: "
              f"{a1['eligible']} of {a1['rows']} eligible")
    elif a1.get("error"):
        print(f"  a1      NOT written: {a1['error']}")
    else:
        print(f"  a1      {a1.get('note', 'already present')}")
    print(f"  elapsed {(time.time()-started)/60:.1f} min")


if __name__ == "__main__":
    main()
