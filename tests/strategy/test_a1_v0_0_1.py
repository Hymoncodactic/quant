"""A1 module tests: admission, score, buffer band, calendar, ordering.

Each test names the defect it catches; the list is
fixplans/t212/b0/01_strategy_a1.md section 6.
"""

from __future__ import annotations

import copy
from decimal import Decimal

import pandas as pd
import pytest

from tests.strategy.conftest import (FakeBar, FakePortfolio, FakeView,
                                     make_panel, ny_ts, ramp, sessions)
from trading212.execution.strategy_loader import load_module

a1 = load_module("a1", "0.0.1")

PARAMS = {
    "n_hold": 3, "band_multiple": 2, "rebalance_every": 21,
    "mom_long": 252, "mom_skip": 21, "liq_window": 252,
    "min_dollar_volume_usd": 1_000_000, "max_zero_volume_share": 0.01,
    "min_history_bars": 300, "order_usd_for_participation": 640,
    "require_verified_ticker": False, "slot_headroom": 0.99,
    "fx_symbol": "GBPUSD=X", "rebalance_anchor": "2020-01-02",
    "live_from": "2020-01-02",
}

SYMBOLS = ["AAA", "BBB", "CCC", "DDD", "EEE"]


def _panel(n_days: int = 400, steps=None):
    days = sessions("2018-01-01", n_days)
    steps = steps or {"AAA": 1.0, "BBB": 0.8, "CCC": 0.6, "DDD": 0.4,
                      "EEE": 0.2}
    closes = {s: ramp(100.0, steps[s], n_days) for s in SYMBOLS}
    return days, make_panel(SYMBOLS, days, closes)


def _view(days, closes, day_index: int, fx: float = 1.25):
    """A view whose last bar for every symbol is the panel row at day_index."""
    history = {}
    for symbol in closes.columns:
        history[symbol] = [FakeBar(ts=ny_ts(d),
                                   close=float(closes.iloc[i][symbol]))
                           for i, d in enumerate(closes.index[:day_index + 1])]
    history["GBPUSD=X"] = [FakeBar(ts=ny_ts(days[day_index]), close=fx)]
    return FakeView(history, ny_ts(days[day_index]))


# --- 1. causality -----------------------------------------------------------

def test_ranking_ignores_prices_after_the_decision_day():
    """Catches: the panel was not truncated at as_of."""
    days, (closes, volumes) = _panel()
    as_of = days[350]
    before = a1.rank_table(closes, volumes, as_of, PARAMS)
    tampered = closes.copy()
    tampered.loc[tampered.index > as_of] = \
        tampered.loc[tampered.index > as_of] * 3.0 + 11.0
    after = a1.rank_table(tampered, volumes, as_of, PARAMS)
    pd.testing.assert_frame_equal(before, after)


# --- 2. score offsets -------------------------------------------------------

def test_score_is_close_at_minus_21_over_close_at_minus_252():
    """Catches: an off-by-one in either momentum offset."""
    days, (closes, volumes) = _panel()
    as_of = days[300]
    table = a1.rank_table(closes, volumes, as_of, PARAMS).set_index("symbol")
    position = list(closes.index).index(as_of)
    for symbol in SYMBOLS:
        expected = (closes.iloc[position - 21][symbol]
                    / closes.iloc[position - 252][symbol] - 1.0)
        assert table.loc[symbol, "score"] == pytest.approx(expected, rel=1e-12)


# --- 3. the five admission conditions --------------------------------------

def test_dollar_volume_floor_rejects_and_names_the_reason():
    days, (closes, volumes) = _panel()
    volumes["EEE"] = 1.0                      # ~100 USD/day, far below 1e6
    table = a1.rank_table(closes, volumes, days[300], PARAMS).set_index("symbol")
    assert not table.loc["EEE", "eligible"]
    assert table.loc["EEE", "elig_reason"] == "dollar_volume"


def test_zero_volume_share_rejects_at_more_than_one_percent():
    days, (closes, volumes) = _panel()
    column = volumes["DDD"].to_numpy().copy()
    column[297:301] = 0.0                      # 4 of the 252 ending at day 300
    volumes["DDD"] = column
    table = a1.rank_table(closes, volumes, days[300], PARAMS).set_index("symbol")
    assert not table.loc["DDD", "eligible"]
    assert table.loc["DDD", "elig_reason"] == "zero_volume"


def test_history_boundary_is_at_min_history_bars():
    """299 bars fails, 300 passes: the threshold direction and the >= sign."""
    days, (closes, volumes) = _panel()
    short = closes.copy()
    short.loc[short.index[:len(days) - 299], "CCC"] = float("nan")
    table = a1.rank_table(short, volumes, days[-1], PARAMS).set_index("symbol")
    assert table.loc["CCC", "elig_reason"] == "history"
    longer = closes.copy()
    longer.loc[longer.index[:len(days) - 300], "CCC"] = float("nan")
    table = a1.rank_table(longer, volumes, days[-1], PARAMS).set_index("symbol")
    assert table.loc["CCC", "elig_reason"] != "history"


def test_participation_uses_the_order_size_over_median_dollar_volume():
    days, (closes, volumes) = _panel()
    params = dict(PARAMS, min_dollar_volume_usd=1.0,
                  order_usd_for_participation=1e12)
    table = a1.rank_table(closes, volumes, days[300], params).set_index("symbol")
    assert set(table["elig_reason"]) == {"participation"}


def test_e5_rejects_a_candidate_with_no_verified_ticker():
    """Catches: E5 missing, which would abort the session at order time."""
    days, (closes, volumes) = _panel()
    tickers = {s: f"{s}_US_EQ" for s in SYMBOLS}
    tickers["BBB"] = None
    params = dict(PARAMS, require_verified_ticker=True,
                  verified_tickers=tickers)
    table = a1.rank_table(closes, volumes, days[300], params).set_index("symbol")
    assert not table.loc["BBB", "eligible"]
    assert table.loc["BBB", "elig_reason"] == "no_ticker"
    assert table.loc["AAA", "eligible"]


def test_e5_without_a_ticker_map_fails_closed():
    days, (closes, volumes) = _panel()
    params = dict(PARAMS, require_verified_ticker=True)
    with pytest.raises(ValueError, match="verified_tickers"):
        a1.rank_table(closes, volumes, days[300], params)


# --- 4. buffer band ---------------------------------------------------------

def test_buffer_band_keeps_inside_2n_drops_outside_and_fills_from_the_top():
    """Catches: the section 6 semantics, in all three of its parts."""
    frame = pd.DataFrame({
        "symbol": [f"S{i}" for i in range(1, 51)],
        "rank": list(range(1, 51)),
        "score": [50 - i for i in range(50)],
        "eligible": [True] * 50,
        "elig_reason": ["ok"] * 50,
        "ticker": [None] * 50, "close": [10.0] * 50})
    params = dict(PARAMS, n_hold=20, band_multiple=2)
    book = {"S30": 0.05, "S41": 0.05, "S2": 0.05}
    pick = a1.select(frame, book, params)
    assert "S30" in pick                       # rank 30 is inside the top 40
    assert "S41" not in pick                   # rank 41 is outside
    assert pick[:2] == ["S30", "S2"]           # kept names keep the book order
    # The 18 vacancies are filled in rank order from the names not kept, so
    # they are S1 and then S3 upward -- S2 is already kept and is skipped.
    assert pick[2:] == ["S1"] + [f"S{i}" for i in range(3, 20)]
    assert len(pick) == 20


# --- 5. rebalance calendar --------------------------------------------------

def test_rebalance_lands_on_every_21st_session_from_the_anchor():
    """Catches: an off-by-one in the rotation counter."""
    days = sessions("2020-01-02", 60)
    flags = [a1._rebalance_today(days, d, 21)[0] for d in days]
    assert [i for i, on in enumerate(flags) if on] == [0, 21, 42]
    assert not flags[22]


def test_a_session_missing_from_the_list_is_not_a_rebalance():
    days = sessions("2020-01-02", 60)
    import datetime
    assert a1._rebalance_today(days, datetime.date(2019, 1, 1), 21) == (False, -1)


# --- 6. names without a price ----------------------------------------------

def test_a_picked_name_without_a_price_idles_and_keeps_its_weight():
    """Catches: redistributing the weight, which would move every other leg."""
    prices = {"AAA": Decimal("100"), "BBB": Decimal("50")}
    out = a1.size(["AAA", "BBB", "CCC"], Decimal("3000"), Decimal("1.25"),
                  prices, PARAMS)
    assert "CCC" not in out
    # AAA still gets one third of the equity, not one half. 3000 / 3 * 0.99 *
    # 1.25 / 100 is 12.375 in exact arithmetic and 12.37499... through the
    # float weight the reference implementation also uses, so the floor to the
    # 0.0001 step lands on 12.3749; one half would be 18.5624.
    assert out["AAA"] == Decimal("12.3749")
    assert out["BBB"] == Decimal("24.7499")


# --- 7 and 8. ordering, empty results --------------------------------------

def test_every_zero_target_precedes_every_positive_target():
    """Catches: buys ahead of sells, which starves the cash check."""
    days, (closes, volumes) = _panel()
    injection = {"panel": {"closes": closes, "volumes": volumes},
                 "sessions": list(closes.index), "a1_book": {}, "thin": []}
    strategy = a1.make_strategy(injection)
    portfolio = FakePortfolio(cash_gbp=Decimal("1000"),
                              positions={"ZZZ": Decimal("5"),
                                         "EEE": Decimal("2")})
    params = dict(PARAMS, live_from=str(closes.index[300]),
                  rebalance_anchor=str(closes.index[300]))
    view = _view(days, closes, 300)
    view._history["ZZZ"] = [FakeBar(ts=view.now, close=10.0)]
    injection["sessions"] = list(closes.index)[300:]
    strategy = a1.make_strategy(injection)
    targets = strategy(view, portfolio, params)
    values = list(targets.values())
    first_buy = next(i for i, q in enumerate(values) if q > 0)
    assert all(q == 0 for q in values[:first_buy])
    assert targets["ZZZ"] == Decimal("0")


def test_non_rebalance_sessions_return_nothing():
    days, (closes, volumes) = _panel()
    all_sessions = list(closes.index)[300:]
    injection = {"panel": {"closes": closes, "volumes": volumes},
                 "sessions": all_sessions, "a1_book": {}, "thin": []}
    params = dict(PARAMS, live_from=str(all_sessions[0]),
                  rebalance_anchor=str(all_sessions[0]))
    strategy = a1.make_strategy(injection)
    portfolio = FakePortfolio(cash_gbp=Decimal("1000"))
    assert strategy(_view(days, closes, 301), portfolio, params) == {}


# --- 9. the book comes from the injection, not from positions --------------

def test_the_band_reads_the_injected_book_and_not_the_positions():
    """Catches: deriving the previous book from holdings (decision A12)."""
    frame = pd.DataFrame({
        "symbol": [f"S{i}" for i in range(1, 51)],
        "rank": list(range(1, 51)), "score": [50 - i for i in range(50)],
        "eligible": [True] * 50, "elig_reason": ["ok"] * 50,
        "ticker": [None] * 50, "close": [10.0] * 50})
    params = dict(PARAMS, n_hold=2, band_multiple=2)
    assert a1.select(frame, {}, params) == ["S1", "S2"]
    assert a1.select(frame, {"S4": 0.5}, params) == ["S4", "S1"]


# --- 10. the rank table may not be newer than the decision -----------------

def test_a_ranking_from_the_future_is_refused():
    table = pd.DataFrame({"symbol": ["AAA"], "rank": [1], "score": [0.5],
                          "eligible": [True], "elig_reason": ["ok"],
                          "ticker": ["AAA_US_EQ"], "close": [10.0]})
    injection = {"a1_rank": table, "rank_as_of": "2026-09-05",
                 "sessions": [], "a1_book": {}, "thin": []}
    with pytest.raises(ValueError, match="after the decision day"):
        a1._ranked_for(injection, pd.Timestamp("2026-09-02").date(), PARAMS)


def test_exactly_one_injection_shape_is_accepted():
    with pytest.raises(ValueError, match="neither"):
        a1._ranked_for({"sessions": []}, pd.Timestamp("2026-09-02").date(),
                       PARAMS)
    both = {"sessions": [], "panel": {"closes": None, "volumes": None},
            "a1_rank": pd.DataFrame()}
    with pytest.raises(ValueError, match="exactly one"):
        a1._ranked_for(both, pd.Timestamp("2026-09-02").date(), PARAMS)


def test_a_frozen_leg_holds_its_book_through_a_rebalance_session():
    """Catches: rotating on a stale ranking, or selling out on a missing one."""
    days, (closes, volumes) = _panel()
    all_sessions = list(closes.index)[300:]
    injection = {"a1_rank": None, "rank_as_of": None, "a1_frozen": True,
                 "sessions": all_sessions, "a1_book": {"AAA": 1.0},
                 "thin": []}
    params = dict(PARAMS, live_from=str(all_sessions[0]),
                  rebalance_anchor=str(all_sessions[0]))
    strategy = a1.make_strategy(injection)
    portfolio = FakePortfolio(cash_gbp=Decimal("1000"),
                              positions={"AAA": Decimal("3")})
    assert strategy(_view(days, closes, 300), portfolio, params) == {}
    tree = a1.signal_diagnostics(_view(days, closes, 300), portfolio, params,
                                 injection)
    assert tree["rebalance"]["frozen"] is True
    assert tree["eligible_count"] == 0
    assert [row["symbol"] for row in tree["book"]] == ["AAA"]


# --- 11. identity -----------------------------------------------------------

def test_the_loader_refuses_a_tampered_identity(tmp_path, monkeypatch):
    """Catches: a module attributing orders to the wrong logic version."""
    from trading212.execution import strategy_loader
    source = strategy_loader.strategy_path("a1", "0.0.1").read_text()
    target = tmp_path / "strategy" / "a1_v0_0_1.py"
    target.parent.mkdir(parents=True)
    target.write_text(source.replace('STRATEGY_NAME = "a1"',
                                     'STRATEGY_NAME = "a1x"', 1))
    monkeypatch.setattr(strategy_loader, "venue_dir", lambda venue: tmp_path)
    with pytest.raises(ValueError, match="identity"):
        strategy_loader.load_module("a1", "0.0.1")


# --- 12. diagnostics --------------------------------------------------------

def test_diagnostics_carry_the_frozen_subtree():
    days, (closes, volumes) = _panel()
    all_sessions = list(closes.index)[300:]
    injection = {"panel": {"closes": closes, "volumes": volumes},
                 "sessions": all_sessions, "a1_book": {"AAA": 0.5},
                 "thin": [], "rank_as_of": str(all_sessions[0]),
                 "rank_stale_sessions": 0}
    params = dict(PARAMS, live_from=str(all_sessions[0]),
                  rebalance_anchor=str(all_sessions[0]))
    portfolio = FakePortfolio(cash_gbp=Decimal("1000"),
                              positions={"AAA": Decimal("1")})
    tree = a1.signal_diagnostics(_view(days, closes, 300), portfolio, params,
                                 injection)
    assert set(tree) == {"rebalance", "eligible_count", "book", "next_in",
                         "band_edge"}
    assert set(tree["rebalance"]) == {
        "anchor", "session_index", "every", "sessions_until_next",
        "last_rebalance", "rank_as_of", "rank_stale_sessions", "frozen"}
    assert tree["rebalance"]["session_index"] == 0
    for row in tree["book"]:
        assert set(row) == {"symbol", "rank", "score", "weight", "status"}
        assert row["status"] in {"held", "held_in_band", "entering",
                                 "exiting", "frozen"}


def test_diagnostics_report_a_thin_name_as_frozen():
    days, (closes, volumes) = _panel()
    all_sessions = list(closes.index)[300:]
    injection = {"panel": {"closes": closes, "volumes": volumes},
                 "sessions": all_sessions, "a1_book": {}, "thin": ["AAA"],
                 "rank_as_of": str(all_sessions[0]), "rank_stale_sessions": 0}
    params = dict(PARAMS, live_from=str(all_sessions[0]),
                  rebalance_anchor=str(all_sessions[0]))
    tree = a1.signal_diagnostics(_view(days, closes, 300),
                                 FakePortfolio(cash_gbp=Decimal("1000")),
                                 params, injection)
    statuses = {row["symbol"]: row["status"] for row in tree["book"]}
    assert statuses.get("AAA") == "frozen"
