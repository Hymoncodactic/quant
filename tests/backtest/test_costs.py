"""Cost-model tests for the T212 adapter.

Responsibility: pin the arithmetic of backtest/t212/costs.py, both the
currency conversion and the per-fill fees and taxes. Conversion covers the
three accepted quote currencies and the rejection of anything else. Fees cover
the FX conversion fee, charged only on USD and worsening the customer on both
sides; UK stamp duty, charged on LSE stock buys only; the PTM levy, with its
principal threshold, its exchange-traded-fund flag and its once-per-order
accumulation across partial fills; the US sell-side fees; the spread sign; the
ordering of the two cost tiers; and the French financial transaction tax.
Discrimination design is recorded in docs/backtest/validation/02_test_plan.md items
U2, U7, U8 and U11: for example the conversion cases use a non-unity rate, so
a swapped multiply and divide would yield 127 instead of about 78.74, and the
partial-fill case splits 12,000 GBP into three fills none of which alone
crosses the 10,000 GBP threshold, so per-fill logic would charge nothing.

Out of scope: when a fill happens and at what price, which is covered by
tests/backtest/test_broker.py; the cost figures used by a real run, which come
from configuration rather than from these tests; the ledger's handling of the
resulting cash deltas, covered by tests/backtest/test_ledger_metrics.py.

Public functions:
    test_usd_to_gbp_divides_by_rate()
        A USD price converts by division by the GBPUSD rate.
    test_gbp_pence_divides_by_100()
        A GBp price converts by division by 100.
    test_gbp_passthrough()
        A GBP price passes through unchanged.
    test_unknown_currency_rejected()
        An unlisted currency raises ValueError rather than converting.
    test_fx_fee_charged_on_usd_both_sides()
        Both a USD buy and a USD sell pay the conversion fee, and the buy's
        cash matches the official effective-rate mechanics.
    test_no_fx_fee_on_gbp_or_pence()
        Neither GBP nor GBp carries a conversion fee, and both reach the same
        GBP notional.
    test_stamp_duty_three_way()
        An LSE stock buy pays the tax, the same stock's sell does not, an LSE
        fund buy does not, and the tax visibly moves cash.
    test_ptm_levy_threshold_and_etf_flag()
        The levy is absent below the principal threshold, is a flat charge
        above it, and follows the exchange-traded-fund flag.
    test_us_sell_side_fees()
        The two US fees appear on sells only and match their per-share and
        per-value formulas.
    test_worst_tier_strictly_worse_than_actual()
        The worst tier's buy price is strictly above the actual tier's, which
        is strictly above the raw price.
    test_spread_signs()
        A buy pays up by the half spread and a sell receives less by it.
    test_ptm_levy_accumulates_across_partial_fills()
        Three partial fills of one order charge the levy exactly once, on the
        fill that carries the cumulative principal past the threshold.
    test_french_ftt_on_qualifying_buys_only()
        The French tax applies to qualifying French buys, not to their sells
        and not to UK stock buys.

Public classes: None.

Constants:
    D
        Alias of decimal.Decimal, used so the tests exercise the same numeric
        type as the production path.
    CFG
        Cost configuration with slippage_bps 0 and spread_session_multiplier
        1. Both are neutralized so a failing assertion points at the fee
        arithmetic under test and not at a spread or slippage term. The
        remaining fields keep their defaults, which are the worst-tier values.

Inputs: None. Every case is a direct call with literal arguments.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backtest.t212.costs import (CostConfig, apply_spread, fill_cash_and_costs,
                                 price_to_gbp)

D = Decimal
CFG = CostConfig(slippage_bps=D("0"), spread_session_multiplier=D("1"))


# ---------------------------------------------------------------------------
# U2: conversion directions with a non-unity rate. A swapped multiply/divide
# yields 127 instead of ~78.74, so the expectations discriminate.
# ---------------------------------------------------------------------------

def test_usd_to_gbp_divides_by_rate():
    assert price_to_gbp(D("100"), "USD", D("1.27")) == D("100") / D("1.27")


def test_gbp_pence_divides_by_100():
    assert price_to_gbp(D("250"), "GBp", None) == D("2.5")


def test_gbp_passthrough():
    assert price_to_gbp(D("3.14"), "GBP", None) == D("3.14")


def test_unknown_currency_rejected():
    with pytest.raises(ValueError):
        price_to_gbp(D("1"), "EUR", None)


# ---------------------------------------------------------------------------
# U8: FX fee three-way -- USD pays it, GBP and GBp do not; and it worsens the
# customer on BOTH sides (official worked example: 1.41 -> 1.412115).
# ---------------------------------------------------------------------------

def test_fx_fee_charged_on_usd_both_sides():
    mid = D("1.40")
    buy_cash, _, buy_costs = fill_cash_and_costs(D("10"), D("100"), "USD", mid,
                                                 "stock", False, CFG)
    sell_cash, _, sell_costs = fill_cash_and_costs(D("-10"), D("100"), "USD", mid,
                                                   "stock", False, CFG)
    mid_gbp = D("1000") / mid
    assert -buy_cash > mid_gbp            # buy costs more than mid conversion
    assert sell_cash < mid_gbp            # sell yields less than mid conversion
    assert buy_costs["currency_conversion_fee"] > 0
    assert sell_costs["currency_conversion_fee"] > 0
    # Official mechanics: effective buy rate = mid * (1 - 0.0015).
    assert -buy_cash == D("1000") / (mid * (1 - D("0.0015")))


def test_no_fx_fee_on_gbp_or_pence():
    for ccy, price in (("GBP", D("10")), ("GBp", D("1000"))):
        cash, _, costs = fill_cash_and_costs(D("10"), price, ccy, None,
                                          "etf", True, CFG)
        assert "currency_conversion_fee" not in costs
        assert -cash == D("100")          # both are 100 GBP notional


# ---------------------------------------------------------------------------
# U7: stamp duty three-way -- LSE stock buy pays 0.5%, the same stock's sell
# pays none, an LSE ETF buy pays none. Three different outcomes.
# ---------------------------------------------------------------------------

def test_stamp_duty_three_way():
    buy_stock, _, costs_bs = fill_cash_and_costs(D("100"), D("250"), "GBp", None,
                                              "stock", True, CFG)
    sell_stock, _, costs_ss = fill_cash_and_costs(D("-100"), D("250"), "GBp", None,
                                               "stock", True, CFG)
    buy_etf, _, costs_be = fill_cash_and_costs(D("100"), D("250"), "GBp", None,
                                            "etf", True, CFG)
    assert costs_bs["stamp_duty_reserve_tax"] == D("250") * D("0.005")
    assert "stamp_duty_reserve_tax" not in costs_ss
    assert "stamp_duty_reserve_tax" not in costs_be
    assert -buy_stock > -buy_etf          # the tax actually moves cash


# ---------------------------------------------------------------------------
# PTM levy: threshold behavior and the unverified-ETF flag.
# ---------------------------------------------------------------------------

def test_ptm_levy_threshold_and_etf_flag():
    below, _, costs_below = fill_cash_and_costs(D("10"), D("999"), "GBP", None,
                                             "stock", True, CFG)
    above, _, costs_above = fill_cash_and_costs(D("11"), D("1000"), "GBP", None,
                                             "stock", True, CFG)
    assert "ptm_levy" not in costs_below   # 9,990 GBP is under the threshold
    assert costs_above["ptm_levy"] == D("1.50")
    no_etf_cfg = CostConfig(slippage_bps=D("0"),
                            spread_session_multiplier=D("1"),
                            ptm_levy_on_etf=False)
    _, _, etf_on = fill_cash_and_costs(D("11"), D("1000"), "GBP", None,
                                    "etf", True, CFG)
    _, _, etf_off = fill_cash_and_costs(D("11"), D("1000"), "GBP", None,
                                     "etf", True, no_etf_cfg)
    assert etf_on.get("ptm_levy") == D("1.50")
    assert "ptm_levy" not in etf_off


# ---------------------------------------------------------------------------
# US sell-side fees: FINRA per share, SEC on value, sells only.
# ---------------------------------------------------------------------------

def test_us_sell_side_fees():
    mid = D("1.25")
    _, _, buy_costs = fill_cash_and_costs(D("100"), D("50"), "USD", mid,
                                       "stock", False, CFG)
    _, _, sell_costs = fill_cash_and_costs(D("-100"), D("50"), "USD", mid,
                                        "stock", False, CFG)
    assert "finra_fee" not in buy_costs and "transaction_fee" not in buy_costs
    assert sell_costs["finra_fee"] == D("0.000195") * 100 / mid
    assert sell_costs["transaction_fee"] == D("5000") * D("0.0000206") / mid


# ---------------------------------------------------------------------------
# U11: the two tiers must produce different, ordered outcomes.
# ---------------------------------------------------------------------------

def test_worst_tier_strictly_worse_than_actual():
    worst, actual = CostConfig(), CostConfig.actual_tier()
    raw = D("100")
    hs = D("1")
    buy_worst = apply_spread(raw, True, hs, worst.slippage_bps)
    buy_actual = apply_spread(raw, True, hs, actual.slippage_bps)
    assert buy_worst > buy_actual > raw


# ---------------------------------------------------------------------------
# Spread direction: buys pay up, sells receive less (U3 companion).
# ---------------------------------------------------------------------------

def test_spread_signs():
    raw = D("100")
    assert apply_spread(raw, True, D("10"), D("0")) == D("100.1")
    assert apply_spread(raw, False, D("10"), D("0")) == D("99.9")


# ---------------------------------------------------------------------------
# PTM levy is once per ORDER: partial fills accumulate toward the threshold
# and never pay twice. Per-fill logic charges 0 here (no single fill exceeds
# 10,000), so the expectations discriminate.
# ---------------------------------------------------------------------------

def test_ptm_levy_accumulates_across_partial_fills():
    fills = []
    prior, charged = D("0"), False
    for qty in (D("50"), D("50"), D("20")):     # 120 x 100 GBP = 12,000 total
        _, principal, costs = fill_cash_and_costs(
            qty, D("100"), "GBP", None, "etf", True, CFG,
            prior_order_principal_gbp=prior, ptm_already_charged=charged)
        prior += principal
        charged = charged or "ptm_levy" in costs
        fills.append(costs)
    levies = [c["ptm_levy"] for c in fills if "ptm_levy" in c]
    assert levies == [D("1.50")]
    assert "ptm_levy" in fills[2] and "ptm_levy" not in fills[0]


# ---------------------------------------------------------------------------
# French FTT: buys of qualifying French shares only (kind == "stock_fr").
# ---------------------------------------------------------------------------

def test_french_ftt_on_qualifying_buys_only():
    _, _, buy_fr = fill_cash_and_costs(D("10"), D("100"), "GBP", None,
                                       "stock_fr", False, CFG)
    _, _, sell_fr = fill_cash_and_costs(D("-10"), D("100"), "GBP", None,
                                        "stock_fr", False, CFG)
    _, _, buy_uk = fill_cash_and_costs(D("10"), D("100"), "GBP", None,
                                       "stock", False, CFG)
    assert buy_fr["french_transaction_tax"] == D("1000") * D("0.004")
    assert "french_transaction_tax" not in sell_fr
    assert "french_transaction_tax" not in buy_uk
