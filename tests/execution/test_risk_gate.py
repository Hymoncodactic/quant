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
                           in_submit_window=True, halt_path=tmp_path / "halt")
    assert report.closed
    assert report.approved == []
    assert "fails closed" in report.rejected[0][1]


def test_halt_flag_closes_the_gate(tmp_path):
    halt = tmp_path / "halt"
    halt.touch()
    report = check_intents([_intent()], _view(), D("0"), RISK,
                           orders_today=0, in_submit_window=True, halt_path=halt)
    assert report.closed
    assert report.approved == []
    assert "halt" in report.rejected[0][1]


def test_outside_submit_window_rejected(tmp_path):
    """A0 fills at the close, so orders sent at any other moment are refused."""
    report = check_intents([_intent()], _view(), D("0"), RISK,
                           orders_today=0, in_submit_window=False,
                           halt_path=tmp_path / "halt")
    assert report.closed
    assert report.approved == []
    assert "submission window" in report.rejected[0][1]


def test_buy_beyond_available_cash_rejected(tmp_path):
    # 3 shares at 175 USD / 1.35 = 388.89 GBP + buffer > 200 GBP cash.
    report = check_intents([_intent(qty="3")], _view(cash="200"), D("0"), RISK,
                           orders_today=0, in_submit_window=True,
                           halt_path=tmp_path / "halt")
    assert report.approved == []
    assert "insufficient_free_for_stocks_buy" in report.rejected[0][1]


def test_sell_trimmed_to_held_never_enlarged(tmp_path):
    view = _view(positions={"NVDA": D("1.5")})
    report = check_intents([_intent(qty="-2")], view, D("0"), RISK,
                           orders_today=0, in_submit_window=True,
                           halt_path=tmp_path / "halt")
    assert len(report.approved) == 1
    assert report.approved[0].quantity == D("-1.5")


def test_sell_without_position_rejected(tmp_path):
    report = check_intents([_intent(qty="-1")], _view(), D("0"), RISK,
                           orders_today=0, in_submit_window=True,
                           halt_path=tmp_path / "halt")
    assert report.approved == []
    assert "never short" in report.rejected[0][1]


def test_quantity_floored_to_step_with_remainder(tmp_path):
    # 0.12347 has a remainder against the 0.0001 step; flooring must produce
    # 0.1234, not 0.1235 (a discriminative sample per verified-dev 4.3).
    view = _view(positions={"NVDA": D("1")})
    report = check_intents([_intent(qty="0.12347")], view, D("0"), RISK,
                           orders_today=0, in_submit_window=True,
                           halt_path=tmp_path / "halt")
    assert report.approved[0].quantity == D("0.1234")
    assert T212_QTY_STEP == D("0.0001")


def test_dust_below_min_value_rejected(tmp_path):
    report = check_intents([_intent(qty="0.0001")], _view(), D("0"), RISK,
                           orders_today=0, in_submit_window=True,
                           halt_path=tmp_path / "halt")
    assert report.approved == []
    assert "below" in report.rejected[0][1]


def test_daily_order_cap(tmp_path):
    intents = [_intent(intent_id=f"i{k}", symbol=s)
               for k, s in enumerate(["NVDA", "AAPL", "MSFT"])]
    report = check_intents(intents, _view(cash="10000"), D("0"),
                           {**RISK, "max_daily_orders": 2, "max_gross_notional_gbp": 99999,
                            "max_order_notional_gbp": 99999},
                           orders_today=1, in_submit_window=True,
                           halt_path=tmp_path / "halt")
    assert len(report.approved) == 1
    assert any("max_daily_orders" in reason for _, reason in report.rejected)


def test_gross_cap_counts_held_positions(tmp_path):
    # Held notional 900 + new buy 129.63 > 1000 cap -> rejected.
    report = check_intents([_intent(qty="1")], _view(cash="10000"), D("900"),
                           RISK, orders_today=0, in_submit_window=True,
                           halt_path=tmp_path / "halt")
    assert report.approved == []
    assert "gross" in report.rejected[0][1]


def test_a_sell_funds_a_later_buy_in_the_same_batch(tmp_path):
    """The backtest books a same-close sell inside submit(), so its cash
    reaches later intents. A static budget would reject this buy and push the
    live rejection rate above the recorded baseline."""
    sell = OrderIntent(intent_id="s1", symbol="NVDA", ticker="NVDA_US_EQ",
                       quantity=D("-3"), ref_price_usd=D("175"),
                       fx_usd_per_gbp=D("1.35"))
    buy = _intent(symbol="AAPL", qty="1", intent_id="b1")
    view = LedgerPortfolioView(cash_gbp=D("10"), available_cash_gbp=D("10"),
                               positions={"NVDA": D("3")},
                               pending_signed_qty={})
    report = check_intents([sell, buy], view, D("0"), RISK, orders_today=0,
                           in_submit_window=True, halt_path=tmp_path / "halt")
    assert [i.symbol for i in report.approved] == ["NVDA", "AAPL"]
    assert report.rejected == []


def test_a_buy_before_any_sell_still_fails_on_the_starting_cash(tmp_path):
    """Order is load bearing: the same pair the other way round cannot fund
    itself, exactly as in the backtest."""
    buy = _intent(symbol="AAPL", qty="1", intent_id="b1")
    sell = OrderIntent(intent_id="s1", symbol="NVDA", ticker="NVDA_US_EQ",
                       quantity=D("-3"), ref_price_usd=D("175"),
                       fx_usd_per_gbp=D("1.35"))
    view = LedgerPortfolioView(cash_gbp=D("10"), available_cash_gbp=D("10"),
                               positions={"NVDA": D("3")},
                               pending_signed_qty={})
    report = check_intents([buy, sell], view, D("0"), RISK, orders_today=0,
                           in_submit_window=True, halt_path=tmp_path / "halt")
    assert [i.symbol for i in report.approved] == ["NVDA"]
    assert any("insufficient_free_for_stocks_buy" in r for _, r in report.rejected)


# ============================================================================
# Per-symbol quantity precision (2026-08-31 first live session: INTC rejected
# with "invalid quantity precision 3" while 4-decimal siblings passed)
# ============================================================================

def test_intc_quantity_floors_to_three_decimals():
    from decimal import Decimal
    from trading212.execution.risk_gate import QTY_STEP_OVERRIDES, qty_step
    assert qty_step("INTC") == Decimal("0.001")
    assert qty_step("NVDA") == Decimal("0.0001")
    assert "INTC" in QTY_STEP_OVERRIDES


def test_gate_trims_intc_to_its_step():
    from decimal import Decimal
    from trading212.execution.risk_gate import OrderIntent, _check_one

    class _View:
        positions = {}
        pending_signed_qty = {}
        cash_gbp = Decimal("1000")
        available_cash_gbp = Decimal("1000")

    intent = OrderIntent(intent_id="x", symbol="INTC", ticker="INTC_US_EQ",
                         quantity=Decimal("0.8326"),
                         ref_price_usd=Decimal("89.5"),
                         fx_usd_per_gbp=Decimal("1.35"))
    out = _check_one(intent, _View(), max_order=Decimal("70"),
                     min_value=Decimal("1"))
    assert not isinstance(out, str)
    assert out.quantity == Decimal("0.832")
