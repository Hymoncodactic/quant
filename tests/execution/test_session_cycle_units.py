"""Session cycle internals: target diffing and the strategy duck types.

Responsibility: prove the live diff reproduces the engine's rule (floor to
the venue step, count pending, drop dust, preserve order) and that the live
view and portfolio objects satisfy the real A0 modules without adaptation.

Out of scope: the full decide sequence and its gates, which need a venue
client; the real-data equivalence proof, in
tests/execution/test_backtest_equivalence.py.

Public functions: None. Pytest collects the test functions directly.

Constants:
    NY  str  Exchange time zone used to build synthetic hourly sessions.

Inputs: None.
Outputs: None.

Change log:
    2026-08-21  Created for the daily cycle.
    2026-08-22  Rewritten for the hourly arm: sessions instead of days, the
                intraday shim bound with an injected daily history, and an
                order-preservation check because submission order decides
                which buys the venue can fund.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd

from trading212.execution.market_data import build_view
from trading212.execution.session_cycle import (_diff_to_intents,
                                                _positions_ref_notional)
from trading212.execution.shadow_ledger import LedgerPortfolioView
from trading212.execution.strategy_loader import (load_intraday_strategy,
                                                  load_strategy)

D = Decimal
NY = "America/New_York"
KEY = (pd.Timestamp("2026-08-20", tz=NY)
       + pd.Timedelta(hours=15, minutes=30)).tz_convert("UTC")


def _hourly_frame(close=100.0, bars=7, key=KEY):
    ts = pd.DatetimeIndex([key - pd.Timedelta(hours=h)
                           for h in range(bars - 1, -1, -1)])
    return pd.DataFrame({"ts": ts, "open": close, "high": close * 1.01,
                         "low": close * 0.99, "close": close, "volume": 1e6,
                         "quote_ccy": "USD"})


class _LedgerStub:
    def __init__(self, positions=None, pending=None):
        self.positions = positions or {}
        self._pending = pending or {}

    def pending_signed_qty(self, symbol):
        return self._pending.get(symbol, D("0"))


def _cycle_stub():
    return SimpleNamespace(strategy_id="a0_v0_0_1",
                           params={"fx_symbol": "GBPUSD=X"})


def _session():
    return SimpleNamespace(date_ny=pd.Timestamp("2026-08-20").date())


def _view():
    return build_view({"NVDA": _hourly_frame(), "AAPL": _hourly_frame(),
                       "GBPUSD=X": _hourly_frame(close=1.25)}, KEY)


# ----------------------------------------------------------------------
# Diff rule
# ----------------------------------------------------------------------

def test_diff_floors_delta_to_the_venue_step():
    """Delta 1.00006 floors to 1.0000 at the 4 dp step; the remainder dies."""
    intents = _diff_to_intents(_cycle_stub(), {"NVDA": D("2.00006")},
                               _LedgerStub(positions={"NVDA": D("1")}),
                               _view(), _session())
    assert len(intents) == 1
    assert intents[0].quantity == D("1.0000")


def test_diff_counts_pending_and_skips_dust():
    """Held plus pending already meets the target bar sub-step dust."""
    intents = _diff_to_intents(_cycle_stub(), {"NVDA": D("2.00004")},
                               _LedgerStub(positions={"NVDA": D("1")},
                                           pending={"NVDA": D("1")}),
                               _view(), _session())
    assert intents == []


def test_diff_produces_a_signed_sell():
    intents = _diff_to_intents(_cycle_stub(), {"NVDA": D("0")},
                               _LedgerStub(positions={"NVDA": D("3")}),
                               _view(), _session())
    assert intents[0].quantity == D("-3")


def test_diff_preserves_target_order():
    """Submission order decides which buys the venue can still fund, so the
    diff must not reorder the strategy's mapping."""
    targets = {"AAPL": D("1"), "NVDA": D("2")}
    intents = _diff_to_intents(_cycle_stub(), targets, _LedgerStub(),
                               _view(), _session())
    assert [i.symbol for i in intents] == ["AAPL", "NVDA"]


def test_intent_ids_are_deterministic_per_session():
    """The same session recomputed must collide, not create a second order."""
    args = ({"NVDA": D("1")}, _LedgerStub(), _view(), _session())
    first = _diff_to_intents(_cycle_stub(), *args)
    second = _diff_to_intents(_cycle_stub(), *args)
    assert first[0].intent_id == second[0].intent_id
    assert "2026-08-20" in first[0].intent_id


def test_positions_ref_notional_converts_through_fx():
    total = _positions_ref_notional(_LedgerStub(positions={"NVDA": D("2")}),
                                    _view(), {"fx_symbol": "GBPUSD=X"})
    assert total == D("2") * D("100") / D("1.25")


# ----------------------------------------------------------------------
# The real strategy modules against the live duck types
# ----------------------------------------------------------------------

def _daily_frame(n, close=None):
    ts = pd.bdate_range("2016-01-04", periods=n, tz="UTC") + pd.Timedelta(hours=4)
    closes = np.linspace(50, 100, n) if close is None else np.full(n, close)
    return pd.DataFrame({"ts": ts, "open": closes, "high": closes * 1.01,
                         "low": closes * 0.99, "close": closes,
                         "volume": 1e6, "quote_ccy": "USD"})


def _a0_params(symbols):
    return {"trade_symbols": symbols, "state_symbol": "QQQ",
            "fx_symbol": "GBPUSD=X", "signal_mode": "tsmom252",
            "tsmom_lookback": 252, "trend_ma": 200, "vol_window": 20,
            "vol_pct_threshold": 0.80, "vol_min_history": 100000,
            "use_vol_gate": True, "use_trend_gate": True, "warmup_bars": 260,
            "live_from": "2018-01-01", "slot_headroom": 0.99}


def _portfolio(positions=None):
    return LedgerPortfolioView(cash_gbp=D("1000"),
                               available_cash_gbp=D("1000"),
                               positions=positions or {},
                               pending_signed_qty={})


def test_a0_signal_runs_on_the_live_daily_view():
    n = 400
    frames = {"NVDA": _daily_frame(n), "AAPL": _daily_frame(n),
              "QQQ": _daily_frame(n), "GBPUSD=X": _daily_frame(n, close=1.25)}
    view = build_view(frames, pd.Timestamp("2100-01-01", tz="UTC"))
    targets = load_strategy("a0", "0.0.1")(view, _portfolio(),
                                           _a0_params(["NVDA", "AAPL"]))
    assert set(targets) == {"NVDA", "AAPL"}
    assert all(q > 0 for q in targets.values())


def test_a0_goes_flat_when_the_trend_gate_trips():
    n = 400
    falling = _daily_frame(n)
    falling["close"] = np.linspace(100, 40, n)
    falling["open"] = falling["close"]
    falling["high"] = falling["close"] * 1.01
    falling["low"] = falling["close"] * 0.99
    frames = {"NVDA": _daily_frame(n), "AAPL": _daily_frame(n),
              "QQQ": falling, "GBPUSD=X": _daily_frame(n, close=1.25)}
    view = build_view(frames, pd.Timestamp("2100-01-01", tz="UTC"))
    targets = load_strategy("a0", "0.0.1")(
        view, _portfolio({"NVDA": D("2")}), _a0_params(["NVDA", "AAPL"]))
    assert targets["NVDA"] == D("0")
    assert targets["AAPL"] == D("0")


def test_intraday_shim_stays_silent_off_the_decision_minute():
    """The shim's own clock gate must reject a view keyed anywhere else."""
    params = _a0_params(["NVDA"])
    params.update({"decision_time_local": "15:30", "exchange_tz": NY})
    off_key = KEY - pd.Timedelta(hours=1)
    view = build_view({"NVDA": _hourly_frame(key=off_key),
                       "QQQ": _hourly_frame(key=off_key),
                       "GBPUSD=X": _hourly_frame(close=1.25, key=off_key)},
                      off_key)
    strategy = load_intraday_strategy("a0_intraday", "0.0.1", {"NVDA": [], "QQQ": []})
    assert strategy(view, _portfolio(), params) == {}


# ----------------------------------------------------------------------
# The submission deadline
# ----------------------------------------------------------------------

def test_submitting_before_the_instant_waits_then_proceeds(tmp_path):
    """A run that is early simply waits; nothing is refused."""
    from trading212.execution.session_cycle import _wait_for_submit_instant
    now = pd.Timestamp.now(tz="UTC")
    out = _wait_for_submit_instant(now + pd.Timedelta(seconds=1),
                                   now + pd.Timedelta(minutes=1),
                                   grace_sec=30, max_wait_sec=60,
                                   halt_path=tmp_path / "halt")
    assert out is None


def test_slightly_late_submission_is_allowed_inside_the_grace(tmp_path):
    from trading212.execution.session_cycle import _wait_for_submit_instant
    now = pd.Timestamp.now(tz="UTC")
    out = _wait_for_submit_instant(now - pd.Timedelta(seconds=5),
                                   now + pd.Timedelta(minutes=1),
                                   grace_sec=30, max_wait_sec=60,
                                   halt_path=tmp_path / "halt")
    assert out is None


def test_late_beyond_the_grace_refuses_to_submit(tmp_path):
    """Sending late would fill at the next open, which is the timing the
    ruling did not choose; the batch is abandoned instead."""
    from trading212.execution.session_cycle import _wait_for_submit_instant
    now = pd.Timestamp.now(tz="UTC")
    out = _wait_for_submit_instant(now - pd.Timedelta(minutes=5),
                                   now + pd.Timedelta(minutes=1),
                                   grace_sec=30, max_wait_sec=60,
                                   halt_path=tmp_path / "halt")
    assert isinstance(out, str) and "grace" in out


def test_after_the_close_refuses_regardless_of_grace(tmp_path):
    from trading212.execution.session_cycle import _wait_for_submit_instant
    now = pd.Timestamp.now(tz="UTC")
    out = _wait_for_submit_instant(now - pd.Timedelta(seconds=5),
                                   now - pd.Timedelta(seconds=1),
                                   grace_sec=3600, max_wait_sec=60,
                                   halt_path=tmp_path / "halt")
    assert isinstance(out, str) and "closed" in out


def test_halt_raised_while_waiting_stops_the_batch(tmp_path):
    from trading212.execution.session_cycle import _wait_for_submit_instant
    halt = tmp_path / "halt"
    halt.touch()
    now = pd.Timestamp.now(tz="UTC")
    out = _wait_for_submit_instant(now + pd.Timedelta(seconds=30),
                                   now + pd.Timedelta(minutes=5),
                                   grace_sec=30, max_wait_sec=600,
                                   halt_path=halt)
    assert isinstance(out, str) and "halt" in out
