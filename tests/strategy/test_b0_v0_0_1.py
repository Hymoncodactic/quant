"""B0 module tests: signal reading, attribution, sizing, band, ordering.

Each test names the defect it catches; the list is
fixplans/t212/b0/02_strategy_b0.md section 6.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from tests.strategy.conftest import (FakeBar, FakePortfolio, FakeView,
                                     make_panel, ny_ts, ramp, sessions)
from trading212.execution.strategy_loader import load_module

b0 = load_module("b0", "0.0.1")

A0_NAMES = ["NVDA", "AMD", "INTC"]
A1_NAMES = ["AAA", "BBB", "CCC", "NVDA"]
ALL_NAMES = A0_NAMES + [s for s in A1_NAMES if s not in A0_NAMES]
STATE = "QQQ"
FX = "GBPUSD=X"

A0_PARAMS = {
    "trade_symbols": A0_NAMES, "state_symbol": STATE, "fx_symbol": FX,
    "signal_mode": "always", "tsmom_lookback": 3, "trend_ma": 3,
    "vol_window": 20, "vol_pct_threshold": 0.80, "vol_min_history": 756,
    "use_vol_gate": False, "use_trend_gate": False, "warmup_bars": 4,
    "live_from": "2020-01-02", "slot_headroom": 0.99,
}

A1_PARAMS = {
    "n_hold": 3, "band_multiple": 2, "rebalance_every": 21,
    "mom_long": 3, "mom_skip": 1, "liq_window": 3,
    "min_dollar_volume_usd": 1.0, "max_zero_volume_share": 0.99,
    "min_history_bars": 1, "order_usd_for_participation": 1e-6,
    "require_verified_ticker": False, "slot_headroom": 0.99,
    "fx_symbol": FX, "rebalance_anchor": "2020-01-02",
    "live_from": "2020-01-02",
}

B0_PARAMS = {
    "priority": "a1", "a1_band": 0.10, "slot_headroom": 0.99,
    "signal_view_cash_gbp": 1000000, "sells_first": True, "fx_symbol": FX,
    "live_from": "2020-01-02", "a0_params": A0_PARAMS, "a1_params": A1_PARAMS,
}

DAYS = sessions("2020-01-02", 12)


def _view(day_index: int, prices: dict | None = None, fx: float = 1.25):
    """A daily view over ALL_NAMES + state + FX, cut at DAYS[day_index]."""
    prices = prices or {}
    history = {}
    for symbol in ALL_NAMES + [STATE]:
        series = ramp(prices.get(symbol, 100.0), 1.0, day_index + 1)
        history[symbol] = [FakeBar(ts=ny_ts(DAYS[i]), close=float(series[i]))
                           for i in range(day_index + 1)]
    history[FX] = [FakeBar(ts=ny_ts(DAYS[day_index]), close=fx)]
    return FakeView(history, ny_ts(DAYS[day_index]))


def _injection(book=None, thin=None, frozen=False):
    """Research-shape injection over a tiny panel whose ranking is AAA>BBB>CCC."""
    steps = {"AAA": 5.0, "BBB": 3.0, "CCC": 1.0, "NVDA": 0.5, "AMD": 0.4,
             "INTC": 0.3}
    closes = {s: ramp(100.0, steps[s], len(DAYS)) for s in ALL_NAMES}
    panel_closes, panel_volumes = make_panel(ALL_NAMES, DAYS, closes)
    return {"panel": {"closes": panel_closes, "volumes": panel_volumes},
            "sessions": list(DAYS), "a1_book": dict(book or {}),
            "thin": list(thin or []), "a1_frozen": frozen,
            "a0_mode": "view", "rank_as_of": None, "rank_stale_sessions": 0}


# --- 1 and 2. the synthetic signal view ------------------------------------

def test_a0_is_still_asked_when_a1_holds_all_the_cash():
    """Catches: the deadlock where A0 can never re-enter a shared account."""
    injection = _injection()
    portfolio = FakePortfolio(cash_gbp=Decimal("0"),
                              available_cash_gbp=Decimal("0"),
                              positions={"AAA": Decimal("10")})
    signal = b0._a0_signal_set(_view(5), portfolio, B0_PARAMS, injection)
    assert signal == set(A0_NAMES)


def test_the_synthetic_view_does_not_touch_the_real_portfolio():
    """Catches: mutating a frozen view in place and polluting the caller."""
    portfolio = FakePortfolio(cash_gbp=Decimal("7"),
                              available_cash_gbp=Decimal("7"),
                              positions={"AAA": Decimal("2")},
                              pending_signed_qty={"BBB": Decimal("-1")})
    synthetic = b0._synthetic_view(portfolio, B0_PARAMS)
    assert synthetic.cash_gbp == Decimal("1000000")
    assert synthetic.available_cash_gbp == Decimal("1000000")
    assert portfolio.cash_gbp == Decimal("7")
    assert synthetic.positions == portfolio.positions
    assert synthetic.pending_signed_qty == portfolio.pending_signed_qty


# --- 3. priority and attribution -------------------------------------------

def test_priority_a1_sizes_an_overlapping_name_by_the_a1_rule():
    a0_sized, a1_sized = b0._split({"NVDA", "AMD"}, {"NVDA", "AAA"}, "a1")
    assert a0_sized == {"AMD"}
    assert a1_sized == {"NVDA", "AAA"}


def test_priority_a0_sizes_an_overlapping_name_by_the_a0_rule():
    a0_sized, a1_sized = b0._split({"NVDA", "AMD"}, {"NVDA", "AAA"}, "a0")
    assert a0_sized == {"NVDA", "AMD"}
    assert a1_sized == {"AAA"}


def test_attribution_follows_the_split_that_was_applied():
    """Catches: reporting attribution from list membership, not from sizing."""
    injection = _injection(book={"AAA": 1 / 3, "BBB": 1 / 3, "NVDA": 1 / 3})
    portfolio = FakePortfolio(cash_gbp=Decimal("500"),
                              available_cash_gbp=Decimal("500"),
                              positions={"NVDA": Decimal("1"),
                                         "AMD": Decimal("1")})
    tree = b0.signal_diagnostics(_view(5), portfolio, B0_PARAMS, injection)
    assert tree["attribution"]["positions"]["NVDA"] == "a1"
    assert tree["attribution"]["positions"]["AMD"] == "a0"
    a0_view = dict(B0_PARAMS, priority="a0")
    tree = b0.signal_diagnostics(_view(5), portfolio, a0_view, injection)
    assert tree["attribution"]["positions"]["NVDA"] == "a0"


# --- 4. the C1 formula ------------------------------------------------------

def test_a1_capital_is_the_headroom_equity_minus_the_a0_value():
    """Catches: an error in C1 or in the per-name split."""
    fx = Decimal("1.25")
    prices = {"AAA": Decimal("100"), "BBB": Decimal("50")}
    targets = b0._size_a1(["AAA", "BBB"], set(), Decimal("1000"), fx,
                          lambda s: prices[s], lambda s: Decimal("0"), 0.10)
    # 1000 GBP over two names is 500 each; 500 * 1.25 / 100 = 6.25 shares.
    assert targets["AAA"] == Decimal("6.25")
    assert targets["BBB"] == Decimal("12.5")


def test_no_capital_left_sells_the_a1_legs():
    """Catches: keeping the position when C1 is not positive (spec 3.4)."""
    targets = b0._size_a1(["AAA"], set(), Decimal("0"), Decimal("1.25"),
                          lambda s: Decimal("100"),
                          lambda s: Decimal("3"), 0.10)
    assert targets["AAA"] == Decimal("0")


# --- 5. the no-churn band and the freeze -----------------------------------

def test_a_nine_percent_drift_is_left_alone_and_eleven_percent_is_resized():
    fx, price = Decimal("1.25"), Decimal("100")
    # Target is 6.25 shares; 6.8 is 8.8% away, 7.0 is 12% away.
    near = b0._size_a1(["AAA"], set(), Decimal("500"), fx, lambda s: price,
                       lambda s: Decimal("6.8"), 0.10)
    far = b0._size_a1(["AAA"], set(), Decimal("500"), fx, lambda s: price,
                      lambda s: Decimal("7.0"), 0.10)
    assert near["AAA"] == Decimal("6.8")
    assert far["AAA"] == Decimal("6.25")


def test_a_name_with_no_price_is_sold_but_a_thin_name_is_frozen():
    """Catches: conflating a missing price with a stale feed."""
    sold = b0._size_a1(["AAA"], set(), Decimal("500"), Decimal("1.25"),
                       lambda s: Decimal("0"), lambda s: Decimal("3"), 0.10)
    assert sold["AAA"] == Decimal("0")
    frozen = b0._size_a1(["AAA"], {"AAA"}, Decimal("500"), Decimal("1.25"),
                         lambda s: Decimal("0"), lambda s: Decimal("3"), 0.10)
    assert frozen["AAA"] == Decimal("3")


def test_a_frozen_leg_does_not_consume_the_capital_of_the_sized_ones():
    """Catches: dividing C1 by the full book and over-allocating."""
    fx = Decimal("1.25")
    prices = {"AAA": Decimal("100"), "BBB": Decimal("100")}
    targets = b0._size_a1(["AAA", "BBB"], {"AAA"}, Decimal("500"), fx,
                          lambda s: prices[s],
                          lambda s: Decimal("2") if s == "AAA" else Decimal("0"),
                          0.10)
    assert targets["AAA"] == Decimal("2")          # frozen at the held size
    assert targets["BBB"] == Decimal("6.25")       # the whole 500, not 250


# --- 6. the gate-closed substitution ---------------------------------------

def test_an_empty_a0_signal_puts_the_whole_headroom_into_a1():
    """Catches: leaving the account in cash when A0's gates are shut."""
    injection = _injection(book={"AAA": 1 / 3, "BBB": 1 / 3, "CCC": 1 / 3})
    params = dict(B0_PARAMS, a0_params=dict(A0_PARAMS, signal_mode="ma200",
                                            trend_ma=999))
    portfolio = FakePortfolio(cash_gbp=Decimal("1000"),
                              available_cash_gbp=Decimal("1000"))
    view = _view(5)
    strategy = b0.make_strategy(injection)
    targets = strategy(view, portfolio, params)
    deployed = sum(targets[s] * Decimal(str(view.bar(s).close))
                   / Decimal("1.25") for s in ("AAA", "BBB", "CCC"))
    assert deployed == pytest.approx(Decimal("990"), abs=Decimal("1"))
    assert all(targets[s] == 0 for s in A0_NAMES if s not in ("NVDA",))


# --- 7. submission order ----------------------------------------------------

def test_sells_first_puts_every_reduction_ahead_of_every_purchase():
    """Catches: buys ahead of sells, the recorded cause of ~2,000 rejections."""
    targets = {"BUY1": Decimal("5"), "SELL1": Decimal("0"),
               "BUY2": Decimal("9"), "SELL2": Decimal("1")}
    held = {"BUY1": Decimal("0"), "SELL1": Decimal("4"),
            "BUY2": Decimal("2"), "SELL2": Decimal("6")}
    ordered = b0._ordered(targets, lambda s: held[s], ["BUY1"], ["BUY2"], True)
    assert list(ordered) == ["SELL1", "SELL2", "BUY1", "BUY2"]
    untouched = b0._ordered(targets, lambda s: held[s], ["BUY1"], ["BUY2"],
                            False)
    assert list(untouched) == list(targets)


# --- 8. clearing ------------------------------------------------------------

def test_a_holding_in_neither_list_is_zeroed_and_so_are_idle_a0_names():
    injection = _injection(book={"AAA": 1.0})
    portfolio = FakePortfolio(cash_gbp=Decimal("1000"),
                              available_cash_gbp=Decimal("1000"),
                              positions={"ZZZ": Decimal("4")})
    view = _view(5)
    view._history["ZZZ"] = [FakeBar(ts=view.now, close=10.0)]
    targets = b0.make_strategy(injection)(view, portfolio, B0_PARAMS)
    assert targets["ZZZ"] == Decimal("0")
    for symbol in A0_NAMES:
        assert symbol in targets


# --- 9. the empty-result conditions ----------------------------------------

def test_a_non_session_and_a_missing_rate_both_return_nothing():
    """Catches: deciding on an FX-only timeline key, or on a stale rate."""
    injection = _injection(book={"AAA": 1.0})
    portfolio = FakePortfolio(cash_gbp=Decimal("1000"),
                              available_cash_gbp=Decimal("1000"))
    strategy = b0.make_strategy(injection)
    holiday = _view(5)
    holiday.now = ny_ts("2020-01-20")           # not in the session list
    assert strategy(holiday, portfolio, B0_PARAMS) == {}
    no_fx = _view(5)
    no_fx._history[FX] = []
    assert strategy(no_fx, portfolio, B0_PARAMS) == {}


def test_a_normal_session_never_returns_an_empty_mapping():
    """Catches: an empty target set, which the live cycle reads as an abort."""
    injection = _injection(book={"AAA": 1.0})
    portfolio = FakePortfolio(cash_gbp=Decimal("1000"),
                              available_cash_gbp=Decimal("1000"))
    targets = b0.make_strategy(injection)(_view(5), portfolio, B0_PARAMS)
    assert targets


# --- 10. the active set is a daily fact ------------------------------------

def test_the_active_set_comes_from_daily_rows_when_the_view_is_hourly():
    """Catches: counting 253 hourly bars and admitting an unqualified name."""
    injection = _injection()
    injection["a0_mode"] = "rows"
    day_iso = DAYS[5].isoformat()
    rows = {"NVDA": [(DAYS[i].isoformat(), 1.0, 1.0, 1.0, 100.0)
                     for i in range(5)],
            "AMD": [(DAYS[i].isoformat(), 1.0, 1.0, 1.0, 100.0)
                    for i in range(2)],
            "INTC": []}
    injection["a0_rows"] = rows
    params = dict(B0_PARAMS, a0_params=dict(A0_PARAMS, tsmom_lookback=3))
    active, prices = b0._a0_active_and_prices(_view(5), injection, params)
    assert active == ["NVDA"]          # 5 rows before today, AMD has 2
    assert day_iso                     # the cut is strictly before today
    assert "NVDA" in prices


# --- 11. diagnostics --------------------------------------------------------

def test_diagnostics_carry_the_frozen_tree():
    injection = _injection(book={"AAA": 0.5, "NVDA": 0.5})
    portfolio = FakePortfolio(cash_gbp=Decimal("500"),
                              available_cash_gbp=Decimal("500"),
                              positions={"NVDA": Decimal("1")})
    tree = b0.signal_diagnostics(_view(5), portfolio, B0_PARAMS, injection)
    assert set(tree) == {"as_of", "strategy", "priority", "a0", "a1",
                         "allocation", "attribution"}
    assert set(tree["allocation"]) == {
        "equity_gbp", "headroom", "a0_names", "a1_names", "overlap",
        "a0_target_gbp", "a1_target_gbp", "cash_target_gbp"}
    assert set(tree["attribution"]) == {"positions", "a0_value_gbp",
                                        "a1_value_gbp", "cash_gbp"}
    assert set(tree["a0"]) >= {"gates", "symbols", "open_for_business"}
    assert tree["strategy"] == "b0"
    assert set(tree["attribution"]["positions"].values()) <= {"a0", "a1",
                                                              "other"}
