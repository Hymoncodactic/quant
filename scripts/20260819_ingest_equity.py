"""Equity ingest: every bar resolution the source will give, partitioned for updates.

Granularity is capped by the source, not by choice. Measured against Yahoo on
2026-08-19, the valid intervals are exactly
[1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 4h, 1d, 5d, 1wk, 1mo, 3mo] -- the API
enumerates them when rejecting anything else -- so no sub-minute equity bar
exists at any price here. Each interval also carries its own history limit,
verified by request rather than assumed:

    1m   8 days per request, and "the requested range must be within the last
         30 days" verbatim from the API. Stitched from consecutive windows.
    2m   60 days
    5m   60 days
    1h   730 days
    1d   the instrument's full listed history, back to 1962 for the oldest names

Only intervals that add information are stored. 15m, 30m and 90m are exact
aggregations of 5m and 1h and are left to be computed on demand; 2m and 5m are
both kept because neither divides the other, so for the 30-to-60 day window each
carries bars the other cannot reconstruct.

Partitioning is chosen so that a refresh rewrites only what changed:
    daily     one file per calendar year
    intraday  one file per calendar month

A single 45-year daily file has to be rewritten in full on every update, which is
the situation this layout replaces.

Public functions:
    main()   Download and store every interval for the configured universe
"""

from __future__ import annotations

__all__ = ["main"]

import sys
import time
import warnings
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pyarrow as pa

from common.paths import equity_daily_path, equity_intraday_path
from common.store import write_table

warnings.filterwarnings("ignore")

# Yahoo throttles intermittently rather than by capability: the same ticker and
# interval returns 17,000 rows on one call and nothing on the next. The cure is
# patience between attempts, not more attempts in quick succession. Measured on
# 2026-08-19: four attempts at a 2-second base left four intervals empty, while
# the same requests succeeded on the first try once given a longer gap.
RETRY_BASE_SEC = 8.0
RETRY_ATTEMPTS = 6
PACE_SEC = 0.6

# (interval, lookback days or None for full history, days per request or None)
# The per-request cap only binds on 1m; the others return their whole window in
# one call.
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


def _quote_currency(ticker: str) -> str:
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
    cols = ["Open", "High", "Low", "Close", "Volume"]
    out = frame[cols].copy()
    out.columns = ["open", "high", "low", "close", "volume"]
    out.index.name = "ts"
    out = out.reset_index()
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    out["quote_ccy"] = _quote_currency(ticker)
    return out.dropna(subset=["close"])


def _history(ticker: str, **kwargs) -> pd.DataFrame:
    """One yfinance history call with exponential back-off.

    Requests go one ticker at a time. A batch request resolves period="max" to a
    single 1927 start applied to every symbol and Yahoo throttles it: a 24-ticker
    batch returned data for 3 and reported the other 21 as delisted, none of which
    were.
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


def _fetch_interval(ticker: str, interval: str, lookback: int | None,
                    chunk: int | None) -> pd.DataFrame:
    """Fetch one interval for one ticker, stitching windows where required."""
    if lookback is None:
        raw = _history(ticker, period="max", interval=interval, auto_adjust=True)
        return _tidy(raw, ticker) if not raw.empty else pd.DataFrame()

    if chunk is None:
        raw = _history(ticker, period=f"{lookback}d", interval=interval,
                       auto_adjust=True)
        return _tidy(raw, ticker) if not raw.empty else pd.DataFrame()

    # 1m only: the API caps a single request at 8 days, so the 30-day window is
    # covered by consecutive requests and concatenated.
    today = date.today()
    parts = []
    cursor = today
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
            .drop_duplicates(subset=["ts"])
            .sort_values("ts")
            .reset_index(drop=True))


def _write_daily(group: str, ticker: str, frame: pd.DataFrame) -> tuple[int, int]:
    """Write daily bars as one file per calendar year."""
    files = written = 0
    for year, part in frame.groupby(frame["ts"].dt.year):
        path = equity_daily_path(group, ticker, int(year))
        write_table(pa.Table.from_pandas(part, preserve_index=False), path, sort_by="ts")
        files += 1
        written += path.stat().st_size
    return files, written


def _write_intraday(group: str, ticker: str, interval: str,
                    frame: pd.DataFrame) -> tuple[int, int]:
    """Write intraday bars as one file per calendar month.

    The file name carries the first and last date actually present rather than
    the nominal month boundaries, so a partial current month is self-describing.
    """
    files = written = 0
    for _, part in frame.groupby(frame["ts"].dt.to_period("M")):
        start = part["ts"].min().strftime("%Y%m%d")
        end = part["ts"].max().strftime("%Y%m%d")
        path = equity_intraday_path(group, ticker, interval, start, end)
        write_table(pa.Table.from_pandas(part, preserve_index=False), path, sort_by="ts")
        files += 1
        written += path.stat().st_size
    return files, written


def main() -> None:
    """Download every configured interval for every instrument."""
    started = time.time()
    total_files = total_rows = total_bytes = 0
    failures: list[str] = []

    for group, members in UNIVERSE.items():
        print(f"\n{'='*100}\n[{group}] {len(members)} instruments\n{'='*100}", flush=True)
        for ticker, name in members.items():
            line = [f"  {ticker:<10} {name[:26]:<27}"]
            for interval, lookback, chunk in INTERVALS:
                frame = _fetch_interval(ticker, interval, lookback, chunk)
                time.sleep(PACE_SEC)
                if frame.empty:
                    line.append(f"{interval}:—")
                    failures.append(f"{ticker}/{interval}")
                    continue
                if interval == "1d":
                    files, size = _write_daily(group, ticker, frame)
                else:
                    files, size = _write_intraday(group, ticker, interval, frame)
                total_files += files
                total_rows += len(frame)
                total_bytes += size
                line.append(f"{interval}:{len(frame):,}({files}f)")
            print("  ".join(line), flush=True)

    print(f"\n{'='*100}")
    print(f"RESULT  {total_files:,} files  {total_rows:,} rows  "
          f"{total_bytes/1e6:.1f} MB  {(time.time()-started)/60:.1f} min")
    if failures:
        print(f"  {len(failures)} interval(s) returned nothing: {failures}")


if __name__ == "__main__":
    main()
