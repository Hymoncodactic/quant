"""Same-close execution mode (EngineConfig.fill_timing == "same_close").

Pins the user-ruled convention of 2026-08-22: a close-price strategy places
its market order in the last minute of the session, so the fill is modeled
at the decision bar's close plus the calibrated close-proximity gap, and
spills to the next open only when the latency draw exceeds the close window
or the market is closed on that key.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backtest.engine.engine import BacktestEngine
from backtest.engine.feed import BarFeed, FxSeries
from backtest.engine.results import run_name
from backtest.engine.types import Bar, EngineConfig, OrderSpec, OrderStatus
from backtest.t212.broker_sim import T212BrokerSim
from backtest.t212.costs import CostConfig, price_to_gbp
from backtest.t212.instruments import exchange_tz
from tests.backtest.conftest import (bar_frame, daily_ts, faults_off,
                                     fx_frame, mk_ledger)

D = Decimal
GAP = D("11")


def _cost() -> CostConfig:
    return CostConfig(slippage_bps=D("0"), spread_session_multiplier=D("1"),
                      cooldown_bars=1, close_gap_bps=GAP, close_window_sec=60)


def _broker(faults=None) -> T212BrokerSim:
    dates = [f"2026-01-{d:02d}" for d in range(1, 30)]
    fx = FxSeries(fx_frame(dates, [1.25] * len(dates)), 86400)
    return T212BrokerSim(_cost(), faults or faults_off(), "1d", fx, daily=True,
                         fill_timing="same_close")


def _bars(date: str, o: float, c: float, symbol: str = "TEST") -> dict:
    ts = daily_ts(date, "America/New_York")
    return {symbol: Bar(ts=ts, open=o, high=max(o, c), low=min(o, c), close=c,
                        volume=1e9, quote_ccy="GBP")}


# ---------------------------------------------------------------------------
# Core: the fill lands on the decision bar at close x (1 + gap), not at the
# next open. Open != close makes the price discriminative.
# ---------------------------------------------------------------------------

def test_same_close_fills_at_decision_bar_close(zero_spread):
    broker, ledger = _broker(), mk_ledger()
    key = pd.Timestamp("2026-01-05")
    broker.process_bar(0, key, _bars("2026-01-05", 100.0, 110.0), ledger)
    order = broker.submit(OrderSpec("TEST", D("1")), key, 0, ledger)
    fills = broker.drain_submit_fills()
    assert len(fills) == 1 and fills[0].at_close
    assert fills[0].step == order.submitted_step == 0
    assert fills[0].price == D("110.0") * (1 + GAP / D("10000"))
    assert order.status is OrderStatus.FILLED
    assert broker.drain_submit_fills() == []          # drained once


# ---------------------------------------------------------------------------
# Latency beyond the close window: no close fill, next-open path instead.
# ---------------------------------------------------------------------------

def test_latency_beyond_window_spills_to_next_open(zero_spread):
    faults = faults_off(latency_normal_sec=(120.0, 120.0))
    faults.switches["F1_latency_normal"] = True
    broker, ledger = _broker(faults), mk_ledger()
    key = pd.Timestamp("2026-01-05")
    broker.process_bar(0, key, _bars("2026-01-05", 100.0, 110.0), ledger)
    broker.submit(OrderSpec("TEST", D("1")), key, 0, ledger)
    assert broker.drain_submit_fills() == []
    fills = broker.process_bar(1, pd.Timestamp("2026-01-06"),
                               _bars("2026-01-06", 120.0, 121.0), ledger)
    assert len(fills) == 1 and not fills[0].at_close
    assert fills[0].price == D("120.0")                # next open, no gap


# ---------------------------------------------------------------------------
# Market closed on the decision key (symbol had no bar): queue to next bar.
# ---------------------------------------------------------------------------

def test_market_closed_on_key_queues(zero_spread):
    broker, ledger = _broker(), mk_ledger()
    broker.process_bar(0, pd.Timestamp("2026-01-05"),
                       _bars("2026-01-05", 100.0, 100.0), ledger)
    broker.process_bar(1, pd.Timestamp("2026-01-06"), {}, ledger)  # closed
    broker.submit(OrderSpec("TEST", D("1")), pd.Timestamp("2026-01-06"), 1,
                  ledger)
    assert broker.drain_submit_fills() == []
    fills = broker.process_bar(2, pd.Timestamp("2026-01-07"),
                               _bars("2026-01-07", 103.0, 104.0), ledger)
    assert len(fills) == 1 and fills[0].price == D("103.0")


# ---------------------------------------------------------------------------
# Engine integration: buy-and-hold fills on bar 0 at its close; the guard
# accepts at_close fills only under the declared mode.
# ---------------------------------------------------------------------------

def _zigzag(days: int):
    rows, price = [], 100.0
    for i, day in enumerate(pd.bdate_range("2026-01-05", periods=days)):
        o = price
        c = o + (5.0 if i % 2 == 0 else -5.0)
        rows.append((daily_ts(str(day.date()), "America/New_York"),
                     o, max(o, c), min(o, c), c, 1e9))
        price = c
    return bar_frame(rows, "GBP")


def _engine(fill_timing: str, broker_timing: str):
    frames = {"TEST": _zigzag(10)}
    dates = [str(d.date()) for d in pd.bdate_range("2025-12-20", "2026-02-01")]
    fx = FxSeries(fx_frame(dates, [1.25] * len(dates)), 86400)
    broker = T212BrokerSim(_cost(), faults_off(), "1d", fx, daily=True,
                           fill_timing=broker_timing)
    config = EngineConfig(symbols=["TEST"], interval="1d", start="2026-01-05",
                          end="2026-01-16", initial_cash_gbp=D("10000"),
                          arm="unit", seed=1, fill_timing=fill_timing)
    return BacktestEngine(config, BarFeed(frames, exchange_tz, True), fx,
                          broker, lambda v, p, s: {"TEST": D("10")},
                          price_to_gbp), config


def test_engine_same_close_buy_and_hold(zero_spread):
    engine, config = _engine("same_close", "same_close")
    result = engine.run()
    assert len(result.trades) == 1
    row = result.trades.iloc[0]
    assert bool(row["at_close"]) and row["step"] == 0
    assert row["price"] == pytest.approx(105.0 * (1 + 11 / 10000))
    assert "fill-same_close" in run_name(config)


def test_guard_rejects_at_close_fill_outside_mode(zero_spread):
    engine, _ = _engine("next_open", "same_close")
    with pytest.raises(AssertionError, match="at-close fill outside"):
        engine.run()
