"""Bar feed: aligned multi-symbol bar stream with a hard data-quality gate.

Responsibility: turn per-symbol DataFrames into a chronologically advancing
stream of (step, key, {symbol: Bar}) and expose a cutoff-safe MarketView.
Not responsible for: reading parquet (backtest/t212/data_source.py) or any
venue cost logic.

Alignment rules (fixplans/framework/02_data_layer.md sections 3-4, verified on
real data 2026-08-20):
    - Intraday bars align on their UTC open timestamp.
    - Daily bars align on the exchange-local trading DAY. During BST a London
      daily bar's raw UTC timestamp falls at 23:00 of the PREVIOUS UTC day, so
      aligning daily data on raw UTC dates shifts London one day; the local
      date is the only correct join key.
    - A symbol missing a bar on some key is normal (holiday asymmetry,
      suspension) and never drops the timeline key.

Public functions and classes:
    validate_frame(frame, symbol)          Data-quality gate, raises on violation
    trading_key(ts, tz_name, daily)        Alignment key for one timestamp
    BarFeed(frames, tz_for_symbol, daily)  The aligned stream
    FxSeries(frame)                        rate_at(ts): last GBPUSD close <= ts
    MarketView                             Cutoff-enforced view for strategies
"""

from __future__ import annotations

__all__ = ["validate_frame", "trading_key", "BarFeed", "FxSeries", "MarketView"]

from decimal import Decimal
from typing import Callable, Iterator

import pandas as pd

from backtest.engine.types import Bar

# Quote currencies known to the engine across both venue lines: t212 equity
# data quotes in USD/GBP/GBp, the crypto line's Binance-sourced bars quote in
# USDT. A venue adapter may pass a tighter set to reject foreign frames.
VALID_QUOTE_CCYS = ("USD", "GBP", "GBp", "USDT")


# ============================================================================
# [1] Quality gate
# ============================================================================

def validate_frame(frame: pd.DataFrame, symbol: str,
                   valid_ccys: tuple[str, ...] = VALID_QUOTE_CCYS) -> None:
    """Assert the data-quality gate on one symbol's bars; raise on violation.

    Gate list is fixplans/framework/02_data_layer.md section 7. A violation
    stops the run: silently skipping bad bars is survivorship bias by another
    name.
    """
    if frame.empty:
        raise ValueError(f"{symbol}: empty frame")
    ts = frame["ts"]
    if not ts.is_monotonic_increasing or ts.duplicated().any():
        raise ValueError(f"{symbol}: ts not strictly increasing")
    bad_hl = (frame["high"] < frame[["open", "close"]].max(axis=1)) | \
             (frame["low"] > frame[["open", "close"]].min(axis=1))
    if bad_hl.any():
        raise ValueError(f"{symbol}: OHLC ordering violated on "
                         f"{int(bad_hl.sum())} bars, first at "
                         f"{frame.loc[bad_hl, 'ts'].iloc[0]}")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError(f"{symbol}: non-positive price present")
    if (frame["volume"] < 0).any():
        raise ValueError(f"{symbol}: negative volume present")
    ccys = set(frame["quote_ccy"].unique())
    if not ccys or not ccys.issubset(set(valid_ccys)):
        raise ValueError(f"{symbol}: unexpected quote_ccy values {ccys}")


# ============================================================================
# [2] Alignment
# ============================================================================

def trading_key(ts: pd.Timestamp, tz_name: str, daily: bool) -> pd.Timestamp:
    """Return the alignment key for one bar timestamp.

    Daily: the exchange-local date (as a tz-naive midnight Timestamp). This is
    what makes SGLN.L's 2026-06-28 23:00 UTC bar join AAPL's 2026-06-29 04:00
    UTC bar on the same trading day 2026-06-29.
    Intraday: the UTC open time unchanged.
    """
    if not daily:
        return ts
    return pd.Timestamp(ts.tz_convert(tz_name).date())


class BarFeed:
    """Chronological multi-symbol bar stream.

    Args:
        frames: symbol -> validated DataFrame with the project bar schema.
        tz_for_symbol: symbol -> IANA zone of its exchange (venue adapter owns
            this mapping; the engine stays venue-neutral).
        daily: True when the interval is 1d (switches the alignment key).
    """

    def __init__(self, frames: dict[str, pd.DataFrame],
                 tz_for_symbol: Callable[[str], str], daily: bool) -> None:
        self._daily = daily
        self._bars: dict[str, list[Bar]] = {}
        self._keys: dict[str, list[pd.Timestamp]] = {}
        for symbol, frame in frames.items():
            validate_frame(frame, symbol)
            tz = tz_for_symbol(symbol)
            bars, keys = [], []
            for row in frame.itertuples(index=False):
                ts = pd.Timestamp(row.ts)
                bars.append(Bar(ts=ts, open=float(row.open), high=float(row.high),
                                low=float(row.low), close=float(row.close),
                                volume=float(row.volume),
                                quote_ccy=str(row.quote_ccy)))
                keys.append(trading_key(ts, tz, daily))
            self._bars[symbol] = bars
            self._keys[symbol] = keys
        merged: set[pd.Timestamp] = set()
        for keys in self._keys.values():
            merged.update(keys)
        self.timeline: list[pd.Timestamp] = sorted(merged)
        # Per-symbol cursor into its own bar list, advanced by __iter__.
        self._cursor: dict[str, int] = {s: 0 for s in self._bars}
        # History grown as the stream advances; MarketView reads it.
        self.history: dict[str, list[Bar]] = {s: [] for s in self._bars}

    def __iter__(self) -> Iterator[tuple[int, pd.Timestamp, dict[str, Bar]]]:
        """Yield (step, key, bars_at_key); grows self.history as it goes."""
        for step, key in enumerate(self.timeline):
            out: dict[str, Bar] = {}
            for symbol, keys in self._keys.items():
                cur = self._cursor[symbol]
                if cur < len(keys) and keys[cur] == key:
                    bar = self._bars[symbol][cur]
                    out[symbol] = bar
                    self.history[symbol].append(bar)
                    self._cursor[symbol] = cur + 1
            yield step, key, out

    def peek(self, symbol: str) -> Bar | None:
        """Next not-yet-delivered bar for one symbol, None at the end.

        Exists solely for the lookahead probe arm
        (fixplans/validation/01_no_lookahead.md section 2); the probe is the
        only legal caller.
        """
        cur = self._cursor[symbol]
        bars = self._bars[symbol]
        return bars[cur] if cur < len(bars) else None


# ============================================================================
# [3] FX series
# ============================================================================

class FxSeries:
    """GBPUSD rate lookup without lookahead.

    Rate semantics: USD per 1 GBP (Yahoo GBPUSD=X, verified in
    fixplans/framework/02_data_layer.md section 5.4).

    Availability rule: a bar's CLOSE only exists once the bar has closed, so
    the close of a bar stamped ts (its open time) becomes available at
    ts + bar_duration. rate_at(query) therefore returns the close of the last
    bar with ts + duration <= query. In daily mode this yields the previous
    trading day's close for a fill at today's open, which is exactly the
    no-lookahead convention of fixplans/framework/02_data_layer.md section 5.3.
    Raises instead of extrapolating when nothing is available yet.
    """

    def __init__(self, frame: pd.DataFrame, bar_duration_sec: int) -> None:
        validate_frame(frame, "GBPUSD=X")
        shift = pd.Timedelta(seconds=bar_duration_sec)
        self._avail = [pd.Timestamp(t) + shift for t in frame["ts"].to_list()]
        self._close = [Decimal(str(c)) for c in frame["close"].to_list()]

    def rate_at(self, query_ts: pd.Timestamp) -> Decimal:
        """Latest GBPUSD close already published at query_ts. USD per GBP."""
        lo, hi = 0, len(self._avail) - 1
        if query_ts < self._avail[0]:
            raise ValueError(f"no FX rate available at or before {query_ts}")
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._avail[mid] <= query_ts:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        return self._close[best]


# ============================================================================
# [4] Strategy view
# ============================================================================

class MarketView:
    """What a strategy is allowed to see at one step: bars with ts <= now.

    Backed by the feed's growing history lists, so no per-step copying. The
    probe_bars argument is populated only by the lookahead probe arm and must
    stay None in every reportable run.
    """

    def __init__(self, history: dict[str, list[Bar]], now_key: pd.Timestamp,
                 probe_bars: dict[str, Bar] | None = None) -> None:
        self._history = history
        self.now = now_key
        self._probe = probe_bars

    def symbols(self) -> list[str]:
        return [s for s, bars in self._history.items() if bars]

    def bar(self, symbol: str) -> Bar | None:
        """Latest bar delivered for this symbol, None before its first bar."""
        bars = self._history.get(symbol) or []
        return bars[-1] if bars else None

    def bars(self, symbol: str, n: int) -> list[Bar]:
        """Last n delivered bars, oldest first."""
        bars = self._history.get(symbol) or []
        return bars[-n:]

    def next_bar(self, symbol: str) -> Bar | None:
        """Probe arm only: the t+1 bar. None unless the probe is active."""
        return self._probe.get(symbol) if self._probe else None
