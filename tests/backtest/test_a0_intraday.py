"""Discriminating tests for the A0 minute-timing variant.

Responsibility: pin the properties of
trading212/strategy/a0_intraday_v0_0_1.py that, if broken, would leak future
information into the decision. Those properties are the decision-time gate,
the refusal to trade on a timestamp without a time zone, the exclusion of the
current minute bar from the information set, the strictly causal slice of
daily history, the splice factor that rescales today's raw minute prices onto
the back-adjusted daily scale, the length of the lookback windows handed to
the signal, and the delegation of the signal itself to the daily module. Every
test names the defect it catches; a test that passes under both the correct
and the defective implementation is worthless (backtest-discipline section 6).
Two tests reach for module-private names on purpose: they call
a0_intraday_v0_0_1._build_daily_view to assert on the synthetic daily view
directly, because comparing only the returned targets was measured to be
non-discriminating for the causal-slice defect, and one test temporarily
substitutes a0_v0_0_1.compute_targets to prove the delegation happens.

Out of scope: the daily A0 signal itself, whose own tests belong with
trading212/strategy/a0_v0_0_1.py; engine behavior such as fill timing and
costs, which is covered by tests/backtest/test_engine.py and
tests/backtest/test_broker.py; the shared fixtures in
tests/backtest/conftest.py, which this module deliberately does not import
because it needs minute-interval stand-ins rather than daily frames.

Public functions:
    params(**over)
        Full strategy parameter dictionary for the decision-time variant, with
        named entries overridden as requested.
    build_history(n_days, start_price, drift)
        Ascending daily rows per symbol, ending on the session before the
        decision day.
    minute_session(day, prices, ccy)
        One session of minute bars stamped 15:55 through 15:59 exchange local.
    make_case(decision_close, current_bar_close, prev_session)
        Assemble a whole decision-day scene: daily history, the previous
        minute session, today's five minute bars, and the decision instant.
    run(hist, minute, now, p)
        Invoke the strategy over a scene and return its target dictionary.
    test_no_targets_outside_the_decision_bar()
        Non-decision minutes must produce no targets, the decision minute must
        produce some.
    test_naive_timestamp_never_trades()
        A timestamp without a time zone must not slip past the time gate.
    test_decision_ignores_its_own_bar()
        An absurd price injected into the 15:59 bar must move nothing, since
        that bar's close only exists at 16:00.
    test_decision_does_use_the_prior_bar()
        Discrimination guard for the test above: moving the 15:58 close across
        the momentum threshold must change the targets.
    test_daily_history_slice_is_strictly_causal()
        A daily row stamped today must never reach the signal.
    test_splice_scale_rescales_todays_minute_prices()
        Halving the previous session's minute closes must halve the implied
        splice factor and change the targets.
    test_first_session_falls_back_to_unit_scale()
        A missing prior session must fall back to a factor of 1.0 rather than
        raise or yield zero.
    test_state_symbol_window_is_not_truncated()
        The state symbol's history must not be capped below what the daily
        signal asks for, because its percentile gate is expanding.
    test_trade_symbol_window_covers_the_lookback()
        The per-symbol window must cover lookback plus one bar.
    test_signal_is_delegated_not_reimplemented()
        The daily module's own compute_targets must be the one that runs.
    test_contract_entry_point_requires_history()
        The plugin-contract entry point must raise when daily history is
        missing, never trade on an empty history.

Public classes:
    FakeBar
        Minimal bar stand-in carrying ts, OHLC, volume and quote currency.
    FakeView
        Minute-interval market view stand-in serving bars from an in-memory
        dictionary. Its next_bar always returns None, so no test can see the
        future through the view.

Constants:
    NY
        "America/New_York". Exchange time zone of the synthetic scene; the
        decision time is expressed in it.
    SYMS
        ["AAA", "BBB"]. Traded symbols of the synthetic scene.
    STATE
        "QQQ". State symbol driving the volatility and trend gates.
    FX
        "GBPUSD=X". Symbol the strategy reads the exchange rate from; the
        spelling follows the venue data layout used elsewhere in the project.

Inputs: None. Every bar and every daily row is synthesized in process; no path
under data/ is read.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backtest.engine.engine import PortfolioView
from trading212.strategy import a0_intraday_v0_0_1 as a0m


NY = "America/New_York"
SYMS = ["AAA", "BBB"]
STATE = "QQQ"
FX = "GBPUSD=X"


class FakeBar:
    __slots__ = ("ts", "open", "high", "low", "close", "volume", "quote_ccy")

    def __init__(self, ts, o, h, l, c, ccy="USD"):
        self.ts, self.open, self.high, self.low, self.close = ts, o, h, l, c
        self.volume, self.quote_ccy = 1e6, ccy


class FakeView:
    """Minute-interval MarketView stand-in."""

    def __init__(self, now, minute: dict[str, list[FakeBar]]):
        self.now = now
        self._m = minute

    def bars(self, symbol, n):
        return (self._m.get(symbol) or [])[-n:]

    def bar(self, symbol):
        bars = self._m.get(symbol) or []
        return bars[-1] if bars else None

    def symbols(self):
        return list(self._m)

    def next_bar(self, symbol):
        return None


def params(**over):
    base = {
        "trade_symbols": list(SYMS), "state_symbol": STATE, "fx_symbol": FX,
        "signal_mode": "tsmom252", "tsmom_lookback": 252, "trend_ma": 200,
        "vol_window": 20, "vol_pct_threshold": 0.80, "vol_min_history": 756,
        "use_vol_gate": False, "use_trend_gate": False, "warmup_bars": 260,
        "live_from": "2000-01-01", "slot_headroom": 0.99,
        "decision_time_local": "15:59", "exchange_tz": NY,
    }
    base.update(over)
    return base


def build_history(n_days=400, start_price=100.0, drift=0.10):
    """Ascending daily rows ending the session BEFORE the decision day."""
    days = pd.bdate_range("2024-01-02", periods=n_days, tz=NY)
    rows = {}
    for sym in SYMS + [STATE]:
        out = []
        for i, d in enumerate(days):
            c = start_price + drift * i
            out.append((d.date().isoformat(), c * 0.99, c * 1.01,
                        c * 0.98, c))
        rows[sym] = out
    return rows, days


def minute_session(day, prices, ccy="USD"):
    """One session of minute bars 15:55..15:59 (only the tail is read)."""
    out = []
    for k, px in enumerate(prices):
        ts = pd.Timestamp(f"{day} 15:{55 + k}:00", tz=NY).tz_convert("UTC")
        out.append(FakeBar(ts, px, px * 1.001, px * 0.999, px, ccy))
    return out


def make_case(decision_close=200.0, current_bar_close=200.0,
              prev_session=True):
    """A decision-day scene: history, prior session, today's five bars."""
    hist, days = build_history()
    last_hist_day = days[-1].date().isoformat()
    today = (days[-1] + pd.Timedelta(days=1)).date().isoformat()
    minute = {}
    for sym in SYMS + [STATE]:
        bars = []
        if prev_session:
            # Previous session's last minute close equals its daily close, so
            # the splice scale is exactly 1.0 unless a test changes it.
            prev_close = hist[sym][-1][4]
            bars += minute_session(last_hist_day, [prev_close] * 5)
        bars += minute_session(
            today, [decision_close] * 4 + [current_bar_close])
        minute[sym] = bars
    minute[FX] = minute_session(today, [1.27] * 5, ccy="GBP")
    now = pd.Timestamp(f"{today} 15:59:00", tz=NY).tz_convert("UTC")
    return hist, minute, now, today, last_hist_day


def run(hist, minute, now, p=None):
    view = FakeView(now, minute)
    pf = PortfolioView(cash_gbp=Decimal("10000"),
                       available_cash_gbp=Decimal("10000"),
                       positions={}, pending_signed_qty={})
    return a0m.make_strategy(hist)(view, pf, p or params())


# ---------------------------------------------------------------- gate

def test_no_targets_outside_the_decision_bar():
    """DEFECT CAUGHT: the decision-time gate is dropped, so the strategy
    trades on every minute bar instead of once per session."""
    hist, minute, now, _, _ = make_case()
    for hhmm in ("09:30", "12:00", "15:58"):
        day = now.tz_convert(NY).date().isoformat()
        other = pd.Timestamp(f"{day} {hhmm}:00", tz=NY).tz_convert("UTC")
        assert run(hist, minute, other) == {}, f"traded at {hhmm}"
    assert run(hist, minute, now), "decision bar produced nothing"


def test_naive_timestamp_never_trades():
    """DEFECT CAUGHT: tz-naive keys silently bypass the time gate."""
    hist, minute, now, _, _ = make_case()
    assert run(hist, minute, pd.Timestamp("2025-07-21 15:59:00")) == {}


# ------------------------------------------------------- information cutoff

def test_decision_ignores_its_own_bar():
    """The core no-lookahead property: information stops at the 15:58 bar.

    DEFECT CAUGHT: the shim forgets to drop the current bar (`[:-1]`), so the
    15:59 bar's close -- which only exists at 16:00, the close itself --
    reaches the signal. Injecting an absurd price into that bar must move
    NOTHING."""
    hist, m_norm, now, _, _ = make_case(current_bar_close=200.0)
    _, m_spike, _, _, _ = make_case(current_bar_close=20000.0)
    assert run(hist, m_norm, now) == run(hist, m_spike, now)


def test_decision_does_use_the_prior_bar():
    """Discrimination guard for the test above: if the 15:58 bar were also
    ignored the previous test would pass vacuously. Moving the 15:58 close
    across the TSMOM threshold MUST change the targets."""
    hist, m_up, now, _, _ = make_case(decision_close=200.0)
    _, m_down, _, _, _ = make_case(decision_close=1.0)
    assert run(hist, m_up, now) != run(hist, m_down, now)


def test_daily_history_slice_is_strictly_causal():
    """DEFECT CAUGHT: the causal slice uses `<=` instead of `<`, letting a
    daily row stamped TODAY (whose close is the 16:00 close) into the
    signal."""
    hist, minute, now, today, _ = make_case()
    poisoned = {s: rows + [(today, 1e5, 1e5, 1e5, 1e5)]
                for s, rows in hist.items()}
    # Assert the property directly on the shim. Comparing only the returned
    # targets is NOT discriminating: an off-by-one in the slice shifts the
    # TSMOM reference bar by one day, which a smooth synthetic price series
    # absorbs without flipping any sign (verified by injecting `<=` -- the
    # targets stayed equal while the poisoned row was plainly inside).
    for hist_variant, tag in ((hist, "clean"), (poisoned, "poisoned")):
        shim = a0m._build_daily_view(
            FakeView(now, minute), params(), hist_variant, NY)
        for sym in SYMS + [STATE]:
            bars = shim.bars(sym, 8000)
            assert not any(b.close == 1e5 for b in bars), \
                f"{tag}: a row dated today reached the signal for {sym}"
            stamps = [b.ts for b in bars[:-1]]
            assert all(pd.Timestamp(t).date().isoformat() < today
                       for t in stamps), f"{tag}: non-causal bar in {sym}"
    assert run(poisoned, minute, now) == run(hist, minute, now)


# ------------------------------------------------------------ splice scale

def test_splice_scale_rescales_todays_minute_prices():
    """DEFECT CAUGHT: the 1d/1m adjustment splice is skipped (f hardcoded to
    1), so a back-adjusted daily history is compared against raw minute
    prices. Halving the previous session's minute closes must halve the
    implied scale and change the targets."""
    hist, minute, now, _, _ = make_case(decision_close=200.0)
    base = run(hist, minute, now)
    scaled = {s: list(b) for s, b in minute.items()}
    prev_day = sorted({b.ts.tz_convert(NY).date() for b in scaled[SYMS[0]]})[0]
    for sym in SYMS + [STATE]:
        scaled[sym] = [
            (FakeBar(b.ts, b.open * 2, b.high * 2, b.low * 2, b.close * 2,
                     b.quote_ccy) if b.ts.tz_convert(NY).date() == prev_day
             else b)
            for b in scaled[sym]]
    assert run(hist, scaled, now) != base


def test_first_session_falls_back_to_unit_scale():
    """DEFECT CAUGHT: a missing prior session raises or yields a zero scale
    instead of falling back to f = 1."""
    hist, minute, now, _, _ = make_case(prev_session=False)
    assert run(hist, minute, now)


# --------------------------------------------------- state-symbol window

def test_state_symbol_window_is_not_truncated():
    """DEFECT CAUGHT: the state symbol's history is capped below what
    a0_v0_0_1 asks its view for (it requests 8000 bars). The vol gate's
    percentile is EXPANDING, so a shorter history silently moves the 0.80
    threshold -- an earlier build capped it at 2000 and flipped the
    2026-07-27 gate open while the daily arm had it shut."""
    long_hist, days = build_history(n_days=3000)
    # The decision day must sit AFTER the whole history, or the causal slice
    # legitimately trims it and the test would measure the wrong thing.
    today = (days[-1] + pd.Timedelta(days=1)).date().isoformat()
    prev = days[-1].date().isoformat()
    minute = {}
    for sym in SYMS + [STATE]:
        close = long_hist[sym][-1][4]
        minute[sym] = (minute_session(prev, [close] * 5)
                       + minute_session(today, [close] * 5))
    minute[FX] = minute_session(today, [1.27] * 5, ccy="GBP")
    now = pd.Timestamp(f"{today} 15:59:00", tz=NY).tz_convert("UTC")
    shim = a0m._build_daily_view(
        FakeView(now, minute), params(), long_hist, NY)
    got = len(shim.bars(STATE, 8000))
    assert got >= 3000, f"state history truncated to {got} bars"


def test_trade_symbol_window_covers_the_lookback():
    """DEFECT CAUGHT: the per-symbol window is trimmed below lookback + 1, so
    TSMOM silently compares against the wrong reference bar."""
    hist, minute, now, _, _ = make_case()
    shim = a0m._build_daily_view(FakeView(now, minute), params(), hist, NY)
    assert len(shim.bars(SYMS[0], 253)) == 253


# ------------------------------------------------------------- delegation

def test_signal_is_delegated_not_reimplemented():
    """The single-copy rule: this module must call a0_v0_0_1's own
    compute_targets. DEFECT CAUGHT: someone reimplements the signal here and
    the two copies drift apart."""
    calls = []
    original = a0m._a0.compute_targets

    def spy(view, portfolio, p):
        calls.append(view)
        return original(view, portfolio, p)

    a0m._a0.compute_targets = spy
    try:
        hist, minute, now, _, _ = make_case()
        run(hist, minute, now)
    finally:
        a0m._a0.compute_targets = original
    assert len(calls) == 1, "A0's own compute_targets was never called"


def test_contract_entry_point_requires_history():
    """The plugin-contract path must fail loudly, never silently trade on an
    empty history. DEFECT CAUGHT: a missing daily_history defaults to {}."""
    hist, minute, now, _, _ = make_case()
    with pytest.raises(ValueError, match="daily_history"):
        a0m.compute_targets(
            FakeView(now, minute),
            PortfolioView(cash_gbp=Decimal("10000"),
                          available_cash_gbp=Decimal("10000"),
                          positions={}, pending_signed_qty={}),
            params())


def test_no_decision_when_the_equity_market_is_shut():
    """DEFECT CAUGHT: the decision gate keys on the wall clock alone, so it
    fires on US market holidays, where GBPUSD=X trades around the clock and
    supplies a timeline key at the decision time while no equity bar exists.
    Measured on real data 2026-08-22: without this guard the 5m arm decided on
    2026-06-19 and 2026-07-03, neither a US trading session."""
    hist, minute, now, _, _ = make_case()
    # Strip today's bars from the state symbol only: the key still lands at
    # the decision time (the FX leg supplies it) but QQQ has no bar there.
    today = now.tz_convert(NY).date()
    shut = {s: list(b) for s, b in minute.items()}
    shut[STATE] = [b for b in shut[STATE] if b.ts.tz_convert(NY).date() != today]
    assert run(hist, shut, now) == {}, "decided on a session with no state bar"
    # Discrimination guard: with the state bar present it DOES decide.
    assert run(hist, minute, now), "gate rejects a normal session"
