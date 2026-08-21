"""Engine loop tests: end-to-end synthetic run, determinism, the same-bar-fill
assertion, and the lookahead probe's discriminative power.

Responsibility: pin the four-step order of the engine's main loop and the
guards that sit around it. The end-to-end case checks that one signal produces
one fill at the next bar's open and that equity is marked on every bar.
Determinism, item U16 of fixplans/validation/02_test_plan.md, requires two
runs of the same configuration to produce identical frames. The same-bar-fill
case wraps the broker in a local subclass that reports every fill as if it had
happened on the submission bar, which the engine must reject with a readable
AssertionError rather than accept. The probe cases implement
fixplans/validation/01_no_lookahead.md section 2: on a series constructed so
that each bar opens exactly at the previous close, the only money available
comes from knowing the coming bar's intra-bar direction, so the probe arm must
profit, the blind momentum arm must lose, and with the probe switched off the
view's next_bar must return None. The last case checks that rejected orders
appear in the order audit with their reason. Three of this module's private
helpers are imported by tests/backtest/test_review_regressions.py, so a change
to their signatures reaches beyond this file.

Out of scope: broker-level admission and fill rules, covered by
tests/backtest/test_broker.py; cost arithmetic, covered by
tests/backtest/test_costs.py; feed alignment, covered by
tests/backtest/test_feed.py; the result files written to disk, covered by
tests/backtest/test_review_regressions.py.

Public functions:
    test_buy_and_hold_end_to_end(zero_spread)
        One buy filled at the next bar's open, the position held to the end,
        equity marked on all 30 bars.
    test_determinism(zero_spread)
        Two runs of one configuration produce equal trades, equity and orders
        frames.
    test_same_bar_fill_is_fatal(zero_spread)
        A broker that back-dates its fills to the submission bar trips the
        engine's assertion.
    test_lookahead_probe_discriminates(zero_spread)
        The probe arm profits, the blind arm loses, and the probe arm wins by
        more than nothing, which is what makes the probe itself meaningful.
    test_probe_off_hides_next_bar(zero_spread)
        With the probe off the strategy's view returns None for the next bar.
    test_rejects_visible_in_order_audit(zero_spread)
        An unaffordable target leaves rejected rows carrying the
        insufficient-free-cash reason.

Public classes: None. CheatingBroker is defined inside
test_same_bar_fill_is_fatal because it exists only to violate the fill-timing
contract for that one case.

Constants:
    D
        Alias of decimal.Decimal, used so the tests carry the same numeric
        type as the production path.

Inputs: None. The bar series, the FX series and the strategy are all
synthesized in process; no path under data/ is read.
Outputs: None. No result file is written by this module.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backtest.engine.engine import BacktestEngine
from backtest.engine.feed import BarFeed, FxSeries
from backtest.engine.types import EngineConfig, OrderStatus
from backtest.t212.broker_sim import T212BrokerSim
from backtest.t212.costs import price_to_gbp
from backtest.t212.instruments import exchange_tz
from tests.backtest.conftest import (bar_frame, cost_cfg_clean, daily_ts,
                                     faults_off, fx_frame)

D = Decimal


def _zigzag_frame(days: int) -> pd.DataFrame:
    """Deterministic alternating bars: even bars rise 5 intra-bar, odd bars
    fall 5; the next bar always opens exactly at the previous close, so the
    ONLY money to be made is knowing the coming bar's intra-bar direction."""
    rows, price = [], 100.0
    dates = pd.bdate_range("2026-01-05", periods=days)
    for i, day in enumerate(dates):
        move = 5.0 if i % 2 == 0 else -5.0
        o = price
        c = o + move
        h, l = max(o, c), min(o, c)
        rows.append((daily_ts(str(day.date()), "America/New_York"),
                     o, h, l, c, 1e9))
        price = c
    return bar_frame(rows, "GBP")


def _mk_engine(config: EngineConfig, strategy, frames=None):
    frames = frames or {"TEST": _zigzag_frame(30)}
    dates = [str(d.date()) for d in pd.bdate_range("2025-12-20", "2026-03-01")]
    fx = FxSeries(fx_frame(dates, [1.25] * len(dates)), 86400)
    feed = BarFeed(frames, exchange_tz, daily=True)
    broker = T212BrokerSim(cost_cfg_clean(), faults_off(), "1d", fx, daily=True)
    return BacktestEngine(config, feed, fx, broker, strategy, price_to_gbp)


def _config(**overrides) -> EngineConfig:
    base = dict(symbols=["TEST"], interval="1d", start="2026-01-05",
                end="2026-03-01", initial_cash_gbp=D("10000"),
                arm="unit", seed=1)
    base.update(overrides)
    return EngineConfig(**base)


def _buy_and_hold(view, portfolio, params):
    return {"TEST": D("10")}


# ---------------------------------------------------------------------------
# End-to-end: one buy, held to the end; equity marks every bar.
# ---------------------------------------------------------------------------

def test_buy_and_hold_end_to_end(zero_spread):
    engine = _mk_engine(_config(), _buy_and_hold)
    result = engine.run()
    buys = result.trades
    assert len(buys) == 1
    # Signal on bar 0 (close 105) fills at bar 1 open == 105.
    assert buys["price"].iloc[0] == 105.0
    assert engine.ledger.position_qty("TEST") == D("10")
    assert len(result.equity) == 30
    assert result.equity["equity_gbp"].iloc[0] == 10000.0


# ---------------------------------------------------------------------------
# U16: determinism. Identical configuration twice, identical outputs.
# ---------------------------------------------------------------------------

def test_determinism(zero_spread):
    r1 = _mk_engine(_config(), _buy_and_hold).run()
    r2 = _mk_engine(_config(), _buy_and_hold).run()
    pd.testing.assert_frame_equal(r1.trades, r2.trades)
    pd.testing.assert_frame_equal(r1.equity, r2.equity)
    pd.testing.assert_frame_equal(r1.orders, r2.orders)


# ---------------------------------------------------------------------------
# Same-bar fill assertion: a broker that fills on the submission bar must be
# caught by the engine, not silently accepted.
# ---------------------------------------------------------------------------

def test_same_bar_fill_is_fatal(zero_spread):
    config = _config()

    class CheatingBroker(T212BrokerSim):
        """Reports every fill as if it happened on the submission bar --
        exactly the timestamp lie the engine guard must catch."""

        def process_bar(self, step, key, bars, ledger):
            fills = super().process_bar(step, key, bars, ledger)
            from dataclasses import replace
            return [replace(f, step=self.orders[f.order_id].submitted_step)
                    for f in fills]

    frames = {"TEST": _zigzag_frame(30)}
    dates = [str(d.date()) for d in pd.bdate_range("2025-12-20", "2026-03-01")]
    fx = FxSeries(fx_frame(dates, [1.25] * len(dates)), 86400)
    feed = BarFeed(frames, exchange_tz, daily=True)
    broker = CheatingBroker(cost_cfg_clean(), faults_off(), "1d", fx, daily=True)
    engine = BacktestEngine(config, feed, fx, broker, _buy_and_hold,
                            price_to_gbp)
    with pytest.raises(AssertionError, match="same-bar fill"):
        engine.run()


# ---------------------------------------------------------------------------
# Lookahead probe discrimination. On the zigzag series:
#   - probe arm (sees bar t+1) buys exactly before up-bars: positive PnL;
#   - momentum arm (past data only) buys after up-bars, i.e. always into
#     down-bars: negative PnL.
# A clean engine must produce probe > 0 > momentum; if the probe arm did NOT
# beat the blind arm the probe itself would be broken (no discrimination).
# ---------------------------------------------------------------------------

def _probe_strategy(view, portfolio, params):
    nxt = view.next_bar("TEST")
    if nxt is not None and nxt.close > nxt.open:
        return {"TEST": D("10")}
    return {"TEST": D("0")}


def _momentum_strategy(view, portfolio, params):
    bar = view.bar("TEST")
    if bar is not None and bar.close > bar.open:
        return {"TEST": D("10")}
    return {"TEST": D("0")}


def test_lookahead_probe_discriminates(zero_spread):
    probe = _mk_engine(_config(lookahead_probe=True), _probe_strategy).run()
    blind = _mk_engine(_config(), _momentum_strategy).run()
    probe_pnl = probe.equity["equity_gbp"].iloc[-1] - 10000.0
    blind_pnl = blind.equity["equity_gbp"].iloc[-1] - 10000.0
    assert probe_pnl > 0, "probe arm must profit on a clean engine"
    assert blind_pnl < 0, "blind momentum must lose on the alternating series"
    assert probe_pnl > blind_pnl


def test_probe_off_hides_next_bar(zero_spread):
    captured = {}

    def spy(view, portfolio, params):
        captured["next"] = view.next_bar("TEST")
        return {}

    _mk_engine(_config(), spy).run()
    assert captured["next"] is None


# ---------------------------------------------------------------------------
# Rejected orders surface in the audit frame with their reasons.
# ---------------------------------------------------------------------------

def test_rejects_visible_in_order_audit(zero_spread):
    def oversized(view, portfolio, params):
        return {"TEST": D("1000000")}      # far beyond available cash

    result = _mk_engine(_config(), oversized).run()
    rejected = result.orders[result.orders["status"]
                             == OrderStatus.REJECTED.value]
    assert not rejected.empty
    assert (rejected["reason"] == "insufficient_free_for_stocks_buy").all()
