"""Load T212-universe bars and the GBPUSD series from the local data lake.

Responsibility: find and read the curated parquet partitions for a symbol
list, one interval, one date window. The backtest never touches a network
(ARCHITECTURE.md section 2, backtest row).
Not responsible for: alignment or validation (engine/feed.py does both).

Layout facts (docs/data/t212/DATA_SPEC.md section 5.1, paths built by
common/paths.py):
    daily     <group>/<symbol>/1d/<symbol>_<year>.parquet
    intraday  <group>/<symbol>/<interval>/<symbol>_<start>_<end>_<interval>.parquet

data_root is injectable because the data lake lives in the main working copy
while code may run from a git worktree; default resolves through
common.paths so business code never assembles paths on its own.

Public functions:
    load_bars(symbols, interval, start, end, data_root=None)
    load_fx(interval, start, end, data_root=None)

Public constants:
    GROUPS, FX_SYMBOL, FX_HISTORY_BUFFER_DAYS
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
