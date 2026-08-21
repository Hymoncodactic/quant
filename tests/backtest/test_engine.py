"""Engine loop tests: end-to-end synthetic run, determinism (U16), the
same-bar-fill assertion, and the lookahead probe's discriminative power
(fixplans/validation/01_no_lookahead.md section 2)."""

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
