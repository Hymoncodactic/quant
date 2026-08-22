"""Unit tests for the daily cycle's diff and an end-to-end strategy check
proving the live view/portfolio duck types satisfy the A0 module."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd

from trading212.execution.daily_cycle import (_diff_to_intents,
                                              _positions_ref_notional)
from trading212.execution.market_data import build_view
from trading212.execution.shadow_ledger import LedgerPortfolioView
from trading212.strategy import get_strategy

D = Decimal
DAY = pd.Timestamp("2026-08-20")


def _frame(n, start="2016-01-04", close=None, ccy="USD"):
    ts = pd.bdate_range(start, periods=n, tz="UTC") + pd.Timedelta(hours=4)
    closes = np.linspace(50, 100, n) if close is None else np.full(n, close)
    return pd.DataFrame({"ts": ts, "open": closes, "high": closes * 1.01,
                         "low": closes * 0.99, "close": closes,
                         "volume": 1e6, "quote_ccy": ccy})


class LedgerStub:
    def __init__(self, positions=None, pending=None):
        self.positions = positions or {}
        self._pending = pending or {}

    def pending_signed_qty(self, symbol):
        return self._pending.get(symbol, D("0"))


def _cycle_stub():
    return SimpleNamespace(strategy_id="a0_v0_0_1",
                           params={"fx_symbol": "GBPUSD=X"})


def _view_for_diff():
    frames = {"NVDA": _frame(300), "GBPUSD=X": _frame(300, ccy="USD", close=1.25)}
    return build_view(frames, pd.Timestamp("2100-01-01"))


def test_diff_floors_delta_to_the_venue_step():
    view = _view_for_diff()
    ledger = LedgerStub(positions={"NVDA": D("1")})
    intents = _diff_to_intents(_cycle_stub(), {"NVDA": D("2.00006")}, ledger,
                               view, DAY)
    # Delta 1.00006 floors to 1.0000 at the 4-dp step (remainder sample).
    assert len(intents) == 1
    assert intents[0].quantity == D("1.0000")


def test_diff_counts_pending_and_skips_dust():
    view = _view_for_diff()
    ledger = LedgerStub(positions={"NVDA": D("1")},
                        pending={"NVDA": D("1")})
    intents = _diff_to_intents(_cycle_stub(), {"NVDA": D("2.00004")}, ledger,
                               view, DAY)
    # held + pending = 2; residual 0.00004 is sub-step dust: no order.
    assert intents == []


def test_diff_produces_signed_sell():
    view = _view_for_diff()
    ledger = LedgerStub(positions={"NVDA": D("3")})
    intents = _diff_to_intents(_cycle_stub(), {"NVDA": D("0")}, ledger,
                               view, DAY)
    assert intents[0].quantity == D("-3")


def test_positions_ref_notional_converts_through_fx():
    view = _view_for_diff()
    ledger = LedgerStub(positions={"NVDA": D("2")})
    total = _positions_ref_notional(ledger, view,
                                    {"fx_symbol": "GBPUSD=X"})
    assert total == D("2") * D("100") / D("1.25")


# ----------------------------------------------------------------------
# End-to-end: the real A0 module against the live duck types.
# ----------------------------------------------------------------------

def _a0_params(symbols):
    return {"trade_symbols": symbols, "state_symbol": "QQQ",
            "fx_symbol": "GBPUSD=X", "signal_mode": "tsmom252",
            "tsmom_lookback": 252, "trend_ma": 200, "vol_window": 20,
            "vol_pct_threshold": 0.80, "vol_min_history": 100000,
            "use_vol_gate": True, "use_trend_gate": True, "warmup_bars": 260,
            "live_from": "2018-01-01", "slot_headroom": 0.99}


def test_a0_runs_on_live_view_and_buys_uptrends():
    n = 400
    frames = {"NVDA": _frame(n), "AAPL": _frame(n),
              "QQQ": _frame(n), "GBPUSD=X": _frame(n, close=1.25)}
    view = build_view(frames, pd.Timestamp("2100-01-01"))
    portfolio = LedgerPortfolioView(cash_gbp=D("1000"),
                                    available_cash_gbp=D("1000"),
                                    positions={}, pending_signed_qty={})
    compute = get_strategy("a0", "0.0.1")
    targets = compute(view, portfolio, _a0_params(["NVDA", "AAPL"]))
    # Rising series, trend gate open, vol gate held open by the min-history
    # floor: both symbols get positive equal-slot targets.
    assert set(targets) == {"NVDA", "AAPL"}
    assert all(q > 0 for q in targets.values())


def test_a0_goes_flat_when_the_trend_gate_trips():
    n = 400
    falling = _frame(n)
    falling["close"] = np.linspace(100, 40, n)   # QQQ below its SMA200
    falling["open"] = falling["close"]
    falling["high"] = falling["close"] * 1.01
    falling["low"] = falling["close"] * 0.99
    frames = {"NVDA": _frame(n), "AAPL": _frame(n),
              "QQQ": falling, "GBPUSD=X": _frame(n, close=1.25)}
    view = build_view(frames, pd.Timestamp("2100-01-01"))
    portfolio = LedgerPortfolioView(cash_gbp=D("1000"),
                                    available_cash_gbp=D("1000"),
                                    positions={"NVDA": D("2")},
                                    pending_signed_qty={})
    compute = get_strategy("a0", "0.0.1")
    targets = compute(view, portfolio, _a0_params(["NVDA", "AAPL"]))
    # Gate closed: every active symbol is forced to zero, held or not.
    assert targets["NVDA"] == D("0")
    assert targets["AAPL"] == D("0")
