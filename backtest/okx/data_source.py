"""Load crypto bars for the OKX backtest line from the local Binance archive.

Responsibility: read Binance kline partitions and reduce them to the engine bar
schema (ts, open, high, low, close, volume, quote_ccy), sliced to a UTC date
window. Crypto trades around the clock in UTC, so unlike the equity loader
there is no exchange-local-day conversion: the window is plain UTC dates. A
symbol with no partitions raises instead of vanishing from the result. The
backtest never touches a network (ARCHITECTURE.md section 2, backtest row).

Out of scope: alignment and validation of the loaded frames, which belong to
backtest/engine/feed.py; OKX cost and execution modeling, which belongs to a
future OKX broker adapter and is blocked on S4 fee research; path assembly,
which belongs to common/paths.py.

Public functions:
    load_klines(symbols, period, start, end, market="spot", data_root=None)
        Bars per symbol as a dict of DataFrames. period is a stored bar
        interval ("1d" or "1m" as of 2026-08-21); start and end are an
        inclusive UTC date window written "YYYY-MM-DD"; market is "spot",
        because um holds no klines locally as of 2026-08-21.

Constants:
    KLINE_QUOTE_CCY
        str, "USDT". Every stored pair quotes in USDT (all symbols end in
        USDT), and the crypto line's accounting currency is USDT per
        CLAUDE.md section 0.
    _BAR_COLUMNS
        The columns read out of each partition: ts, open, high, low, close,
        volume. The flow extras are deliberately left unread.

Inputs:
    data/binance/curated/<market>/klines/<symbol>/<period>/year=YYYY/*.parquet
        Path built by common.paths.binance_partition_dir. data_root overrides
        the lake root, because code may run from a git worktree while the data
        lake sits beside the main working copy.
        Data facts (docs/data/binance/DATA_SPEC.md, verified against the tree
        2026-08-21): spot klines exist for 9 USDT pairs at 1d and 1m, 2017-08
        to present; columns are ts/open/high/low/close/volume plus flow extras
        (quote_volume, count, taker_buy_*); ts is the bar OPEN time, UTC.
        Binance is a DATA SOURCE only, never a venue (common/paths.py
        DATA_SOURCES versus VENUES).
Outputs: None. This module only reads.

Change log:
    2026-08-22  Header expanded to the six-section spec; the data facts of the
                previous header are carried over into "Inputs".
"""

from __future__ import annotations

__all__ = ["load_klines", "KLINE_QUOTE_CCY"]

from pathlib import Path

import pandas as pd

from common.paths import binance_partition_dir

# Every stored pair quotes in USDT (symbols all end in USDT; the crypto
# line's accounting currency will be USDT per CLAUDE.md section 0).
KLINE_QUOTE_CCY = "USDT"

_BAR_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def _read_symbol(symbol: str, period: str, start: str, end: str,
                 market: str, data_root: Path | str | None) -> pd.DataFrame:
    """Read one symbol's kline partitions and slice to the UTC date window.

    Crypto trades around the clock in UTC, so unlike the equity loader there
    is no exchange-local-day conversion: the window is plain UTC dates.
    """
    folder = binance_partition_dir(market, "klines", symbol, period,
                                   data_root=data_root)
    parts = sorted(folder.glob("year=*/*.parquet"))
    if not parts:
        raise FileNotFoundError(f"no {period} klines for {symbol} under {folder}")
    frame = pd.concat([pd.read_parquet(p, columns=_BAR_COLUMNS)
                       for p in parts], ignore_index=True)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    frame = frame.sort_values("ts").reset_index(drop=True)
    lo = pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    frame = frame.loc[(frame["ts"] >= lo) & (frame["ts"] < hi)]
    frame = frame.reset_index(drop=True)
    frame["quote_ccy"] = KLINE_QUOTE_CCY
    return frame


def load_klines(symbols: list[str], period: str, start: str, end: str,
                market: str = "spot",
                data_root: Path | str | None = None) -> dict[str, pd.DataFrame]:
    """Bars for every symbol; a missing symbol raises instead of vanishing.

    Args:
        symbols: Binance pair symbols, e.g. "BTCUSDT".
        period: Stored bar interval, "1d" or "1m" as of 2026-08-21.
        start, end: Inclusive UTC date window, "YYYY-MM-DD".
        market: "spot" (um holds no klines locally as of 2026-08-21).
        data_root: Optional data-lake root override.
    """
    return {s: _read_symbol(s, period, start, end, market, data_root)
            for s in symbols}
