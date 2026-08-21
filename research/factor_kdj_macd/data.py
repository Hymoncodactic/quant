"""Daily bar loading for the KDJ+MACD study.

Responsibility: read the per-year parquet partitions written by
scripts/20260819_ingest_equity.py and return one clean, sorted frame per symbol.
Not responsible for: downloading, gap filling, resampling.

Data spec: data/t212/curated/DATA_SPEC.md sections 3 and 5.1.
    ts       timestamp UTC, one row per trading day
    open/high/low/close  float64, split- and dividend-adjusted (auto_adjust=True)
    volume   float64, shares
    quote_ccy  string, USD for every symbol used here

Public functions:
    load_daily(symbol, start, end)  One symbol, one window, sorted and deduped
    daily_dir(symbol)               Partition directory for one symbol
"""

from __future__ import annotations

__all__ = ["load_daily", "daily_dir", "DATA_ROOT", "GROUP"]

from pathlib import Path

import pandas as pd

# Resolved relative to the repository root, which is three levels above this
# file (research/factor_kdj_macd/data.py -> repo root).
DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
GROUP = "us_equity"

_REQUIRED_COLUMNS = ("ts", "open", "high", "low", "close", "volume")


def daily_dir(symbol: str) -> Path:
    """Return the 1d partition directory for one symbol."""
    return DATA_ROOT / "t212" / "curated" / GROUP / symbol / "1d"


def load_daily(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Load daily bars for one symbol.

    Args:
        symbol: Ticker as it appears on disk, for example "NVDA".
        start: Inclusive lower bound on the trading date, "YYYY-MM-DD", or None.
        end: Inclusive upper bound on the trading date, "YYYY-MM-DD", or None.

    Returns:
        Frame with columns date (datetime64[ns], tz-naive trading date),
        open/high/low/close (float64, USD, adjusted), volume (float64, shares).
        Sorted ascending by date, duplicate dates dropped keeping the last,
        index reset.

    Raises:
        FileNotFoundError: No parquet partitions exist for the symbol.
        ValueError: A partition is missing a required column, or the quote
            currency is not USD (the study assumes a single currency).
    """
    files = sorted(daily_dir(symbol).glob(f"{symbol}_*.parquet"))
    if not files:
        raise FileNotFoundError(f"no daily partitions for {symbol} under {daily_dir(symbol)}")

    frames = []
    for path in files:
        frame = pd.read_parquet(path)
        missing = [c for c in _REQUIRED_COLUMNS if c not in frame.columns]
        if missing:
            raise ValueError(f"{path} is missing columns {missing}")
        if "quote_ccy" in frame.columns:
            currencies = set(frame["quote_ccy"].dropna().unique())
            if not currencies <= {"USD"}:
                raise ValueError(f"{path} holds non-USD quotes {currencies}")
        frames.append(frame[list(_REQUIRED_COLUMNS)])

    bars = pd.concat(frames, ignore_index=True)
    # The stored timestamp is midnight New York expressed in UTC, so the calendar
    # date must be taken after converting, not from the raw UTC day.
    bars["date"] = pd.to_datetime(bars["ts"], utc=True).dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
    bars = bars.drop(columns=["ts"])
    bars = bars.sort_values("date").drop_duplicates(subset=["date"], keep="last")

    if start is not None:
        bars = bars[bars["date"] >= pd.Timestamp(start)]
    if end is not None:
        bars = bars[bars["date"] <= pd.Timestamp(end)]

    return bars.reset_index(drop=True)[["date", "open", "high", "low", "close", "volume"]]
