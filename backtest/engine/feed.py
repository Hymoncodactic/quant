"""Bar feed: aligned multi-symbol bar stream with a hard data-quality gate.

Responsibility: turn per-symbol DataFrames into a chronologically advancing
stream of (step, key, {symbol: Bar}), provide a lookahead-free GBPUSD rate
lookup, and expose a cutoff-enforced view of history to the strategy.
Alignment rules (docs/backtest/framework/02_data_layer.md sections 3 and 4, verified
on real data 2026-08-20): intraday bars align on their UTC open timestamp;
daily bars align on the exchange-local trading day, because during BST a London
daily bar's raw UTC timestamp falls at 23:00 of the previous UTC day, so
aligning daily data on raw UTC dates would shift London by one day; a symbol
missing a bar on some key is normal, caused by holiday asymmetry or suspension,
and never drops the timeline key.

Out of scope: reading parquet, which belongs to backtest/t212/data_source.py
and backtest/okx/data_source.py; the exchange time-zone mapping, which the
venue adapter owns and injects as tz_for_symbol so the engine stays
venue-neutral; every venue cost rule, which belongs to backtest/t212/costs.py.

Public functions and classes:
    validate_frame(frame, symbol, valid_ccys)   Data-quality gate; raises on
                                                violation, because silently
                                                skipping bad bars is
                                                survivorship bias by another
                                                name.
    trading_key(ts, tz_name, daily)             Alignment key for one bar
                                                timestamp.
    BarFeed(frames, tz_for_symbol, daily)       The aligned stream; iterating
                                                it yields (step, key, bars) and
                                                grows self.history. Its peek()
                                                method exists solely for the
                                                lookahead probe arm, whose only
                                                caller is engine.py.
    FxSeries(frame, bar_duration_sec)           rate_at(ts) returns the latest
                                                GBPUSD close already published
                                                at ts, in USD per GBP, and
                                                raises rather than
                                                extrapolating.
    MarketView(history, now_key, probe_bars)    What a strategy is allowed to
                                                see at one step: bars with
                                                ts at or before now.

Constants:
    VALID_QUOTE_CCYS   tuple[str, ...]   Quote currencies known to the engine
                       across both venue lines: USD, GBP and GBp for t212
                       equity data, USDT for the crypto line's Binance-sourced
                       bars. This is only the default; a venue adapter may pass
                       a tighter set to reject foreign frames.

The OHLC ordering test inside validate_frame carries a relative tolerance of
1e-9. Source: measurement of 2026-08-21 on adjusted equity data, where every
violation was a 1-ULP artifact of the adjustment multiply, about 1e-16
relative. Anything beyond the tolerance still stops the run.

Inputs: None. Bar data arrives as already-constructed DataFrames from the venue
    data source; this module opens no file and makes no network call.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
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

    Gate list is docs/backtest/framework/02_data_layer.md section 7. A violation
    stops the run: silently skipping bad bars is survivorship bias by another
    name.
    """
    if frame.empty:
        raise ValueError(f"{symbol}: empty frame")
    ts = frame["ts"]
    if not ts.is_monotonic_increasing or ts.duplicated().any():
        raise ValueError(f"{symbol}: ts not strictly increasing")
    # Relative tolerance for the ordering test: Yahoo's adjusted prices carry
    # 1-ULP inconsistencies (measured 2026-08-21: equity violations are all
    # ~1e-16 relative, e.g. close exceeding high by one float step after the
    # adjustment multiply). Those are representation noise, not data errors;
    # anything beyond the tolerance still stops the run.
    tol = 1e-9
    upper = frame[["open", "close"]].max(axis=1)
    lower = frame[["open", "close"]].min(axis=1)
    bad_hl = (frame["high"] < upper - tol * upper.abs()) | \
             (frame["low"] > lower + tol * lower.abs())
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
        (docs/backtest/validation/01_no_lookahead.md section 2); the probe is the
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
    docs/backtest/framework/02_data_layer.md section 5.4).

    Availability rule: a bar's CLOSE only exists once the bar has closed, so
    the close of a bar stamped ts (its open time) becomes available at
    ts + bar_duration. rate_at(query) therefore returns the close of the last
    bar with ts + duration <= query. In daily mode this yields the previous
    trading day's close for a fill at today's open, which is exactly the
    no-lookahead convention of docs/backtest/framework/02_data_layer.md section 5.3.
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
