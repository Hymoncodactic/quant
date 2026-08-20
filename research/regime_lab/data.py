"""Data loading for the regime study: daily and intraday, equities and ETFs.

Responsibility: read the per-year (daily) and per-month (intraday) parquet
partitions and return clean sorted frames. Extends the previous study's loader
with the us_etf group and intraday intervals. Not responsible for indicators.

Data spec: data/t212/curated/DATA_SPEC.md. All symbols used here quote in USD;
non-USD rows are rejected.

Public functions:
    load_daily(symbol, group, start, end)      Daily bars, full available history
    load_intraday(symbol, interval, group)     Intraday bars (1h/5m/2m/1m)
    TECH18, ETF_SYMBOLS
"""

from __future__ import annotations

__all__ = ["load_daily", "load_intraday", "TECH18", "ETF_SYMBOLS", "DATA_ROOT"]

from pathlib import Path

import pandas as pd

DATA_ROOT = Path(__file__).resolve().parents[2] / "data"

TECH18 = ("AAPL", "AMAT", "AMD", "AMZN", "AVGO", "DELL", "GOOGL", "INTC", "LRCX",
          "META", "MRVL", "MSFT", "MU", "NVDA", "ORCL", "PLTR", "TSLA", "TSM")

ETF_SYMBOLS = ("QQQ", "SPY", "TLT", "IEF", "VXX", "PSQ", "SQQQ", "GLD",
               "XLP", "XLU", "XLV", "IWM", "BIL", "SH", "SHY", "UUP", "GDX")

_REQUIRED = ("ts", "open", "high", "low", "close", "volume")


def _read_partitions(files: list[Path]) -> pd.DataFrame:
    """Read parquet partitions, enforce schema and USD quotes, keep raw ts."""
    frames = []
    for path in files:
        frame = pd.read_parquet(path)
        missing = [c for c in _REQUIRED if c not in frame.columns]
        if missing:
            raise ValueError(f"{path} is missing columns {missing}")
        if "quote_ccy" in frame.columns:
            currencies = set(frame["quote_ccy"].dropna().unique())
            if not currencies <= {"USD"}:
                raise ValueError(f"{path} holds non-USD quotes {currencies}")
        frames.append(frame[list(_REQUIRED)])
    return pd.concat(frames, ignore_index=True)


def load_daily(
    symbol: str,
    group: str = "us_equity",
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Load daily bars for one symbol.

    Args:
        symbol: Ticker as stored on disk.
        group: "us_equity" or "us_etf".
        start: Inclusive lower bound "YYYY-MM-DD" or None.
        end: Inclusive upper bound "YYYY-MM-DD" or None.

    Returns:
        Frame with date (tz-naive New York trading date), open/high/low/close
        (USD, adjusted), volume. Sorted, deduped, index reset.

    Raises:
        FileNotFoundError: No partitions for the symbol.
        ValueError: Schema or currency violation.
    """
    directory = DATA_ROOT / "t212" / "curated" / group / symbol / "1d"
    files = sorted(directory.glob(f"{symbol}_*.parquet"))
    if not files:
        raise FileNotFoundError(f"no daily partitions under {directory}")
    bars = _read_partitions(files)
    bars["date"] = (pd.to_datetime(bars["ts"], utc=True)
                    .dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize())
    bars = bars.drop(columns=["ts"]).sort_values("date").drop_duplicates("date", keep="last")
    if start is not None:
        bars = bars[bars["date"] >= pd.Timestamp(start)]
    if end is not None:
        bars = bars[bars["date"] <= pd.Timestamp(end)]
    return bars.reset_index(drop=True)[["date", "open", "high", "low", "close", "volume"]]


def load_intraday(symbol: str, interval: str = "1h", group: str = "us_equity") -> pd.DataFrame:
    """Load all stored intraday bars for one symbol at one interval.

    Args:
        symbol: Ticker as stored on disk.
        interval: One of "1h", "5m", "2m", "1m".
        group: "us_equity" or "us_etf".

    Returns:
        Frame with ts (tz-aware New York), date (the New York trading date the
        bar belongs to), open/high/low/close, volume. Sorted by ts, deduped.

    Raises:
        FileNotFoundError: No partitions for the symbol/interval.
    """
    directory = DATA_ROOT / "t212" / "curated" / group / symbol / interval
    files = sorted(directory.glob(f"{symbol}_*_{interval}.parquet"))
    if not files:
        raise FileNotFoundError(f"no {interval} partitions under {directory}")
    bars = _read_partitions(files)
    ts = pd.to_datetime(bars["ts"], utc=True).dt.tz_convert("America/New_York")
    bars["ts"] = ts
    bars["date"] = ts.dt.tz_localize(None).dt.normalize()
    return (bars.sort_values("ts").drop_duplicates("ts", keep="last")
            .reset_index(drop=True)[["ts", "date", "open", "high", "low", "close", "volume"]])
