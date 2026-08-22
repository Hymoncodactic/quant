"""Second-pass conservatism fixes and the expanded metrics and chart outputs.

Responsibility: pin the findings of the 2026-08-21 conservatism review so that
a later change cannot silently undo them. Finding 2 is the limit rule: a bare
touch of the limit price no longer fills and strict penetration does, with the
equality case as the discriminator. Finding 3, which is also item 7 of the
conservative hard list, is the cooldown between fills of different orders,
together with the exemption that lets one order's partial fill roll into the
next bar. Finding 5 is the stale-position guard, which aborts the run when a
held symbol's feed stops. Finding 1 is liquidation-valued equity, where a held
USD position marks below its mid value because exiting it would pay the FX fee
and the sell-side fees. The remaining tests cover holding-duration statistics,
where mean and median must be reported separately and an episode still open at
the end of the window must be flagged, and the run chart, which must be
written with both equity traces and the in-market lane.

Out of scope: the first-pass broker behaviors, covered by
tests/backtest/test_broker.py; the 2026-08-20 adversarial review, covered by
tests/backtest/test_review_regressions.py; ledger arithmetic and the
capital-occupancy definition, covered by tests/backtest/test_ledger_metrics.py.

Public functions:
    test_limit_touch_does_not_fill_penetration_does()
        A buy limit fills only when the bar's low goes strictly through it; a
        sell limit touched exactly at its price does not fill.
    test_cooldown_spaces_fills_of_different_orders(zero_spread, cooldown,
                                                   fill_day)
        Parametrized over cooldown_bars 1 and 2: the second order's fill day
        moves out by one bar and the total fill count stays at one.
    test_cooldown_exempts_same_order_rollover(zero_spread)
        A single order's volume-capped remainder keeps filling on consecutive
        bars despite a cooldown of two bars.
    test_stale_held_position_aborts(zero_spread)
        A held symbol whose frame ends early raises RuntimeError rather than
        marking a stale price forever.
    test_liquidation_column_below_mid_for_usd_position(zero_spread)
        On every bar with a position the liquidation equity column is below
        the mid column, and the metrics carry the liquidation counterparts.
    test_holding_duration_mean_vs_median()
        On a skewed episode set of one, one and seven days the mean is 3.0 and
        the median 1.0, so reporting one number for both cannot pass.
    test_censored_episode_flagged()
        An episode still open at the end of the window is flagged and measured
        to the window end.
    test_chart_written_with_traces(zero_spread, tmp_path)
        The chart file is written and contains both equity traces, the symbol
        and the chart element identifier.

Public classes: None.

Constants:
    D
        Alias of decimal.Decimal, used so quantities and prices in the tests
        carry the same type as the production path.

Inputs: None under data/. Frames, trades and equity curves are synthesized in
process.
Outputs: One chart HTML file per run of test_chart_written_with_traces,
written under pytest's tmp_path and reclaimed by pytest; nothing lands in the
project directory.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backtest.engine.engine import BacktestEngine
from backtest.engine.feed import BarFeed, FxSeries
from backtest.engine.matching import match_limit
from backtest.engine.metrics import compute_metrics, holding_episodes
from backtest.engine.report import in_market_spans, write_chart
from backtest.engine.types import (Bar, EngineConfig, OrderSpec, OrderStatus,
                                   OrderType, TimeInForce)
from backtest.t212.costs import CostConfig, price_to_gbp
from backtest.t212.instruments import exchange_tz
from backtest.t212.runner import liquidation_valuer
from tests.backtest.conftest import (bar_frame, cost_cfg_clean, daily_ts,
                                     faults_off, fx_frame, mk_broker,
                                     mk_ledger)

D = Decimal


def _bar(ts, o, h, l, c, v=1e9, ccy="GBP") -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v, quote_ccy=ccy)


# ---------------------------------------------------------------------------
# Finding 2: a bare touch of the limit no longer fills; strict penetration
# does. The equality case is the discriminator.
# ---------------------------------------------------------------------------

def test_limit_touch_does_not_fill_penetration_does():
    touch = _bar(pd.Timestamp("2026-01-06", tz="UTC"), 100.5, 100.6, 100.0,
                 100.4)
    poke = _bar(pd.Timestamp("2026-01-07", tz="UTC"), 100.5, 100.6, 99.99,
                100.4)
    assert match_limit(True, D("100.0"), touch) is None
    assert match_limit(True, D("100.0"), poke) == D("100.0")
    sell_touch = _bar(pd.Timestamp("2026-01-08", tz="UTC"), 99.5, 100.0, 99.0,
                      99.6)
    assert match_limit(False, D("100.0"), sell_touch) is None


# ---------------------------------------------------------------------------
# Finding 3 / hard-list item 7: cooldown between fills of DIFFERENT orders.
# cooldown_bars=2 defers the second order's fill by one extra bar relative to
# cooldown_bars=1; same-order partial-fill rollover is exempt.
# ---------------------------------------------------------------------------

def _flat_bars(symbol, date, px, v=1e9):
    ts = daily_ts(date, "America/New_York")
    return {symbol: _bar(ts, px, px, px, px, v)}


@pytest.mark.parametrize("cooldown, fill_day", [(1, "2026-01-07"),
                                                (2, "2026-01-08")])
def test_cooldown_spaces_fills_of_different_orders(zero_spread, cooldown,
                                                   fill_day):
    cost = CostConfig(slippage_bps=D("0"), spread_session_multiplier=D("1"),
                      cooldown_bars=cooldown)
    broker, ledger = mk_broker(cost=cost), mk_ledger()
    broker.process_bar(0, pd.Timestamp("2026-01-05"),
                       _flat_bars("TEST", "2026-01-05", 100.0), ledger)
    broker.submit(OrderSpec("TEST", D("1")), pd.Timestamp("2026-01-05"), 0,
                  ledger)
    fills1 = broker.process_bar(1, pd.Timestamp("2026-01-06"),
                                _flat_bars("TEST", "2026-01-06", 100.0),
                                ledger)
    assert len(fills1) == 1                     # first order fills day 06
    broker.submit(OrderSpec("TEST", D("1")), pd.Timestamp("2026-01-06"), 1,
                  ledger)
    fills_by_day = {}
    for i, day in enumerate(["2026-01-07", "2026-01-08"], 2):
        fills = broker.process_bar(i, pd.Timestamp(day),
                                   _flat_bars("TEST", day, 100.0), ledger)
        fills_by_day[day] = len(fills)
    assert fills_by_day[fill_day] == 1
    assert sum(fills_by_day.values()) == 1


def test_cooldown_exempts_same_order_rollover(zero_spread):
    cost = CostConfig(slippage_bps=D("0"), spread_session_multiplier=D("1"),
                      cooldown_bars=2)
    faults = faults_off()
    faults.switches["F13_partial_fill"] = True
    broker, ledger = mk_broker(cost=cost, faults=faults), mk_ledger()
    broker.process_bar(0, pd.Timestamp("2026-01-05"),
                       _flat_bars("TEST", "2026-01-05", 100.0), ledger)
    order = broker.submit(OrderSpec("TEST", D("20")),
                          pd.Timestamp("2026-01-05"), 0, ledger)
    for i, day in enumerate(["2026-01-06", "2026-01-07"], 1):
        fills = broker.process_bar(i, pd.Timestamp(day),
                                   _flat_bars("TEST", day, 100.0, v=100.0),
                                   ledger)
        assert len(fills) == 1                  # 10 shares each bar, rolling
    assert order.status is OrderStatus.FILLED


# ---------------------------------------------------------------------------
# Finding 5: a held symbol whose feed dies aborts the run.
# ---------------------------------------------------------------------------

def _engine(config, strategy, frames):
    dates = [str(d.date()) for d in pd.bdate_range("2025-12-20", "2026-03-01")]
    fx = FxSeries(fx_frame(dates, [1.25] * len(dates)), 86400)
    from backtest.t212.broker_sim import T212BrokerSim
    broker = T212BrokerSim(cost_cfg_clean(), faults_off(), "1d", fx,
                           daily=True)
    return BacktestEngine(config, BarFeed(frames, exchange_tz, True), fx,
                          broker, strategy, price_to_gbp,
                          to_liquidation=liquidation_valuer(broker))


def _daily_frame(symbol_dates, tz, px=100.0):
    return bar_frame([(daily_ts(d, tz), px, px, px, px, 1e9)
                      for d in symbol_dates], "GBP")


def test_stale_held_position_aborts(zero_spread):
    days = [str(d.date()) for d in pd.bdate_range("2026-01-05", periods=15)]
    frames = {"TEST": _daily_frame(days[:3], "America/New_York"),
              "USDX": _daily_frame(days, "America/New_York")}

    def buy_dying_symbol(view, portfolio, params):
        return {"TEST": D("1")}

    config = EngineConfig(symbols=["TEST", "USDX"], interval="1d",
                          start=days[0], end=days[-1],
                          initial_cash_gbp=D("10000"), arm="unit", seed=1)
    with pytest.raises(RuntimeError, match="feed died"):
        _engine(config, buy_dying_symbol, frames).run()


# ---------------------------------------------------------------------------
# Finding 1: liquidation-valued equity. A held USD position marks lower on
# the liquidation column than at mid (FX fee + sell fees), and the metrics
# carry the liquidation counterparts.
# ---------------------------------------------------------------------------

def test_liquidation_column_below_mid_for_usd_position(zero_spread):
    days = [str(d.date()) for d in pd.bdate_range("2026-01-05", periods=6)]
    frames = {"USDX": bar_frame([(daily_ts(d, "America/New_York"),
                                  100, 100, 100, 100, 1e9)
                                 for d in days], "USD")}
    config = EngineConfig(symbols=["USDX"], interval="1d", start=days[0],
                          end=days[-1], initial_cash_gbp=D("10000"),
                          arm="unit", seed=1)
    result = _engine(config, lambda v, p, s: {"USDX": D("10")}, frames).run()
    held = result.equity[result.equity["invested_cost_gbp"] > 0]
    assert (held["equity_liq_gbp"] < held["equity_gbp"]).all()
    metrics = compute_metrics(result.equity, result.trades, 10000.0, 252)
    assert metrics["exit_costs_at_end_gbp"] > 0
    assert metrics["final_equity_liquidation_gbp"] \
        < metrics["capital_peak_occupied_gbp"] + 10000.0
    assert "max_drawdown_liq_gbp" in metrics


# ---------------------------------------------------------------------------
# Holding-duration statistics: average and median must differ on a skewed
# episode set (1d, 1d, 7d -> mean 3, median 1), censored episodes flagged.
# ---------------------------------------------------------------------------

def _trade(step, symbol, qty, ts, cash):
    return {"step": step, "order_id": step, "symbol": symbol,
            "quantity": qty, "cash_delta_gbp": cash,
            "ts": pd.Timestamp(ts, tz="UTC")}


def test_holding_duration_mean_vs_median():
    trades = pd.DataFrame([
        _trade(1, "A", 1.0, "2026-01-05", -10.0),
        _trade(2, "A", -1.0, "2026-01-06", 11.0),
        _trade(3, "B", 1.0, "2026-01-05", -10.0),
        _trade(4, "B", -1.0, "2026-01-06", 9.0),
        _trade(5, "C", 1.0, "2026-01-05", -10.0),
        _trade(6, "C", -1.0, "2026-01-12", 10.0),
    ])
    episodes = holding_episodes(trades, pd.Timestamp("2026-01-20", tz="UTC"))
    assert [e["days"] for e in episodes] == [1.0, 1.0, 7.0]
    equity = pd.DataFrame({
        "step": range(3),
        "ts": pd.date_range("2026-01-05", periods=3, freq="D"),
        "cash_gbp": [0.0] * 3, "reserved_gbp": [0.0] * 3,
        "invested_cost_gbp": [30.0, 30.0, 0.0],
        "occupied_gbp": [30.0, 30.0, 0.0],
        "equity_gbp": [1000.0, 1001.0, 1000.0],
    })
    metrics = compute_metrics(equity, trades, 1000.0, 252)
    assert metrics["avg_holding_days"] == 3.0
    assert metrics["median_holding_days"] == 1.0
    assert metrics["holding_episodes"] == 3
    assert metrics["holding_episodes_open_at_end"] == 0


def test_censored_episode_flagged():
    trades = pd.DataFrame([_trade(1, "A", 1.0, "2026-01-05", -10.0)])
    episodes = holding_episodes(trades, pd.Timestamp("2026-01-08", tz="UTC"))
    assert len(episodes) == 1 and episodes[0]["open_at_end"]
    assert episodes[0]["days"] == 3.0


# ---------------------------------------------------------------------------
# Chart output: file written with both equity traces and the holding lane.
# ---------------------------------------------------------------------------

def test_chart_written_with_traces(zero_spread, tmp_path):
    days = [str(d.date()) for d in pd.bdate_range("2026-01-05", periods=6)]
    frames = {"USDX": bar_frame([(daily_ts(d, "America/New_York"),
                                  100, 100, 100, 100, 1e9)
                                 for d in days], "USD")}
    config = EngineConfig(symbols=["USDX"], interval="1d", start=days[0],
                          end=days[-1], initial_cash_gbp=D("10000"),
                          arm="unit", seed=1)
    result = _engine(config, lambda v, p, s: {"USDX": D("10")}, frames).run()
    spans = in_market_spans(result.equity)
    assert len(spans) == 1
    path = write_chart(result, "unit_chart", tmp_path / "run.chart.html")
    html = path.read_text()
    assert "equity_mid_gbp" in html and "equity_liquidation_gbp" in html
    assert "USDX" in html and "backtest_chart" in html