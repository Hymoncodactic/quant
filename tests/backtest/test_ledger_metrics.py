"""Ledger and metrics tests: cash exactness, occupancy definition and the two
medians.

Responsibility: pin the parts of backtest/engine/ledger.py and
backtest/engine/metrics.py whose definition decides the magnitude and the
honesty of every reported number. Cash is Decimal, so a sequence that a float
ledger would leave with a trailing remainder must come out exact. The ledger
refuses to sell more than is held and refuses to drive cash negative, and it
releases cost basis in proportion to the shares that leave. Capital is the
peak concurrent occupancy rather than the cumulative outlay: the overlapping
and the sequential deployment in these tests have the same cumulative outlay
of 200 and different peaks of 200 and 100, so an implementation that sums
outlays cannot satisfy both. Days in market count only the days on which
occupancy was positive. Realized profit and loss is replayed on an
average-cost basis. The signed median and the median absolute deviation are
separate numbers, as required by CLAUDE.md section 2.3: the sample used here
has signed median 2 and absolute deviation median 8, so reporting one number
for both cannot pass. A reservation reduces available cash without changing
the cash balance. Discrimination design is recorded in
fixplans/validation/02_test_plan.md items U1 and U13.

Out of scope: what causes the fills, which is covered by
tests/backtest/test_broker.py; the liquidation-valued metrics added by the
2026-08-21 review, covered by
tests/backtest/test_conservatism_and_metrics.py; the shared fixtures, which
this module does not use because its inputs are single fills and hand-built
frames rather than whole runs.

Public functions:
    test_cash_arithmetic_is_exact()
        Two fills of 0.1 and 0.2 leave exactly 0.7 cash and 0.3 of cost basis.
    test_sell_beyond_holdings_refused()
        Selling more than is held raises rather than going short.
    test_negative_cash_refused()
        A fill that would drive cash negative raises.
    test_average_cost_release()
        Selling a quarter of the shares releases a quarter of the cost basis.
    test_capital_is_peak_not_cumulative()
        Overlapping deployment reports a peak of 200, sequential deployment a
        peak of 100.
    test_online_days_counts_occupied_days_only()
        Days with zero occupancy are not counted as days in market.
    test_realized_pnl_average_cost()
        Two sells against one buy replay to plus ten and minus ten.
    test_signed_and_absolute_medians_are_separate()
        The signed median and the median absolute deviation come out as two
        different numbers.
    test_reservation_affects_available_not_cash()
        A reservation lowers available cash and raises occupancy while the
        cash balance is unchanged, and releasing it restores availability.

Public classes: None.

Constants:
    D
        Alias of decimal.Decimal. The exactness case depends on the ledger
        holding Decimal rather than float, so the tests must construct Decimal
        values too.

Inputs: None. Fills, equity curves and trade frames are built in process; no
path under data/ is read.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backtest.engine.ledger import Ledger
from backtest.engine.metrics import compute_metrics, realized_pnl_per_sell
from backtest.engine.types import Fill

D = Decimal


def _fill(symbol: str, step: int, qty: str, cash: str) -> Fill:
    return Fill(order_id=step, symbol=symbol,
                ts=pd.Timestamp("2026-01-05", tz="UTC"), step=step,
                quantity=D(qty), price=D("1"), quote_ccy="GBP", fx_mid=None,
                cash_delta_gbp=D(cash))


# ---------------------------------------------------------------------------
# U1: Decimal exactness. 0.1 + 0.2 is not representable in binary floats;
# a float-backed ledger ends at 0.30000000000000004-flavored cash.
# ---------------------------------------------------------------------------

def test_cash_arithmetic_is_exact():
    ledger = Ledger(initial_cash_gbp=D("1"))
    ledger.apply_fill(_fill("A", 1, "1", "-0.1"))
    ledger.apply_fill(_fill("A", 2, "2", "-0.2"))
    assert ledger.cash_gbp == D("0.7")
    assert ledger.positions["A"].cost_basis_gbp == D("0.3")


def test_sell_beyond_holdings_refused():
    ledger = Ledger(initial_cash_gbp=D("100"))
    ledger.apply_fill(_fill("A", 1, "1", "-10"))
    with pytest.raises(ValueError, match="exceeds held"):
        ledger.apply_fill(_fill("A", 2, "-2", "20"))


def test_negative_cash_refused():
    ledger = Ledger(initial_cash_gbp=D("5"))
    with pytest.raises(ValueError, match="negative"):
        ledger.apply_fill(_fill("A", 1, "1", "-10"))


def test_average_cost_release():
    ledger = Ledger(initial_cash_gbp=D("100"))
    ledger.apply_fill(_fill("A", 1, "4", "-40"))
    ledger.apply_fill(_fill("A", 2, "-1", "15"))
    # A quarter of the basis leaves with a quarter of the shares.
    assert ledger.positions["A"].qty == D("3")
    assert ledger.positions["A"].cost_basis_gbp == D("30")


# ---------------------------------------------------------------------------
# U13: capital = PEAK concurrent occupancy. Overlapping and sequential
# deployments have identical cumulative outlay (200) but different peaks
# (200 vs 100); an implementation summing outlays cannot pass both.
# ---------------------------------------------------------------------------

def _equity_frame(occupied: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "step": range(len(occupied)),
        "ts": pd.date_range("2026-01-05", periods=len(occupied), freq="D"),
        "cash_gbp": [1000.0] * len(occupied),
        "reserved_gbp": [0.0] * len(occupied),
        "invested_cost_gbp": occupied,
        "occupied_gbp": occupied,
        "equity_gbp": [1000.0] * len(occupied),
    })


def test_capital_is_peak_not_cumulative():
    overlapping = compute_metrics(_equity_frame([100, 200, 100, 0]),
                                  pd.DataFrame(), 1000.0, 252)
    sequential = compute_metrics(_equity_frame([100, 0, 100, 0]),
                                 pd.DataFrame(), 1000.0, 252)
    assert overlapping["capital_peak_occupied_gbp"] == 200
    assert sequential["capital_peak_occupied_gbp"] == 100


def test_online_days_counts_occupied_days_only():
    metrics = compute_metrics(_equity_frame([0, 100, 100, 0]),
                              pd.DataFrame(), 1000.0, 252)
    assert metrics["online_trading_days"] == 2


# ---------------------------------------------------------------------------
# Realized PnL replay and the two medians.
# ---------------------------------------------------------------------------

def test_realized_pnl_average_cost():
    trades = pd.DataFrame([
        {"step": 1, "order_id": 1, "symbol": "A", "quantity": 4.0,
         "cash_delta_gbp": -40.0},
        {"step": 2, "order_id": 2, "symbol": "A", "quantity": -2.0,
         "cash_delta_gbp": 30.0},
        {"step": 3, "order_id": 3, "symbol": "A", "quantity": -2.0,
         "cash_delta_gbp": 10.0},
    ])
    assert realized_pnl_per_sell(trades) == [10.0, -10.0]


def test_signed_and_absolute_medians_are_separate():
    # Signed median hides the dispersion: [-10, +2, +10] has signed median 2
    # but absolute deviation median 8. An implementation reporting one number
    # for both cannot satisfy the pair.
    days = pd.date_range("2026-01-05", periods=4, freq="D", tz="UTC")
    trades = pd.DataFrame([
        {"step": 1, "order_id": 1, "symbol": "A", "quantity": 3.0,
         "cash_delta_gbp": -30.0, "ts": days[0]},
        {"step": 2, "order_id": 2, "symbol": "A", "quantity": -1.0,
         "cash_delta_gbp": 0.0, "ts": days[1]},
        {"step": 3, "order_id": 3, "symbol": "A", "quantity": -1.0,
         "cash_delta_gbp": 12.0, "ts": days[2]},
        {"step": 4, "order_id": 4, "symbol": "A", "quantity": -1.0,
         "cash_delta_gbp": 20.0, "ts": days[3]},
    ])
    equity = _equity_frame([30, 30, 30, 0])
    metrics = compute_metrics(equity, trades, 1000.0, 252)
    assert metrics["pnl_median_signed_gbp"] == 2.0
    assert metrics["pnl_median_abs_deviation_gbp"] == 8.0


def test_reservation_affects_available_not_cash():
    ledger = Ledger(initial_cash_gbp=D("100"))
    ledger.reserve(7, D("40"))
    assert ledger.cash_gbp == D("100")
    assert ledger.available_cash_gbp == D("60")
    assert ledger.occupied_gbp == D("40")
    ledger.release(7)
    assert ledger.available_cash_gbp == D("100")
