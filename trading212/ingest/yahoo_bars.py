"""Yahoo bar ingest for the Trading 212 equity universe.

Responsibility: fetch bars from Yahoo through yfinance, reduce them to the project
schema (ts, open, high, low, close, volume, quote_ccy) and persist them as parquet
partitions under data/t212/curated/. This is the single implementation of that logic:
the initial ingest and the incremental updater both call it, so the partition layout
and the naming rule cannot drift between them.

Out of scope: partition path construction, which belongs to common/paths.py; atomic
parquet writing, which belongs to common/store.py; the schedule, the progress report
and the choice of which tickers to refresh, which belong to scripts/update_data.py;
field, unit and time zone documentation, which belongs to docs/data/t212/DATA_SPEC.md;
trading decisions, which belong to trading212/strategy/.

Public functions:
    fetch_interval(ticker, interval, lookback, chunk)  Bars for one ticker/interval
    write_daily(group, ticker, frame)                  Store daily bars, one file per year
    write_intraday(group, ticker, interval, frame)     Store intraday bars, one file per month
    latest_stored(group, ticker, interval)             Newest timestamp already on disk
    discover_symbols(group)                            Configured symbols plus whatever is on disk
    stored_intervals(group, ticker)                    Which intervals already exist on disk
    earliest_bar(group, ticker)                        Oldest stored daily (date, close), for adjustment checks
    stored_close_at(group, ticker, ts)                 Stored daily close on one date, for anchor checks
    quote_currency(ticker)                             Exchange quote currency, cached
    exchange_tz(ticker)                                IANA zone of the listing

Constants:
    RETRY_BASE_SEC   float  Seconds to wait before retrying an empty or failed history
                            call, 8.0, doubled on each further attempt. Source: measured
                            on this host 2026-08-19. Yahoo throttles intermittently
                            rather than by capability, returning 17,000 rows for one
                            call and nothing for the next, so the cure is patience
                            between attempts rather than more attempts in quick
                            succession. Four attempts at a 2-second base left four
                            intervals empty, and the same requests succeeded on the
                            first try once given a longer gap.
    RETRY_ATTEMPTS   int    Attempts per history call, 6. Same measurement.
    PACE_SEC         float  Seconds of pause between the stitched 1m windows, 0.6.
                            Source unknown, needs verification.
    INTERVALS        list   Tuples of (interval, lookback in days or None for the full
                            listed history, days per request or None). Source: measured
                            against Yahoo 2026-08-19. Yahoo enumerates its valid
                            intervals when rejecting anything else: 1m, 2m, 5m, 15m,
                            30m, 60m, 90m, 1h, 4h, 1d, 5d, 1wk, 1mo, 3mo. No sub-minute
                            equity bar exists here at any price. Each interval carries
                            its own history limit, verified by request: 1m allows 8 days
                            per request and 30 days in total, so it is stitched from
                            consecutive windows; 2m and 5m allow 60 days; 1h allows 730
                            days; 1d returns the instrument's full listed history. The
                            per-request cap therefore binds on 1m only. 15m, 30m and 90m
                            are omitted because they are exact aggregations of 5m and
                            1h. Both 2m and 5m are kept because neither divides the
                            other, so in the 30 to 60 day window each carries bars the
                            other cannot reconstruct.
    UNIVERSE         dict   Group name mapped to {ticker: description}. Three groups:
                            us_equity with 24 US listings, us_etf with 17 US ETFs, and
                            uk_tradable with 11 London listings including the GBPUSD=X
                            spot pseudo-ticker used for currency conversion. Source
                            unknown, needs verification: the selection criteria are not
                            recorded in this file.

Inputs:
    Yahoo through yfinance, imported lazily inside quote_currency() and _history() so
        that importing this module does not require the dependency to be installed.
        yfinance.Ticker(ticker).history(...) returns the bars, and
        yfinance.Ticker(ticker).get_info()["currency"] returns the quote currency. The
        currency call is made only for London tickers ending in ".L", because London
        lists in GBp, GBP and USD interchangeably and GBp is pence, so mistaking it
        costs a factor of a hundred; US listings are uniformly USD.
    data/t212/curated/<group>/<ticker>/<interval>/*.parquet, footer statistics only.
        latest_stored() reads the row group statistics of the "ts" column and never
        touches the data body, so probing the whole universe costs no data read.
Outputs:
    data/t212/curated/<group>/<ticker>/1d/<ticker>_<year>.parquet
    data/t212/curated/<group>/<ticker>/<interval>/<ticker>_<start>_<end>_<interval>.parquet
    Both paths come from common/paths.py through equity_daily_path(),
    equity_intraday_path() and month_bounds(), and the files are written by
    common/store.py::write_table(). An "=" in a ticker becomes "_" in the file name,
    which affects GBPUSD=X only.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["fetch_interval", "write_daily", "write_intraday", "latest_stored",
           "discover_symbols", "stored_intervals", "earliest_bar", "stored_close_at",
           "quote_currency", "exchange_tz", "INTERVALS", "UNIVERSE",
           "RETRY_BASE_SEC", "RETRY_ATTEMPTS", "PACE_SEC"]

import time
import warnings
from datetime import date, timedelta

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from common.paths import (equity_daily_path, equity_intraday_path, month_bounds,
                          DIR_DATA)
from common.store import write_table

warnings.filterwarnings("ignore")

# Yahoo throttles intermittently rather than by capability: the same ticker and
# interval returns 17,000 rows on one call and nothing on the next. The cure is
# patience between attempts, not more attempts in quick succession. Four attempts
# at a 2-second base left four intervals empty; the same requests succeeded on
# the first try once given a longer gap.
RETRY_BASE_SEC = 8.0
RETRY_ATTEMPTS = 6
PACE_SEC = 0.6

_TZ_LONDON = "Europe/London"
_TZ_NEW_YORK = "America/New_York"

# (interval, lookback days or None for full history, days per request or None).
# The per-request cap only binds on 1m. 15m, 30m and 90m are omitted because they
# are exact aggregations of 5m and 1h; 2m and 5m are both kept because neither
# divides the other, so in the 30-to-60 day window each carries bars the other
# cannot reconstruct.
INTERVALS = [
    ("1d", None, None),
    ("1h", 730, None),
    ("5m", 59, None),
    ("2m", 59, None),
    ("1m", 29, 7),
]

UNIVERSE: dict[str, dict[str, str]] = {
    "us_equity": {
        "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AMZN": "Amazon",
        "GOOGL": "Alphabet", "META": "Meta", "TSLA": "Tesla", "AVGO": "Broadcom",
        "AMD": "AMD", "MU": "Micron", "INTC": "Intel", "TSM": "TSMC",
        "ORCL": "Oracle", "PLTR": "Palantir", "MRVL": "Marvell", "LRCX": "Lam Research",
        "AMAT": "Applied Materials", "DELL": "Dell",
        "KO": "Coca-Cola", "PG": "Procter & Gamble", "JNJ": "Johnson & Johnson",
        "WMT": "Walmart", "NEM": "Newmont", "XOM": "Exxon",
    },
    "us_etf": {
        "SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000",
        "GLD": "Gold bullion", "GDX": "Gold miners",
        "TLT": "Treasuries 20y+", "IEF": "Treasuries 7-10y", "SHY": "Treasuries 1-3y",
        "BIL": "T-bills 1-3m",
        "XLU": "Utilities", "XLP": "Consumer staples", "XLV": "Healthcare",
        "UUP": "US dollar index",
        "SH": "Inverse S&P 500", "PSQ": "Inverse Nasdaq 100",
        "SQQQ": "-3x Nasdaq 100", "VXX": "VIX short-term futures",
    },
    "uk_tradable": {
        "SGLN.L": "iShares Physical Gold ETC, GBX",
        "IGLN.L": "iShares Physical Gold ETC, USD",
        "IB01.L": "iShares USD T-Bond 0-1yr UCITS",
        "IDTL.L": "iShares USD T-Bond 20+yr UCITS, USD",
        "IBTL.L": "iShares USD T-Bond 20+yr UCITS, GBP",
        "XSPS.L": "Xtrackers S&P 500 Inverse Daily Swap UCITS",
        "VUSA.L": "Vanguard S&P 500 UCITS",
        "CSPX.L": "iShares Core S&P 500 UCITS acc",
        "EQQQ.L": "Invesco Nasdaq 100 UCITS",
        "IUCS.L": "iShares S&P 500 Consumer Staples UCITS",
        "GBPUSD=X": "GBP/USD spot",
    },
}

_CURRENCY_CACHE: dict[str, str] = {}


# ============================================================================
# [1] Fetching
# ============================================================================

def quote_currency(ticker: str) -> str:
    """Return a ticker's exchange quote currency, caching the lookup.

    London lists in GBp, GBP and USD interchangeably and the price alone does not
    say which; GBp is pence, so mistaking it costs a factor of a hundred. US
    listings are uniformly USD, so the slow metadata call is made only for London.
    """
    if ticker in _CURRENCY_CACHE:
        return _CURRENCY_CACHE[ticker]
    if not ticker.endswith(".L"):
        _CURRENCY_CACHE[ticker] = "USD"
        return "USD"
    import yfinance as yf
    try:
        ccy = yf.Ticker(ticker).get_info().get("currency", "UNKNOWN")
    except Exception:
        ccy = "UNKNOWN"
    _CURRENCY_CACHE[ticker] = ccy
    return ccy


def exchange_tz(ticker: str) -> str:
    """IANA zone of a ticker's exchange.

    London listings and the Yahoo FX series carry London-local stamps;
    everything else in this universe is US. Same rule as
    trading212/execution/market_data.py and backtest/t212/instruments.py.
    """
    return _TZ_LONDON if ticker.endswith(".L") or ticker.endswith("=X") \
        else _TZ_NEW_YORK


def _tidy(frame: pd.DataFrame, ticker: str,
          drop_from: date | None = None) -> pd.DataFrame:
    """Reduce a yfinance frame to the project schema.

    Args:
        drop_from: Discard every row whose EXCHANGE-LOCAL date is at or after
            this date. Queried during a trading session, Yahoo returns a row
            for the session in progress: an open, a running high and low, and
            the last print as the close. Stored, that half-formed bar looks
            exactly like a finished one, and because the updater skips a
            symbol whose newest stored timestamp already covers the fetch, it
            is never replaced -- 1,475 symbols carried such a row from
            2026-08-31. Every daily consumer then reads a close that is not a
            close: A1's admission, its 12-1 score and its ranking all shift.
            The guard is the caller's decision, so a run made after the close
            passes None and keeps the finished bar.
    """
    out = frame[["Open", "High", "Low", "Close", "Volume"]].copy()
    out.columns = ["open", "high", "low", "close", "volume"]
    out.index.name = "ts"
    out = out.reset_index()
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    out["quote_ccy"] = quote_currency(ticker)
    out = out.dropna(subset=["close"])
    if drop_from is not None:
        local_day = out["ts"].dt.tz_convert(exchange_tz(ticker)).dt.date
        out = out.loc[local_day < drop_from].reset_index(drop=True)
    return out


def _history(ticker: str, **kwargs) -> pd.DataFrame:
    """One yfinance history call with exponential back-off.

    Requests go one ticker at a time. A batch request resolves period="max" to a
    single 1927 start applied to every symbol and Yahoo throttles it: a 24-ticker
    batch returned data for 3 and reported the other 21 as delisted, none of
    which were.
    """
    import yfinance as yf
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            frame = yf.Ticker(ticker).history(**kwargs)
            if frame is not None and not frame.empty:
                return frame
        except Exception:
            pass
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_BASE_SEC * (2 ** (attempt - 1)))
    return pd.DataFrame()


def fetch_interval(ticker: str, interval: str, lookback: int | None,
                   chunk: int | None,
                   drop_from: date | None = None) -> pd.DataFrame:
    """Fetch one interval for one ticker, stitching windows where required.

    The whole available window is refetched rather than only the new tail.
    Adjusted prices are retroactive: a split changes every historical bar, so
    appending to an old file would leave the series inconsistent across the split
    date. Refetching the window is both cheaper than detecting that and correct
    by construction.
    """
    if lookback is None:
        raw = _history(ticker, period="max", interval=interval, auto_adjust=True)
        return _tidy(raw, ticker, drop_from) if not raw.empty \
            else pd.DataFrame()

    if chunk is None:
        raw = _history(ticker, period=f"{lookback}d", interval=interval,
                       auto_adjust=True)
        return _tidy(raw, ticker, drop_from) if not raw.empty \
            else pd.DataFrame()

    # 1m only: a single request is capped at 8 days, so the 30-day window is
    # covered by consecutive requests and concatenated.
    today = date.today()
    parts, cursor = [], today
    while (today - cursor).days < lookback:
        start = max(cursor - timedelta(days=chunk), today - timedelta(days=lookback))
        raw = _history(ticker, start=start.isoformat(), end=cursor.isoformat(),
                       interval=interval, auto_adjust=True)
        if not raw.empty:
            parts.append(_tidy(raw, ticker, drop_from))
        cursor = start
        time.sleep(PACE_SEC)
        if start <= today - timedelta(days=lookback):
            break
    if not parts:
        return pd.DataFrame()
    return (pd.concat(parts, ignore_index=True)
            .drop_duplicates(subset=["ts"]).sort_values("ts").reset_index(drop=True))


# ============================================================================
# [2] Storing
# ============================================================================

def write_daily(group: str, ticker: str, frame: pd.DataFrame,
                years: set[int] | None = None) -> tuple[int, int]:
    """Store daily bars as one file per calendar year.

    Args:
        years: Restrict the write to these calendar years. Adjusted prices are
            retroactive, so a split rewrites the whole history and every year has
            to be written; but on the ordinary day when nothing was adjusted,
            only the current year can differ. Rewriting 28 year files per symbol
            across 1,500 symbols on every run is pointless churn, so the caller
            passes the narrow set once it has established that history is intact.

    Returns:
        (files written, bytes written).
    """
    files = written = 0
    for year, part in frame.groupby(frame["ts"].dt.year):
        if years is not None and int(year) not in years:
            continue
        path = equity_daily_path(group, ticker, int(year))
        write_table(pa.Table.from_pandas(part, preserve_index=False), path, sort_by="ts")
        files += 1
        written += path.stat().st_size
    return files, written


def write_intraday(group: str, ticker: str, interval: str,
                   frame: pd.DataFrame) -> tuple[int, int]:
    """Store intraday bars as one file per calendar month.

    The name is anchored to the calendar rather than to the data, so a month
    whose first trading day is the 2nd is still labeled 01. The month in
    progress carries the latest date present instead of a future month end,
    which means its name changes as data accumulates; any earlier file for the
    same month is removed so a stale name cannot survive alongside the new one.

    Returns:
        (files written, bytes written).
    """
    latest = frame["ts"].max()
    files = written = 0
    for period, part in frame.groupby(frame["ts"].dt.to_period("M")):
        start, end = month_bounds(period.start_time.date(), latest)
        path = equity_intraday_path(group, ticker, interval, start, end)
        for stale in path.parent.glob(f"*_{start}_*_{interval}.parquet"):
            if stale != path:
                stale.unlink()
        write_table(pa.Table.from_pandas(part, preserve_index=False), path, sort_by="ts")
        files += 1
        written += path.stat().st_size
    return files, written


def latest_stored(group: str, ticker: str, interval: str) -> pd.Timestamp | None:
    """Return the newest timestamp already stored, or None if nothing is stored.

    Read from Parquet footer statistics rather than by scanning, so probing the
    whole universe costs no data read.
    """
    folder = DIR_DATA / "t212" / "curated" / group / ticker / interval
    if not folder.is_dir():
        return None
    newest = None
    for path in folder.glob("*.parquet"):
        try:
            meta = pq.read_metadata(path)
        except Exception:
            continue
        names = meta.schema.names
        if "ts" not in names:
            continue
        idx = names.index("ts")
        for group_index in range(meta.num_row_groups):
            stats = meta.row_group(group_index).column(idx).statistics
            if stats is None or stats.max is None:
                continue
            value = pd.Timestamp(stats.max)
            if newest is None or value > newest:
                newest = value
    return newest


# ============================================================================
# [3] Discovery
# ============================================================================

def _group_dir(group: str):
    return DIR_DATA / "t212" / "curated" / group


def discover_symbols(group: str) -> list[str]:
    """Return every symbol to maintain: those configured plus those already on disk.

    The configured universe is a starting point, not the whole truth. Symbols get
    added to the lake by hand and by other tooling, and a symbol that exists on
    disk but not in the configuration would otherwise never be refreshed again --
    it would sit there quietly going stale while the report claimed everything was
    current. Reading the directory makes the lake self-describing.
    """
    configured = set(UNIVERSE.get(group, {}))
    folder = _group_dir(group)
    on_disk = {p.name for p in folder.iterdir() if p.is_dir()} if folder.is_dir() else set()
    return sorted(configured | on_disk)


def stored_intervals(group: str, ticker: str) -> list[str]:
    """Return the intervals already stored for one symbol, in INTERVALS order.

    An update maintains what exists rather than expanding it. Most of the lake
    holds daily bars only; fetching five intervals for every symbol would turn a
    30-minute refresh into a multi-hour one and would be throttled long before it
    finished. A symbol with no data at all falls back to the full set, which is
    what a newly configured symbol needs.
    """
    folder = _group_dir(group) / ticker
    present = {p.name for p in folder.iterdir() if p.is_dir()} if folder.is_dir() else set()
    ordered = [iv for iv, _lb, _ch in INTERVALS if iv in present]
    return ordered if ordered else [iv for iv, _lb, _ch in INTERVALS]


def earliest_bar(group: str, ticker: str) -> tuple[pd.Timestamp, float] | None:
    """Return the oldest stored daily bar as (timestamp, close), or None if empty.

    Used to detect a retroactive adjustment. Adjusted prices are rewritten all
    the way back whenever a dividend or split lands, so if the oldest close no
    longer matches what was fetched, every year file is stale on that basis and
    must be rewritten. When it does match, only the current year can have changed.

    The timestamp is returned alongside because the close alone cannot
    distinguish an adjustment from a truncated response. A partial fetch that
    starts years later also has a different first close, and treating that as an
    adjustment would rewrite the recent years on a new basis while leaving the
    older files on the old one -- a silent split-brain in the series. The caller
    compares dates first and only then compares closes.
    """
    folder = _group_dir(group) / ticker / "1d"
    if not folder.is_dir():
        return None
    files = sorted(folder.glob("*.parquet"))
    if not files:
        return None
    try:
        frame = pq.read_table(files[0], columns=["ts", "close"]).to_pandas()
    except Exception:
        return None
    if not len(frame):
        return None
    frame = frame.sort_values("ts")
    return pd.Timestamp(frame["ts"].iloc[0]), float(frame["close"].iloc[0])


def stored_close_at(group: str, ticker: str, ts) -> float | None:
    """Return the stored daily close on one date, or None if that date is absent.

    Used as an adjustment anchor. Comparing a bar from a couple of years back
    against a freshly fetched copy of the same bar answers whether prices were
    retroactively restated, and it answers it without downloading the whole
    history: period="max" measured 9.55 seconds and succeeded on only one attempt
    in three, while a two-year window measured 0.57 seconds and succeeded every
    time. Anchoring inside a short window is what makes a daily pass over fifteen
    hundred symbols finish at all.
    """
    stamp = pd.Timestamp(ts)
    path = equity_daily_path(group, ticker, int(stamp.year))
    if not path.is_file():
        return None
    try:
        frame = pq.read_table(path, columns=["ts", "close"]).to_pandas()
    except Exception:
        return None
    match = frame.loc[pd.to_datetime(frame["ts"], utc=True) == stamp, "close"]
    return float(match.iloc[0]) if len(match) else None
