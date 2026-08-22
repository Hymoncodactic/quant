"""Discriminative tests for the tighten-only risk gate."""

from __future__ import annotations

from decimal import Decimal

from trading212.execution.risk_gate import (OrderIntent, check_intents,
                                            T212_QTY_STEP)
from trading212.execution.shadow_ledger import LedgerPortfolioView

D = Decimal

RISK = {"max_order_notional_gbp": 500, "max_gross_notional_gbp": 1000,
        "max_daily_orders": 10, "min_order_value_gbp": 1,
        "fee_buffer": 0.005}


def _view(cash="1000", positions=None, pending=None) -> LedgerPortfolioView:
    return LedgerPortfolioView(cash_gbp=D(cash), available_cash_gbp=D(cash),
                               positions=positions or {},
                               pending_signed_qty=pending or {})


def _intent(symbol="NVDA", qty="1", price="175", fx="1.35",
            intent_id="i1") -> OrderIntent:
    return OrderIntent(intent_id=intent_id, symbol=symbol,
                       ticker=f"{symbol}_US_EQ", quantity=D(qty),
                       ref_price_usd=D(price), fx_usd_per_gbp=D(fx))


def test_zero_limits_fail_closed(tmp_path):
    # An unset risk file must reject everything, not mean "unlimited".
    report = check_intents([_intent()], _view(), D("0"),
                           {k: 0 for k in RISK}, orders_today=0,
                           market_open=False, halt_path=tmp_path / "halt")
    assert report.closed
    assert report.approved == []
    assert "fails closed" in report.rejected[0][1]


def test_halt_flag_closes_the_gate(tmp_path):
    halt = tmp_path / "halt"
    halt.touch()
    report = check_intents([_intent()], _view(), D("0"), RISK,
                           orders_today=0, market_open=False, halt_path=halt)
    assert report.closed
    assert report.approved == []
    assert "halt" in report.rejected[0][1]


def test_open_market_rejected_by_default(tmp_path):
    report = check_intents([_intent()], _view(), D("0"), RISK,
                           orders_today=0, market_open=True,
                           halt_path=tmp_path / "halt")
    assert report.closed
    assert report.approved == []
    assert "session is open" in report.rejected[0][1]


def test_buy_beyond_available_cash_rejected(tmp_path):
    # 3 shares at 175 USD / 1.35 = 388.89 GBP + buffer > 200 GBP cash.
    report = check_intents([_intent(qty="3")], _view(cash="200"), D("0"), RISK,
                           orders_today=0, market_open=False,
                           halt_path=tmp_path / "halt")
    assert report.approved == []
    assert "exceeds available cash" in report.rejected[0][1]


def test_sell_trimmed_to_held_never_enlarged(tmp_path):
    view = _view(positions={"NVDA": D("1.5")})
    report = check_intents([_intent(qty="-2")], view, D("0"), RISK,
                           orders_today=0, market_open=False,
                           halt_path=tmp_path / "halt")
    assert len(report.approved) == 1
    assert report.approved[0].quantity == D("-1.5")


def test_sell_without_position_rejected(tmp_path):
    report = check_intents([_intent(qty="-1")], _view(), D("0"), RISK,
                           orders_today=0, market_open=False,
                           halt_path=tmp_path / "halt")
    assert report.approved == []
    assert "never short" in report.rejected[0][1]


def test_quantity_floored_to_step_with_remainder(tmp_path):
    # 0.12347 has a remainder against the 0.0001 step; flooring must produce
    # 0.1234, not 0.1235 (a discriminative sample per verified-dev 4.3).
    view = _view(positions={"NVDA": D("1")})
    report = check_intents([_intent(qty="0.12347")], view, D("0"), RISK,
                           orders_today=0, market_open=False,
                           halt_path=tmp_path / "halt")
    assert report.approved[0].quantity == D("0.1234")
    assert T212_QTY_STEP == D("0.0001")


def test_dust_below_min_value_rejected(tmp_path):
    report = check_intents([_intent(qty="0.0001")], _view(), D("0"), RISK,
                           orders_today=0, market_open=False,
                           halt_path=tmp_path / "halt")
    assert report.approved == []
    assert "below" in report.rejected[0][1]


def test_daily_order_cap(tmp_path):
    intents = [_intent(intent_id=f"i{k}", symbol=s)
               for k, s in enumerate(["NVDA", "AAPL", "MSFT"])]
    report = check_intents(intents, _view(cash="10000"), D("0"),
                           {**RISK, "max_daily_orders": 2, "max_gross_notional_gbp": 99999,
                            "max_order_notional_gbp": 99999},
                           orders_today=1, market_open=False,
                           halt_path=tmp_path / "halt")
    assert len(report.approved) == 1
    assert any("max_daily_orders" in reason for _, reason in report.rejected)


def test_gross_cap_counts_held_positions(tmp_path):
    # Held notional 900 + new buy 129.63 > 1000 cap -> rejected.
    report = check_intents([_intent(qty="1")], _view(cash="10000"), D("900"),
                           RISK, orders_today=0, market_open=False,
                           halt_path=tmp_path / "halt")
    assert report.approved == []
    assert "gross" in report.rejected[0][1]
