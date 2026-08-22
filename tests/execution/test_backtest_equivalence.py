"""Live and backtest data paths must hand the strategy the same decision.

Responsibility: prove on real stored data that, at one and the same 15:30
decision key, the live execution layer's view and the backtest engine's view
drive trading212/strategy/a0_intraday_v0_0_1.py to byte-identical targets.
This is the test that would fail first if the live data assembly, the cutoff
rule or the FX lag ever drifted from the engine.

Out of scope: order placement and accounting, covered by the other modules
in tests/execution/; engine internals, covered by tests/backtest/.

Public functions: None. Pytest collects the test functions directly.

Constants:
    DECISION_DATE  str  Exchange-local date of the session under test. Chosen
                        as a recent full session present in the curated store.

Inputs:
    data/t212/curated/<group>/<symbol>/{1h,1d}/*.parquet
Outputs:
    None.

Change log:
    2026-08-22  Created with the hourly live cycle.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from trading212.execution import market_data
from trading212.execution.shadow_ledger import LedgerPortfolioView
from trading212.execution.strategy_loader import load_intraday_strategy

DECISION_DATE = "2026-08-20"
HISTORY_START = "2010-01-04"
NY = "America/New_York"

pytest.importorskip("pyarrow")


def _params(symbols):
    return {"trade_symbols": symbols, "state_symbol": "QQQ",
            "fx_symbol": "GBPUSD=X", "signal_mode": "tsmom252",
            "tsmom_lookback": 252, "trend_ma": 200, "vol_window": 20,
            "vol_pct_threshold": 0.80, "vol_min_history": 756,
            "use_vol_gate": True, "use_trend_gate": True, "warmup_bars": 260,
            "live_from": "2018-01-01", "slot_headroom": 0.99,
            "decision_time_local": "15:30", "exchange_tz": NY,
            "bars_per_session": 7}


def _decision_key(date_str: str) -> pd.Timestamp:
    return (pd.Timestamp(date_str, tz=NY) + pd.Timedelta(hours=15, minutes=30)) \
        .tz_convert("UTC")


def _portfolio():
    return LedgerPortfolioView(cash_gbp=Decimal("1000"),
                               available_cash_gbp=Decimal("1000"),
                               positions={}, pending_signed_qty={})


def _have_data(symbols) -> bool:
    try:
        market_data.load_frames(symbols, "1h", DECISION_DATE, DECISION_DATE)
        return True
    except FileNotFoundError:
        return False


def test_live_view_matches_engine_view_targets():
    """Same key, same strategy, two independent data paths, same targets."""
    symbols = ["AAPL", "NVDA", "MSFT", "AMD"]
    state, fx = "QQQ", "GBPUSD=X"
    feed_symbols = symbols + [state, fx]
    if not _have_data(feed_symbols):
        pytest.skip("curated 1h store not present")

    key = _decision_key(DECISION_DATE)
    params = _params(symbols)

    # --- live path -----------------------------------------------------
    start = (pd.Timestamp(DECISION_DATE) - pd.Timedelta(days=40)).date().isoformat()
    live_frames = market_data.load_frames(feed_symbols, "1h", start, DECISION_DATE)
    market_data.assert_intraday_ready(live_frames, key, symbols, state, fx)
    live_view = market_data.build_view(live_frames, key)
    history = market_data.daily_rows(symbols + [state], HISTORY_START,
                                     DECISION_DATE)
    live_strategy = load_intraday_strategy("a0_intraday", "0.0.1", history)
    live_targets = live_strategy(live_view, _portfolio(), params)

    # --- backtest path -------------------------------------------------
    from backtest.engine.feed import BarFeed, MarketView
    from backtest.t212.data_source import load_bars
    from backtest.t212.instruments import exchange_tz

    bt_frames = load_bars(feed_symbols, "1h", start, DECISION_DATE)
    feed = BarFeed(bt_frames, exchange_tz, daily=False)
    bt_view = None
    for _step, feed_key, _bars in feed:
        if feed_key == key:
            bt_view = MarketView(feed.history, feed_key, None)
            break
    assert bt_view is not None, f"engine feed never produced key {key}"
    bt_strategy = load_intraday_strategy("a0_intraday", "0.0.1", history)
    bt_targets = bt_strategy(bt_view, _portfolio(), params)

    assert live_targets == bt_targets
    assert live_targets, "the session under test produced no targets at all"


def test_freshness_gate_rejects_a_missing_fx_bar():
    """Dropping the FX bar the decision needs must stop the decision."""
    symbols = ["AAPL"]
    state, fx = "QQQ", "GBPUSD=X"
    feed_symbols = symbols + [state, fx]
    if not _have_data(feed_symbols):
        pytest.skip("curated 1h store not present")

    key = _decision_key(DECISION_DATE)
    start = (pd.Timestamp(DECISION_DATE) - pd.Timedelta(days=10)).date().isoformat()
    frames = market_data.load_frames(feed_symbols, "1h", start, DECISION_DATE)
    market_data.assert_intraday_ready(frames, key, symbols, state, fx)

    holed = dict(frames)
    fx_key = key - pd.Timedelta(minutes=market_data.FX_LAG_MINUTES)
    holed[fx] = frames[fx][frames[fx]["ts"] != fx_key].reset_index(drop=True)
    with pytest.raises(RuntimeError, match="GBPUSD"):
        market_data.assert_intraday_ready(holed, key, symbols, state, fx)


def test_view_excludes_bars_after_the_decision_key():
    """The cutoff must hide later bars, including any in-progress one."""
    symbols = ["AAPL"]
    if not _have_data(symbols):
        pytest.skip("curated 1h store not present")
    key = _decision_key(DECISION_DATE)
    start = (pd.Timestamp(DECISION_DATE) - pd.Timedelta(days=10)).date().isoformat()
    frames = market_data.load_frames(symbols, "1h", start, "2026-08-21")
    view = market_data.build_view(frames, key)
    bars = view.bars("AAPL", 5000)
    assert bars[-1].ts == key
    assert all(b.ts <= key for b in bars)
    # Discriminative: the store really does hold later bars that were hidden.
    assert (frames["AAPL"]["ts"] > key).any()
