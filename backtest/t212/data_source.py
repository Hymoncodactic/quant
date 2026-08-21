"""Load T212-universe bars and the GBPUSD series from the local data lake.

Responsibility: find and read the curated parquet partitions for a symbol list,
one interval, one date window. The window is interpreted in the exchange's
LOCAL calendar: a bar belongs to the window when its exchange-local trading day
falls inside it. Slicing raw UTC timestamps instead would drop or leak the
London bars that stamp 23:00 UTC of the previous day during BST
(fixplans/framework/02_data_layer.md section 3.1). A symbol with no partitions
raises instead of vanishing from the result. The backtest never touches a
network (ARCHITECTURE.md section 2, backtest row).
One data repair happens here, and only for GBPUSD=X: Yahoo stamps the FX daily
close from a different session cut than the high and low, leaving close outside
[low, high] by up to about 6e-4 relative (103 bars measured 2026-08-21). The
engine consumes only the FX close (strategy sizing through the feed, conversion
through FxSeries), so enveloping high and low over open and close changes no
number anywhere; it only lets the frame pass the ordering gate instead of
failing on a field nothing reads.

Out of scope: alignment and validation of the loaded frames, which belong to
backtest/engine/feed.py; path assembly, which belongs to common/paths.py;
exchange time zone mapping, which belongs to backtest/t212/instruments.py.

Public functions:
    load_bars(symbols, interval, start, end, data_root=None)
        Bars per symbol as a dict of DataFrames. symbols are Yahoo-style
        tickers as stored, for example "AAPL" or "SGLN.L"; interval is one of
        the stored intervals 1m, 2m, 5m, 1h, 1d; start and end are an inclusive
        window of exchange-local days written "YYYY-MM-DD".
    load_fx(interval, start, end, data_root=None)
        GBPUSD bars covering the window plus the leading buffer.

Constants:
    GROUPS
        tuple, ("us_equity", "us_etf", "uk_tradable"): the curated groups
        searched for a symbol, in that order. Source: the curated layout of
        docs/data/t212/DATA_SPEC.md section 5.1.
    FX_SYMBOL
        str, "GBPUSD=X": the FX ticker as stored.
    FX_HISTORY_BUFFER_DAYS
        int, 14 calendar days. The FX availability rule hands a fill the
        PREVIOUS day's close, so the series must start before the backtest
        window; two weeks covers any holiday cluster around the window start.

Inputs (layout per docs/data/t212/DATA_SPEC.md section 5.1, paths built by
common/paths.py; data_root is injectable because the data lake lives beside the
main working copy while code may run from a git worktree):
    daily
        data/t212/curated/<group>/<symbol>/1d/<symbol>_<year>.parquet
    intraday
        data/t212/curated/<group>/<symbol>/<interval>/
        <symbol>_<start>_<end>_<interval>.parquet
Outputs: None. This module only reads.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["load_bars", "load_fx", "GROUPS", "FX_SYMBOL",
           "FX_HISTORY_BUFFER_DAYS"]

from pathlib import Path

import pandas as pd

from backtest.t212.instruments import exchange_tz
from common.paths import equity_curated_root, equity_interval_dir

GROUPS = ("us_equity", "us_etf", "uk_tradable")
FX_SYMBOL = "GBPUSD=X"

# The FX availability rule hands a fill the PREVIOUS day's close, so the
# series must start before the backtest window; two weeks covers any holiday
# cluster around the window start.
FX_HISTORY_BUFFER_DAYS = 14


def _symbol_dir(data_root: Path | str | None, symbol: str,
                interval: str) -> Path:
    """Locate a symbol's interval directory, layout via common/paths only."""
    for group in GROUPS:
        candidate = equity_interval_dir(group, symbol, interval, data_root)
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        f"no {interval} data for {symbol} under "
        f"{equity_curated_root(data_root)} (groups {GROUPS})")


def _read_symbol(data_root: Path | str | None, symbol: str, interval: str,
                 start: str, end: str) -> pd.DataFrame:
    """Read one symbol's partitions and slice to the window.

    The window is interpreted in the exchange's local calendar: a daily bar
    belongs to the window when its exchange-local trading DAY is inside it.
    Slicing raw UTC timestamps instead would drop or leak the London bars
    that stamp 23:00 UTC of the previous day during BST
    (fixplans/framework/02_data_layer.md section 3.1).
    """
    folder = _symbol_dir(data_root, symbol, interval)
    parts = sorted(folder.glob("*.parquet"))
    if not parts:
        raise FileNotFoundError(f"{folder} holds no parquet files")
    frame = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    frame = frame.sort_values("ts").reset_index(drop=True)
    local_day = frame["ts"].dt.tz_convert(exchange_tz(symbol)).dt.date
    mask = (local_day >= pd.Timestamp(start).date()) & \
           (local_day <= pd.Timestamp(end).date())
    frame = frame.loc[mask].reset_index(drop=True)
    if symbol == FX_SYMBOL:
        # Yahoo stamps the FX daily close from a different session cut than
        # the high/low, leaving close outside [low, high] by up to ~6e-4
        # relative (103 bars measured 2026-08-21). The engine consumes only
        # the FX CLOSE (strategy sizing via the feed, conversion via
        # FxSeries); the high/low are never read, so enveloping them over
        # open/close changes no number anywhere -- it only lets the frame
        # pass the ordering gate instead of failing on a field nothing uses.
        frame["high"] = frame[["high", "open", "close"]].max(axis=1)
        frame["low"] = frame[["low", "open", "close"]].min(axis=1)
    return frame


def load_bars(symbols: list[str], interval: str, start: str, end: str,
              data_root: Path | str | None = None) -> dict[str, pd.DataFrame]:
    """Bars for every symbol; missing symbols raise instead of vanishing.

    Args:
        symbols: Yahoo-style tickers as stored, e.g. "AAPL", "SGLN.L".
        interval: One of the stored intervals: 1m, 2m, 5m, 1h, 1d.
        start, end: Inclusive window, "YYYY-MM-DD", exchange-local days.
    """
    return {s: _read_symbol(data_root, s, interval, start, end)
            for s in symbols}


def load_fx(interval: str, start: str, end: str,
            data_root: Path | str | None = None) -> pd.DataFrame:
    """GBPUSD bars covering the window plus the leading buffer."""
    buffered = (pd.Timestamp(start)
                - pd.Timedelta(days=FX_HISTORY_BUFFER_DAYS)).strftime("%Y-%m-%d")
    return _read_symbol(data_root, FX_SYMBOL, interval, buffered, end)
