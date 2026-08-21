"""Fetching and storing Yahoo bar data for the equity universe.

This is the single implementation of that logic. The initial ingest and the
incremental updater both call it, so the partition layout and the naming rule
cannot drift between them.

Granularity is capped by the source. Measured 2026-08-19, Yahoo enumerates its
valid intervals when rejecting anything else:
[1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 4h, 1d, 5d, 1wk, 1mo, 3mo]. No sub-minute
equity bar exists here at any price. Each interval carries its own history
limit, verified by request:

    1m   8 days per request, 30 days total; stitched from consecutive windows
    2m   60 days
    5m   60 days
    1h   730 days
    1d   the instrument's full listed history

Public functions:
    fetch_interval(ticker, interval, lookback, chunk)  Bars for one ticker/interval
    write_daily(group, ticker, frame)                  Store daily bars, one file per year
    write_intraday(group, ticker, interval, frame)     Store intraday bars, one file per month
    latest_stored(group, ticker, interval)             Newest timestamp already on disk
    quote_currency(ticker)                             Exchange quote currency

Public constants:
    INTERVALS, UNIVERSE
"""

from __future__ import annotations

__all__ = ["fetch_interval", "write_daily", "write_intraday", "latest_stored",
           "quote_currency", "INTERVALS", "UNIVERSE",
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


def _tidy(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Reduce a yfinance frame to the project schema."""
    out = frame[["Open", "High", "Low", "Close", "Volume"]].copy()
    out.columns = ["open", "high", "low", "close", "volume"]
    out.index.name = "ts"
    out = out.reset_index()
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    out["quote_ccy"] = quote_currency(ticker)
    return out.dropna(subset=["close"])


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
                   chunk: int | None) -> pd.DataFrame:
    """Fetch one interval for one ticker, stitching windows where required.

    The whole available window is refetched rather than only the new tail.
    Adjusted prices are retroactive: a split changes every historical bar, so
    appending to an old file would leave the series inconsistent across the split
    date. Refetching the window is both cheaper than detecting that and correct
    by construction.
    """
    if lookback is None:
        raw = _history(ticker, period="max", interval=interval, auto_adjust=True)
        return _tidy(raw, ticker) if not raw.empty else pd.DataFrame()

    if chunk is None:
        raw = _history(ticker, period=f"{lookback}d", interval=interval,
                       auto_adjust=True)
        return _tidy(raw, ticker) if not raw.empty else pd.DataFrame()

    # 1m only: a single request is capped at 8 days, so the 30-day window is
    # covered by consecutive requests and concatenated.
    today = date.today()
    parts, cursor = [], today
    while (today - cursor).days < lookback:
        start = max(cursor - timedelta(days=chunk), today - timedelta(days=lookback))
        raw = _history(ticker, start=start.isoformat(), end=cursor.isoformat(),
                       interval=interval, auto_adjust=True)
        if not raw.empty:
            parts.append(_tidy(raw, ticker))
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

def write_daily(group: str, ticker: str, frame: pd.DataFrame) -> tuple[int, int]:
    """Store daily bars as one file per calendar year.

    Returns:
        (files written, bytes written).
    """
    files = written = 0
    for year, part in frame.groupby(frame["ts"].dt.year):
        path = equity_daily_path(group, ticker, int(year))
        write_table(pa.Table.from_pandas(part, preserve_index=False), path, sort_by="ts")
        files += 1
        written += path.stat().st_size
    return files, written


def write_intraday(group: str, ticker: str, interval: str,
                   frame: pd.DataFrame) -> tuple[int, int]:
    """Store intraday bars as one file per calendar month.

    The name is anchored to the calendar rather than to the data, so a month
    whose first trading day is the 2nd is still labelled 01. The month in
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
