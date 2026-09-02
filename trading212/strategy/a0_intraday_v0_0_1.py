"""A0 decided one minute before the close, executed at the next session's open.

Responsibility: adapt a minute-interval MarketView into the daily view that
a0_v0_0_1.compute_targets already expects, then delegate the entire decision to that
unmodified function. The signal is NOT reimplemented here: this module is pure
plumbing, so there is still exactly one copy of the A0 signal (CLAUDE.md section 4,
ARCHITECTURE.md section 2.0) and changing A0's logic in this file is impossible by
construction. The adaptation runs through two private helpers: _DailyBar, a minimal
Bar stand-in exposing the fields a0_v0_0_1 reads, and _DailyView, a read-only
MarketView shim built fresh per decision that holds no cross-call state and whose
next_bar() always returns None, so no consumer of the shim can look ahead.

The timing rule is the only thing this module decides:

    decision bar   The session's last bar, exchange-local 15:59 by default.
    information    Bars strictly BEFORE that bar, that is through the 15:58 bar, which
                   is everything a trader knows at 15:59:00, one minute before the
                   16:00 close. The current bar is dropped.
    execution      No order may trade in the remaining session. The engine makes an
                   order submitted at 15:59 eligible at 16:00, and no bar exists
                   between 16:00 and the next session's 09:30, so the market order
                   fills at the NEXT session's open with no engine change. Verified on
                   real data 2026-08-22.

Today's daily bar is synthesized from the session's own minute bars: open is the first
minute bar's open, high is the maximum over 09:30 to 15:58, low is the minimum over
the same span, and close is the 15:58 bar's close. Prior days come from the 1d
partitions, injected by the entry layer through make_strategy() and sliced by
exchange-local date strictly before today.

Adjustment splice, measured 2026-08-22: the 1d partitions are back-adjusted for
dividends while the 1m partitions are raw, so the two series sit on different scales.
Over the current window MSFT differs by -0.190%, AMAT by -0.105% and AAPL by -0.051%,
with the rest at noise. Today's minute prices are therefore rescaled by
f = close_1d(previous session) / close_1m_last(previous session), which is knowable at
the decision instant and tracks the ex-dividend step. f is 1.0 on the first session of
a run, where no prior overlap exists.

Out of scope: the A0 signal itself, which belongs to a0_v0_0_1.py; parameter values,
which belong to trading212/config/strategies/a0_v0_0_1.yaml; reading the daily history
off disk, which belongs to the entry layer, currently
scripts/20260822_a0_minute_backtest.py; fills, costs and performance statistics, which
belong to backtest/.

Public functions:
    daily_view(view, params, daily_history, tz)  The synthetic daily view the
                                   signal is computed on; B0 reads the same
                                   one for its gates and its active set.
    make_strategy(daily_history)               Bind the daily history, return callable
    compute_targets(view, portfolio, params)   Plugin-contract entry point

Public constants:
    STRATEGY_NAME     str  "a0_intraday". Must match the file name or strategy_loader
                           refuses to load the module.
    STRATEGY_VERSION  str  "0.0.1". Same check.

Constants:
    DEFAULT_DECISION_TIME  str  Exchange-local decision time, "15:59", overridable per
                                run through params["decision_time_local"]. Source: the
                                timing rule above. The US equity session closes at
                                16:00 America/New_York, and the decision is taken on
                                the last minute bar before that close.
    DEFAULT_EXCHANGE_TZ    str  "America/New_York", overridable per run through
                                params["exchange_tz"].

Parameters, read from params in addition to everything a0_v0_0_1 already reads:
    decision_time_local  str   Exchange-local "HH:MM" the decision fires at. A step at
                               any other minute returns an empty target mapping.
    exchange_tz          str   IANA zone the decision time is expressed in.
    daily_history        dict  Used only on the compute_targets() path: symbol mapped
                               to rows of (iso_date, open, high, low, close) in
                               ascending order. Absent, compute_targets raises
                               ValueError. The make_strategy() path injects the same
                               data as a closure instead, which keeps it out of the run
                               metadata.

Inputs: None. Everything arrives as the view, portfolio and params arguments or as the
    history bound by make_strategy(); the module opens no file and makes no network
    call.
Outputs: None as far as data is concerned. There is one import-time side effect: the
    repository root is prepended to sys.path, if it is not already there, so that the
    absolute import of trading212.strategy.a0_v0_0_1 resolves when an entry script
    runs this file directly.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["STRATEGY_NAME", "STRATEGY_VERSION", "make_strategy",
           "compute_targets", "daily_view"]

import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from trading212.strategy import a0_v0_0_1 as _a0        # noqa: E402

STRATEGY_NAME = "a0_intraday"
STRATEGY_VERSION = "0.0.1"

DEFAULT_DECISION_TIME = "15:59"
DEFAULT_EXCHANGE_TZ = "America/New_York"


class _DailyBar:
    """Minimal Bar stand-in: a0_v0_0_1 reads only these five fields."""

    __slots__ = ("ts", "open", "high", "low", "close", "volume", "quote_ccy")

    def __init__(self, ts, o, h, l, c, volume=0.0, quote_ccy="USD"):
        self.ts, self.open, self.high = ts, o, h
        self.low, self.close, self.volume = l, c, volume
        self.quote_ccy = quote_ccy


class _DailyView:
    """MarketView shim exposing a per-symbol DAILY history to the A0 signal.

    Read-only and built fresh per decision; holds no cross-call state.
    """

    def __init__(self, now, series: dict[str, list[_DailyBar]],
                 fx_bar) -> None:
        self.now = now
        self._series = series
        self._fx_bar = fx_bar
        self._fx_symbol = None

    def bind_fx(self, fx_symbol: str) -> None:
        self._fx_symbol = fx_symbol

    def symbols(self) -> list[str]:
        return [s for s, bars in self._series.items() if bars]

    def bar(self, symbol: str):
        if symbol == self._fx_symbol:
            return self._fx_bar
        bars = self._series.get(symbol) or []
        return bars[-1] if bars else None

    def bars(self, symbol: str, n: int) -> list:
        return (self._series.get(symbol) or [])[-n:]

    def next_bar(self, symbol: str):
        return None


def _session_dates(bars, tz):
    """Exchange-local dates of a minute-bar list, aligned to the list."""
    return [b.ts.tz_convert(tz).date() for b in bars]


def _today_daily_bar(minute_bars, dates, today, scale: float):
    """Synthesize today's daily bar from this session's bars before the last.

    minute_bars must EXCLUDE the current (decision) bar; the caller drops it.
    Returns None when the session has no completed bar yet.
    """
    todays = [b for b, d in zip(minute_bars, dates) if d == today]
    if not todays:
        return None
    return _DailyBar(
        ts=todays[-1].ts,
        o=todays[0].open * scale,
        h=max(b.high for b in todays) * scale,
        l=min(b.low for b in todays) * scale,
        c=todays[-1].close * scale,
        quote_ccy=todays[-1].quote_ccy)


def _splice_scale(minute_bars, dates, today, daily_rows) -> float:
    """Adjustment factor bringing raw minute prices onto the 1d scale.

    Uses only the previous session, so it is knowable at the decision instant
    and steps correctly across an ex-dividend date. Returns 1.0 when the run
    has no prior minute session or no matching daily close.
    """
    prior = [d for d in dates if d < today]
    if not prior or not daily_rows:
        return 1.0
    prev = max(prior)
    last_minute_close = [b.close for b, d in zip(minute_bars, dates)
                         if d == prev][-1]
    prev_iso = prev.isoformat()
    for row in reversed(daily_rows):
        if row[0] == prev_iso:
            if last_minute_close > 0:
                return float(row[4]) / float(last_minute_close)
            break
    return 1.0


def daily_view(view, params, daily_history, tz: str | None = None):
    """Public wrapper over the synthetic daily view.

    B0 needs the SAME daily view A0's signal is computed on, both to read the
    gates for the diagnostics panel and to decide which of the eighteen names
    have enough daily history to hold a slot. Computing either from the hourly
    view would be wrong -- 253 hourly bars span about thirty-six sessions, not
    253 -- and re-deriving the splice here would be a second copy of it.
    """
    return _build_daily_view(view, params, daily_history,
                             tz or params.get("exchange_tz",
                                              DEFAULT_EXCHANGE_TZ))


def _build_daily_view(view, params, daily_history, tz):
    """Assemble the synthetic daily view for the current decision instant.

    Every element is drawn from information available at the decision bar:
    daily rows STRICTLY before today, plus this session's completed minute
    bars excluding the decision bar itself.
    """
    today = view.now.tz_convert(tz).date()
    today_iso = today.isoformat()
    lookback = int(params.get("tsmom_lookback", 252))
    trend_ma = int(params.get("trend_ma", 200))
    # Window sizes MUST match what a0_v0_0_1.compute_targets asks its view
    # for, or the two arms compute different signals from the same data. It
    # requests bars(state_symbol, 8000) and bars(symbol, max(lb, ma) + 1).
    # The vol gate's percentile is EXPANDING, so truncating the state
    # symbol's history silently moves the 0.80 threshold: an earlier build
    # capped it at 2000 bars and flipped the 2026-07-27 gate open when the
    # daily arm had it shut. Keep the state window at least as deep as 8000.
    deep = 8000
    span = max(lookback, trend_ma) + 8

    series: dict[str, list[_DailyBar]] = {}
    for symbol in list(params["trade_symbols"]) + [params["state_symbol"]]:
        rows = daily_history.get(symbol) or []
        # Causal slice: strictly before today's exchange-local date.
        past = [r for r in rows if r[0] < today_iso]
        keep = deep if symbol == params["state_symbol"] else span
        past = past[-keep:]
        # The engine hands out the decision bar itself; drop it so the
        # information set stops one minute before the close.
        minute_bars = view.bars(symbol, 2 * 390)[:-1]
        dates = _session_dates(minute_bars, tz)
        scale = _splice_scale(minute_bars, dates, today, rows)
        bars = [_DailyBar(pd.Timestamp(r[0]), float(r[1]), float(r[2]),
                          float(r[3]), float(r[4])) for r in past]
        today_bar = _today_daily_bar(minute_bars, dates, today, scale)
        if today_bar is not None:
            bars.append(today_bar)
        series[symbol] = bars

    fx_bars = view.bars(params["fx_symbol"], 4000)[:-1] \
        or view.bars(params["fx_symbol"], 4000)
    fx_bar = fx_bars[-1] if fx_bars else None
    shim = _DailyView(view.now, series, fx_bar)
    shim.bind_fx(params["fx_symbol"])
    return shim


def _decide(view, portfolio, params, daily_history) -> dict[str, Decimal]:
    """Gate to the decision bar, then delegate to the unmodified A0 signal."""
    tz = params.get("exchange_tz", DEFAULT_EXCHANGE_TZ)
    now = view.now
    if now.tzinfo is None:
        return {}
    local = now.tz_convert(tz)
    if local.strftime("%H:%M") != params.get("decision_time_local",
                                             DEFAULT_DECISION_TIME):
        return {}
    # The clock alone is not enough. The FX symbol trades around the clock and
    # supplies timeline keys on days the US equity market is shut, so a purely
    # time-based gate fires on US holidays too (measured 2026-08-22: 5m fired
    # on 2026-06-19 and 2026-07-03, neither a trading day). Require that the
    # state symbol actually delivered a bar AT this key -- for intraday feeds
    # the alignment key IS the bar's open timestamp, so equality is the test.
    state_bar = view.bar(params["state_symbol"])
    if state_bar is None or pd.Timestamp(state_bar.ts) != pd.Timestamp(now):
        return {}
    shim = _build_daily_view(view, params, daily_history, tz)
    return _a0.compute_targets(shim, portfolio, params)


def make_strategy(daily_history: dict[str, list]):
    """Bind the injected daily history and return the strategy callable.

    daily_history maps symbol -> list of (iso_date, open, high, low, close)
    ordered ascending. It is read-only; the returned callable keeps no state
    between calls and performs no I/O.
    """
    frozen = {s: list(rows) for s, rows in daily_history.items()}

    def strategy(view, portfolio, params) -> dict[str, Decimal]:
        return _decide(view, portfolio, params, frozen)

    return strategy


def compute_targets(view, portfolio, params) -> dict[str, Decimal]:
    """Plugin-contract entry point; daily history arrives inside params."""
    history = params.get("daily_history")
    if history is None:
        raise ValueError(
            "a0_intraday needs params['daily_history'] (symbol -> rows of "
            "(iso_date, o, h, l, c)); the entry layer normally injects it "
            "through make_strategy() instead, to keep it out of the run "
            "metadata")
    return _decide(view, portfolio, params, history)
