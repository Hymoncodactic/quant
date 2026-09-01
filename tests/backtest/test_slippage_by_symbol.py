"""Discriminating tests for CostConfig.slippage_bps_by_symbol.

Each test names the defect it catches (backtest-discipline section 6): an
implementation that ignores the per-symbol table, or one that breaks the
flat fallback, fails here.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from backtest.engine.types import Bar, OrderSpec
from backtest.t212.costs import CostConfig
from tests.backtest.conftest import faults_off, mk_broker, mk_ledger

D = Decimal
TZ_NY = "America/New_York"


def _bars(symbol: str, date: str, price: float) -> dict:
    return {symbol: Bar(ts=pd.Timestamp(date, tz=TZ_NY).tz_convert("UTC"),
                        open=price, high=price, low=price, close=price,
                        volume=1e9, quote_ccy="GBP")}


def _cfg(**over) -> CostConfig:
    base = dict(slippage_bps=D("0"), spread_session_multiplier=D("1"),
                cooldown_bars=1)
    base.update(over)
    return CostConfig(**base)


def _fill_price(cost: CostConfig, symbol: str) -> Decimal:
    """One market buy filled at the next bar's open of 100, spread zeroed
    by monkeypatching being unavailable here, so half spread (1bp) rides
    along identically in every scenario and cancels in comparisons."""
    broker, ledger = mk_broker(cost=cost, faults=faults_off()), mk_ledger()
    broker.process_bar(0, pd.Timestamp("2026-01-05"),
                       _bars(symbol, "2026-01-05", 100.0), ledger)
    broker.submit(OrderSpec(symbol, D("1")), pd.Timestamp("2026-01-05"), 0,
                  ledger)
    fills = broker.process_bar(1, pd.Timestamp("2026-01-06"),
                               _bars(symbol, "2026-01-06", 100.0), ledger)
    assert len(fills) == 1
    return fills[0].price


def test_override_is_applied_to_the_named_symbol():
    """DEFECT CAUGHT: the fill path keeps reading the flat slippage_bps and
    the measured table changes nothing."""
    flat = _fill_price(_cfg(), "AAA")
    with_table = _fill_price(
        _cfg(slippage_bps_by_symbol={"AAA": D("10")}), "AAA")
    # 10bp extra on a 100 buy = +0.10 exactly (half spread identical in both).
    assert with_table - flat == D("100") * D("10") / D("10000")


def test_absent_symbol_falls_back_to_flat_value():
    """DEFECT CAUGHT: a non-empty table changes symbols it does not name."""
    flat = _fill_price(_cfg(slippage_bps=D("7")), "BBB")
    with_table = _fill_price(
        _cfg(slippage_bps=D("7"),
             slippage_bps_by_symbol={"OTHER": D("50")}), "BBB")
    assert with_table == flat


def test_empty_table_reproduces_flat_behavior_exactly():
    """DEFECT CAUGHT: adding the field itself shifts prices even when unset
    (the byte-identical default contract)."""
    assert _fill_price(_cfg(slippage_bps=D("5")), "CCC") == \
        _fill_price(_cfg(slippage_bps=D("5"),
                         slippage_bps_by_symbol={}), "CCC")
