"""Market data for the live A0 hourly cycle: refresh, load, cutoff view.

Responsibility: keep the curated store current for the symbols A0 needs,
read it back, and present the exact information set the backtest's 1h arm
sees at a decision -- hourly bars up to and including the 15:30 decision bar,
plus the adjusted daily rows the intraday shim splices today's session onto.

Trading 212 publishes no market data (WORKING_MEMORY open item 2: the
equity price, quote and candle endpoints all answer 404), so bars come from
the same Yahoo pipeline the backtest was built on, through
trading212/ingest/yahoo_bars.py. Using a different source or a different
adjustment convention would make live and backtest incomparable
(fixplans/t212/a0/02_execution.md section 5).

Why the intraday view keeps the decision bar: the shim drops it itself with
a trailing slice, and it needs the bar present to prove the session is
actually trading at that key. That mirrors the engine exactly, which hands
the strategy the current bar and lets the shim discard it
(trading212/strategy/a0_intraday_v0_0_1.py, information rule).

Out of scope: fetching and partition naming, which belong to
trading212/ingest/yahoo_bars.py; the splice arithmetic and the synthetic
daily bar, which belong to trading212/strategy/a0_intraday_v0_0_1.py; the
session calendar, which belongs to trading212/execution/instruments.py;
deciding when to run, which belongs to
trading212/execution/session_cycle.py.

Public functions:
    group_for(symbol)                       Universe group holding one symbol.
    refresh_bars(symbols, interval)         Re-fetch and store one interval.
    load_frames(symbols, interval, start, end)  Read stored bars per symbol.
    daily_rows(symbols, start, end)         Shim-format adjusted daily rows.
    build_view(frames, cutoff_ts)           Cutoff-enforced LiveMarketView.
    assert_intraday_ready(frames, decision_key, exact_symbols, fx_symbol)
                                            Freshness gate for one decision.

Public classes:
    LiveBar          One OHLCV bar; field-compatible with the engine's Bar.
    LiveMarketView   Read-only view exposing bar/bars/symbols/now.

Constants:
    GROUPS           tuple  Curated group search order, mirroring
                            backtest/t212/data_source.py GROUPS so both sides
                            resolve a symbol to the same partitions.
    FX_SYMBOL        str    "GBPUSD=X".
    FX_LAG_MINUTES   int    90. US equity 1h bars stamp on the half hour and
                            FX 1h bars on the hour, so the FX bar in force at
                            a 15:30 decision always starts 90 minutes earlier
                            and its close is 30 minutes stale. Verified on
                            690 of 691 backtest decision keys; the one
                            exception was an FX data hole, which this gate
                            now refuses rather than silently mispricing.
    INTRADAY_SESSIONS_LOADED int  40. Sessions of 1h history handed to the
                            shim. It only reads today's bars and the previous
                            session's last bar, so a deeper window changes no
                            number and only costs memory.

Inputs:
    data/t212/curated/<group>/<symbol>/<interval>/*.parquet
Outputs:
    data/t212/curated/<group>/<symbol>/<interval>/*.parquet   (refresh_bars)

Change log:
    2026-08-21  Created for the daily A0 cycle: daily refresh, daily cutoff
                view, day-granularity freshness gate.
    2026-08-22  Rewritten for the hourly arm. The daily-only loader became a
                general one; added daily_rows() for the shim, an hourly
                cutoff view keyed on the decision bar, and a freshness gate
                that pins the FX bar to decision_key minus FX_LAG_MINUTES.
                The old five-day FX tolerance was a daily-scale rule and is
                useless at 1h.
"""

from __future__ import annotations

__all__ = ["group_for", "refresh_bars", "load_frames", "daily_rows",
           "build_view", "assert_intraday_ready", "LiveBar", "LiveMarketView",
           "GROUPS", "FX_SYMBOL", "FX_LAG_MINUTES",
           "INTRADAY_SESSIONS_LOADED"]

from dataclasses import dataclass

import pandas as pd

from common.logging_setup import get_logger
from common.paths import equity_curated_root, equity_interval_dir
from trading212.ingest.yahoo_bars import (UNIVERSE, fetch_interval, write_daily,
                                          write_intraday)

log = get_logger("t212.execution")

GROUPS = ("us_equity", "us_etf", "uk_tradable")
FX_SYMBOL = "GBPUSD=X"
FX_LAG_MINUTES = 90
INTRADAY_SESSIONS_LOADED = 40

_TZ_LONDON = "Europe/London"
_TZ_NEW_YORK = "America/New_York"

# Fetch spans per interval, mirroring trading212/ingest/yahoo_bars.INTERVALS:
# the daily series is refetched whole because adjustment is retroactive, and
# 1h is capped by the vendor at 730 sessions.
_FETCH_SPAN = {"1d": (None, None), "1h": (730, None)}


@dataclass(frozen=True)
class LiveBar:
    """One OHLCV bar; field-compatible with backtest/engine/types.py Bar."""
    ts: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_ccy: str


# ============================================================================
# [1] Refresh and load
# ============================================================================

def group_for(symbol: str) -> str:
    """Return the universe group holding one symbol."""
    for group, members in UNIVERSE.items():
        if symbol in members:
            return group
    raise KeyError(f"{symbol!r} is not in the ingest universe "
                   f"(trading212/ingest/yahoo_bars.py UNIVERSE)")


def refresh_bars(symbols: list[str], interval: str) -> dict[str, int]:
    """Re-fetch and store one interval for every symbol.

    The whole span is refetched rather than appended: adjustment is
    retroactive, so an ex-dividend date rewrites the entire daily series and
    an appended file would straddle two scales. Returns rows fetched per
    symbol; an empty fetch is logged and left to the freshness gate to
    reject, so one flaky symbol does not abort the others.
    """
    if interval not in _FETCH_SPAN:
        raise ValueError(f"unsupported interval {interval!r}, "
                         f"expected one of {sorted(_FETCH_SPAN)}")
    lookback, chunk = _FETCH_SPAN[interval]
    rows: dict[str, int] = {}
    for symbol in symbols:
        frame = fetch_interval(symbol, interval, lookback, chunk)
        rows[symbol] = len(frame)
        if frame.empty:
            log.warning("[bars] %s refresh returned nothing for %s",
                        interval, symbol)
            continue
        group = group_for(symbol)
        if interval == "1d":
            write_daily(group, symbol, frame)
        else:
            write_intraday(group, symbol, interval, frame)
    log.info("[bars] refreshed %s for %d symbols: %s", interval, len(symbols), rows)
    return rows


def _exchange_tz(symbol: str) -> str:
    """IANA zone of a symbol's exchange.

    London listings and the Yahoo FX series carry London-local stamps; every
    other symbol in the A0 universe is US. Same rule as
    backtest/t212/instruments.py exchange_tz().
    """
    return _TZ_LONDON if symbol.endswith(".L") or symbol.endswith("=X") \
        else _TZ_NEW_YORK


def _symbol_dir(symbol: str, interval: str):
    for group in GROUPS:
        candidate = equity_interval_dir(group, symbol, interval)
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        f"no {interval} data for {symbol} under {equity_curated_root()} "
        f"(groups {GROUPS}); run the refresh first")


def load_frames(symbols: list[str], interval: str, start: str,
                end: str) -> dict[str, pd.DataFrame]:
    """Read stored bars per symbol, sliced by exchange-local day.

    The window is interpreted in the exchange's local calendar, not raw UTC,
    because a London bar stamps 23:00 UTC of the previous day during BST.
    A missing symbol raises rather than vanishing from the result: silently
    dropping a symbol would change the strategy's slot count.
    """
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        folder = _symbol_dir(symbol, interval)
        parts = sorted(folder.glob("*.parquet"))
        if not parts:
            raise FileNotFoundError(f"{folder} holds no parquet files")
        frame = pd.concat([pd.read_parquet(p) for p in parts],
                          ignore_index=True)
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        frame = (frame.drop_duplicates(subset=["ts"], keep="last")
                 .sort_values("ts").reset_index(drop=True))
        local_day = frame["ts"].dt.tz_convert(_exchange_tz(symbol)).dt.date
        mask = (local_day >= pd.Timestamp(start).date()) & \
               (local_day <= pd.Timestamp(end).date())
        frame = frame.loc[mask].reset_index(drop=True)
        if symbol == FX_SYMBOL:
            # Yahoo stamps the FX close from a different session cut than the
            # high and low, leaving close outside [low, high] by up to ~6e-4
            # relative. Only the close is ever read, so enveloping the
            # untouched fields changes no number and only keeps the frame
            # self-consistent. Same repair as backtest/t212/data_source.py.
            frame["high"] = frame[["high", "open", "close"]].max(axis=1)
            frame["low"] = frame[["low", "open", "close"]].min(axis=1)
        out[symbol] = frame
    return out


def daily_rows(symbols: list[str], start: str,
               end: str) -> dict[str, list[tuple]]:
    """Adjusted daily rows in the shim's format, ascending.

    Each row is (iso_local_date, open, high, low, close), matching
    scripts/20260822_a0_intraday_backtest.py daily_history() exactly, so the
    live shim receives the same structure the backtest fed it.
    """
    frames = load_frames(symbols, "1d", start, end)
    out: dict[str, list[tuple]] = {}
    for symbol, frame in frames.items():
        local = frame["ts"].dt.tz_convert(_TZ_NEW_YORK).dt.date
        out[symbol] = [
            (d.isoformat(), float(o), float(h), float(l), float(c))
            for d, o, h, l, c in zip(local, frame["open"], frame["high"],
                                     frame["low"], frame["close"])]
    return out


# ============================================================================
# [2] Cutoff view and freshness gate
# ============================================================================

def build_view(frames: dict[str, pd.DataFrame],
               cutoff_ts: pd.Timestamp) -> "LiveMarketView":
    """Build the view: every bar whose ts is at or before cutoff_ts.

    cutoff_ts is the decision bar's own timestamp, so the decision bar is
    included and everything after it -- notably any later in-progress bar the
    vendor may already publish -- is dropped. The shim then discards the
    decision bar itself, which is what makes the live information set
    identical to the backtest's at the same key.
    """
    history: dict[str, list[LiveBar]] = {}
    for symbol, frame in frames.items():
        keep = frame["ts"] <= cutoff_ts
        history[symbol] = [
            LiveBar(ts=row.ts, open=float(row.open), high=float(row.high),
                    low=float(row.low), close=float(row.close),
                    volume=float(row.volume), quote_ccy=str(row.quote_ccy))
            for row in frame.loc[keep].itertuples(index=False)]
    return LiveMarketView(history, cutoff_ts)


def assert_intraday_ready(frames: dict[str, pd.DataFrame],
                          decision_key: pd.Timestamp,
                          exact_symbols: list[str], fx_symbol: str) -> None:
    """Refuse to decide unless every series is exactly where it must be.

    Three conditions, each a way the decision would silently diverge from the
    backtest:
      1. Every trade symbol and the state symbol has a bar AT decision_key.
         That bar is the session's own 15:30 stub; its absence means the
         session is not trading normally or the feed is behind.
      2. Each of those symbols also has the preceding bar, which carries the
         information the decision is actually computed on.
      3. The FX series has a bar at exactly decision_key minus
         FX_LAG_MINUTES. The backtest's availability rule always lands there;
         under an FX hole it would silently fall back to a much older rate
         and price the slots wrong, so the gate pins the bar instead of
         tolerating staleness.
    """
    problems: list[str] = []
    prior_key = decision_key - pd.Timedelta(hours=1)
    for symbol in exact_symbols:
        stamps = set(frames[symbol]["ts"])
        if decision_key not in stamps:
            newest = frames[symbol]["ts"].max() if not frames[symbol].empty else None
            problems.append(f"{symbol}: no bar at decision key {decision_key} "
                            f"(newest {newest})")
        elif prior_key not in stamps:
            problems.append(f"{symbol}: missing the information bar {prior_key}")
    fx_key = decision_key - pd.Timedelta(minutes=FX_LAG_MINUTES)
    if fx_key not in set(frames[fx_symbol]["ts"]):
        newest = frames[fx_symbol]["ts"].max() if not frames[fx_symbol].empty else None
        problems.append(f"{fx_symbol}: no bar at {fx_key} "
                        f"(decision key minus {FX_LAG_MINUTES}m; newest {newest})")
    if problems:
        raise RuntimeError("intraday freshness gate failed: " + "; ".join(problems))


class LiveMarketView:
    """Cutoff-enforced market view; duck type of the engine's MarketView."""

    def __init__(self, history: dict[str, list[LiveBar]],
                 now_key: pd.Timestamp) -> None:
        self._history = history
        self.now = now_key

    def symbols(self) -> list[str]:
        return [s for s, bars in self._history.items() if bars]

    def bar(self, symbol: str) -> LiveBar | None:
        """Latest bar at or before the cutoff, None before the first one."""
        bars = self._history.get(symbol) or []
        return bars[-1] if bars else None

    def bars(self, symbol: str, n: int) -> list[LiveBar]:
        """Last n bars at or before the cutoff, oldest first."""
        bars = self._history.get(symbol) or []
        return bars[-n:]

    def next_bar(self, symbol: str) -> None:
        """Always None: the live view has no probe arm."""
        return None
