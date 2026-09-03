"""Execution-layer changes for B0: seams, gate rules, records, adoption.

Each test names the defect it catches; the list is
fixplans/t212/b0/04_execution.md section 11.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from trading212 import archive
from trading212.execution import (instruments, ledger_store, market_data,
                                  order_router, risk_gate, session_cycle)
from trading212.execution.risk_gate import OrderIntent
from trading212.execution.strategy_loader import load_module
from trading212.execution.shadow_ledger import (LedgerPortfolioView,
                                                ShadowLedger)

NY = "America/New_York"


def _cfg(name: str = "b0", **execution):
    base = {"strategy": {"name": name, "version": "0.0.1",
                         "intraday_name": name, "intraday_version": "0.0.1"}}
    if name == "b0":
        base["b0_live_from"] = "2026-09-15"
    base.update(execution)
    return {"_env": "paper", "execution": base}


# --- 1. seam S1 -------------------------------------------------------------

def test_a0_params_are_byte_identical_to_the_pre_seam_behaviour():
    """Catches: the seam quietly changing what A0 has always been given."""
    import yaml

    from common.paths import config_dir
    params = session_cycle.assemble_params(_cfg("a0"))
    expected = yaml.safe_load(
        (config_dir("t212") / "strategies" / "a0_v0_0_1.yaml").read_text())
    expected.update(session_cycle.DECISION_PARAM_OVERRIDES)
    assert params == expected


def test_b0_params_carry_three_layers_with_the_start_date_pushed_down():
    """Catches: A0 trading before B0 started, or A1 anchored on another date."""
    params = session_cycle.assemble_params(_cfg("b0"))
    assert params["live_from"] == "2026-09-15"
    assert params["a0_params"]["live_from"] == "2026-09-15"
    assert params["a1_params"]["live_from"] == "2026-09-15"
    assert params["a1_params"]["rebalance_anchor"] == "2026-09-15"
    # The yaml's own dates must NOT survive: A0 ships 2018-01-01, which would
    # let it trade inside the merged book years before B0 existed.
    assert params["a0_params"]["live_from"] != "2018-01-01"
    assert params["trade_symbols"] == params["a0_params"]["trade_symbols"]
    assert params["state_symbol"] == params["a0_params"]["state_symbol"]
    for nested in ("a0_params", "a1_params"):
        for key in session_cycle.DECISION_PARAM_OVERRIDES:
            assert key in params[nested]


def test_b0_without_a_start_date_is_refused():
    cfg = _cfg("b0")
    cfg["execution"].pop("b0_live_from")
    with pytest.raises(ValueError, match="b0_live_from"):
        session_cycle.assemble_params(cfg)


# --- 2 and 3. seams S2 and S3 ----------------------------------------------

def test_the_session_list_counts_half_days(monkeypatch):
    """Catches: dropping a half day, which shifts every later rotation."""
    stamps = [pd.Timestamp(f"{d} 00:00", tz=NY).tz_convert("UTC")
              for d in ("2026-11-25", "2026-11-27", "2026-11-30")]
    frame = pd.DataFrame({"ts": stamps, "open": 1.0, "high": 1.0, "low": 1.0,
                          "close": 1.0, "volume": 1.0, "quote_ccy": "USD"})
    monkeypatch.setattr(market_data, "load_frames",
                        lambda symbols, interval, start, end: {"SPY": frame})
    days = market_data.us_sessions("2026-11-01", "2026-12-01")
    assert [str(d) for d in days] == ["2026-11-25", "2026-11-27", "2026-11-30"]


def test_the_injection_touches_no_network_and_takes_no_lock(monkeypatch,
                                                            tmp_path):
    """Catches: the dashboard triggering a refresh or blocking a decision."""
    def explode(*args, **kwargs):
        raise AssertionError("load_b0_injection must not refresh or lock")

    monkeypatch.setattr(market_data, "refresh_bars", explode)
    monkeypatch.setattr(market_data, "fetch_interval", explode)
    monkeypatch.setattr(market_data, "refresh_for_decision", explode)
    monkeypatch.setattr(market_data, "us_sessions",
                        lambda start, end: [pd.Timestamp("2026-09-15").date()])
    monkeypatch.setattr(market_data, "daily_rows",
                        lambda symbols, start, end: {s: [] for s in symbols})
    monkeypatch.setattr(market_data, "a1_rank_path",
                        lambda day: tmp_path / f"{day}.parquet")
    params = session_cycle.assemble_params(_cfg("b0"))
    out = market_data.load_b0_injection(params,
                                        pd.Timestamp("2026-09-15").date(),
                                        held=["NVDA"], records_root=tmp_path)
    assert out["a1_rank"] is None
    assert out["a1_frozen"] is True          # no table at all
    assert out["a0_mode"] == "rows"
    assert "NVDA" in out["view_symbols"]


def test_a_stale_ranking_falls_back_and_freezes_past_the_limit(monkeypatch,
                                                               tmp_path):
    """Catches: rotating on a ranking days older than the rotation itself."""
    days = [pd.Timestamp(f"2026-09-{d:02d}").date()
            for d in (1, 2, 3, 4, 5, 8, 9)]
    monkeypatch.setattr(market_data, "us_sessions",
                        lambda start, end: [d for d in days
                                            if str(d) <= str(end)])
    monkeypatch.setattr(market_data, "daily_rows",
                        lambda symbols, start, end: {s: [] for s in symbols})
    monkeypatch.setattr(market_data, "a1_rank_path",
                        lambda day: tmp_path / f"{day}.parquet")
    pd.DataFrame({"symbol": ["AAA"], "ticker": ["AAA_US_EQ"],
                  "close": [10.0], "score": [0.5], "eligible": [True],
                  "elig_reason": ["ok"], "rank": [1]}
                 ).to_parquet(tmp_path / "2026-09-02.parquet")
    params = session_cycle.assemble_params(_cfg("b0"))

    near = market_data.load_b0_injection(params, days[2], records_root=tmp_path)
    assert near["rank_as_of"] == days[1] and near["rank_stale_sessions"] == 0
    assert near["a1_frozen"] is False

    far = market_data.load_b0_injection(params, days[6], records_root=tmp_path)
    assert far["rank_as_of"] == days[1]
    assert far["rank_stale_sessions"] > market_data.RANK_STALE_FREEZE_SESSIONS
    assert far["a1_frozen"] is True


def test_calendar_day_staleness_freezes_even_when_the_session_count_cannot(
        monkeypatch, tmp_path):
    """Catches: the circular guard that measured staleness on the same stalled
    series that produced the stale table, so it read zero and never fired."""
    stalled = [pd.Timestamp("2026-08-31").date()]
    monkeypatch.setattr(market_data, "us_sessions", lambda start, end: stalled)
    monkeypatch.setattr(market_data, "daily_rows",
                        lambda symbols, start, end: {s: [] for s in symbols})
    monkeypatch.setattr(market_data, "a1_rank_path",
                        lambda day: tmp_path / f"{day}.parquet")
    pd.DataFrame({"symbol": ["AAA"], "ticker": ["AAA_US_EQ"], "close": [10.0],
                  "score": [0.5], "eligible": [True], "elig_reason": ["ok"],
                  "rank": [1]}).to_parquet(tmp_path / "2026-08-31.parquet")
    params = session_cycle.assemble_params(_cfg("b0"))
    out = market_data.load_b0_injection(params,
                                        pd.Timestamp("2026-09-30").date(),
                                        records_root=tmp_path)
    # The session count cannot see the gap: the list itself stopped moving.
    assert out["rank_stale_sessions"] == 0
    assert out["rank_stale_days"] > market_data.RANK_STALE_FREEZE_DAYS
    assert out["a1_frozen"] is True
    assert out["session_calendar_stale"] is True


def test_the_prospective_pick_is_in_the_view_symbols(monkeypatch, tmp_path):
    """Catches the first-night blocker: the names about to be BOUGHT were
    absent from the view, priced at zero, and therefore targeted to zero."""
    prev = pd.Timestamp("2026-09-14").date()
    day = pd.Timestamp("2026-09-15").date()
    monkeypatch.setattr(market_data, "us_sessions",
                        lambda start, end: [prev, day])
    monkeypatch.setattr(market_data, "daily_rows",
                        lambda symbols, start, end: {s: [] for s in symbols})
    monkeypatch.setattr(market_data, "a1_rank_path",
                        lambda d: tmp_path / f"{d}.parquet")
    picks = [f"NEW{i}" for i in range(1, 4)]
    pd.DataFrame({"symbol": picks, "ticker": [f"{p}_US_EQ" for p in picks],
                  "close": [10.0] * 3, "score": [0.9, 0.8, 0.7],
                  "eligible": [True] * 3, "elig_reason": ["ok"] * 3,
                  "rank": [1, 2, 3]}).to_parquet(tmp_path / f"{prev}.parquet")
    params = session_cycle.assemble_params(_cfg("b0"))
    params["a1_params"] = dict(params["a1_params"], n_hold=3)
    # No previous rotation: the book is empty, so ONLY the prospective pick
    # can put these names in the view.
    universe = market_data.a1_universe_for(params, day, records_root=tmp_path)
    assert universe["book"] == {}
    assert set(universe["pick"]) == set(picks)
    out = market_data.load_b0_injection(params, day, records_root=tmp_path)
    assert set(picks) <= set(out["view_symbols"])
    assert set(picks) <= set(out["a1_names"])


def test_the_decision_session_is_appended_when_its_own_bar_is_unwritten(
        monkeypatch, tmp_path):
    """Catches: refusing to decide because today's SPY bar does not exist yet.

    decide runs at 15:30, inside the session; SPY's daily bar for that session
    is published after the close. Without the append every session would read
    as "not a session" and abort.
    """
    stored = [pd.Timestamp("2026-09-14").date()]
    monkeypatch.setattr(market_data, "us_sessions", lambda start, end: stored)
    monkeypatch.setattr(market_data, "daily_rows",
                        lambda symbols, start, end: {s: [] for s in symbols})
    monkeypatch.setattr(market_data, "a1_rank_path",
                        lambda day: tmp_path / f"{day}.parquet")
    params = session_cycle.assemble_params(_cfg("b0"))
    today = pd.Timestamp("2026-09-15").date()
    out = market_data.load_b0_injection(params, today, records_root=tmp_path)
    assert out["sessions"][-1] == today


def test_the_previous_book_comes_from_the_record_stream(tmp_path):
    """Catches: rebuilding the buffer band from positions (decision A12)."""
    assert market_data.a1_book_from_records(tmp_path) == {}
    archive.record_a1_plan(tmp_path, {
        "rebalance_date": "2026-09-15",
        "book": [{"symbol": "PLTR", "weight": 0.05},
                 {"symbol": "MU", "weight": 0.05}]})
    assert market_data.a1_book_from_records(tmp_path) == {"PLTR": 0.05,
                                                          "MU": 0.05}
    # Keyed by rebalance_date: replaying a session must not duplicate the row.
    archive.record_a1_plan(tmp_path, {"rebalance_date": "2026-09-15",
                                      "book": []})
    assert len(archive.read_stream(tmp_path, "a1_plan", limit=10)) == 1


# --- 4. seam S4 -------------------------------------------------------------

def test_a_short_window_refresh_does_not_truncate_the_month(tmp_path,
                                                            monkeypatch):
    """Catches: write_intraday replacing a full month with seven days of it."""
    from common.paths import equity_interval_dir

    monkeypatch.setattr(market_data, "group_for", lambda symbol: "us_equity")
    monkeypatch.setattr("common.paths.DIR_DATA", tmp_path)
    folder = equity_interval_dir("us_equity", "AAA", "1h")
    folder.mkdir(parents=True)
    month = pd.date_range("2026-09-01 13:30", periods=100, freq="1h", tz="UTC")
    pd.DataFrame({"ts": month, "open": 1.0, "high": 1.0, "low": 1.0,
                  "close": 1.0, "volume": 1.0, "quote_ccy": "USD"}
                 ).to_parquet(folder / "AAA_20260901_20260905_1h.parquet")
    tail = month[-3:]
    fresh = pd.DataFrame({"ts": tail, "open": 2.0, "high": 2.0, "low": 2.0,
                          "close": 2.0, "volume": 2.0, "quote_ccy": "USD"})
    market_data._write_intraday_merged("us_equity", "AAA", "1h", fresh)
    stored = pd.concat([pd.read_parquet(p) for p in folder.glob("*.parquet")])
    assert len(stored) == 100                    # nothing lost
    assert float(stored.sort_values("ts")["close"].iloc[-1]) == 2.0  # newest wins


def test_a_name_that_will_not_refresh_is_reported_thin_not_fatal(monkeypatch):
    """Catches: one slow name aborting a session that could still be decided."""
    monkeypatch.setattr(market_data, "refresh_bars",
                        lambda symbols, interval: {})
    monkeypatch.setattr(market_data, "_refresh_one_short",
                        lambda symbol, interval, days: 0)
    monkeypatch.setattr(market_data, "A1_REFRESH_BACKOFF_SEC", 0)
    params = {"trade_symbols": ["NVDA"], "state_symbol": "QQQ",
              "fx_symbol": "GBPUSD=X"}
    thin = market_data.refresh_for_decision(params, None, None, ["ZZZ", "YYY"])
    assert thin == ["ZZZ", "YYY"]


def test_the_refresh_budget_reports_the_rest_thin(monkeypatch):
    """Catches: an unbounded loop walking past the submission instant."""
    monkeypatch.setattr(market_data, "refresh_bars",
                        lambda symbols, interval: {})
    monkeypatch.setattr(market_data, "A1_REFRESH_BUDGET_SEC", -1)
    params = {"trade_symbols": [], "state_symbol": "QQQ",
              "fx_symbol": "GBPUSD=X"}
    assert market_data.refresh_for_decision(params, None, None,
                                            ["A", "B"]) == ["A", "B"]


def test_an_a0_name_missing_its_information_bar_still_stops_the_session():
    """Catches: relaxing the freshness gate for the caliber names too."""
    key = pd.Timestamp("2026-09-15 19:30", tz="UTC")
    full = pd.DataFrame({"ts": [key - pd.Timedelta(hours=1), key]})
    thin_frame = pd.DataFrame({"ts": [key]})
    fx = pd.DataFrame({"ts": [key - pd.Timedelta(minutes=90),
                              key - pd.Timedelta(minutes=30)]})
    frames = {"NVDA": thin_frame, "QQQ": full, "GBPUSD=X": fx, "ZZZ": thin_frame}
    with pytest.raises(RuntimeError, match="information bar"):
        market_data.assert_intraday_ready(frames, key, ["NVDA"], "QQQ",
                                          "GBPUSD=X")
    frames["NVDA"] = full
    thin = market_data.assert_intraday_ready(frames, key, ["NVDA"], "QQQ",
                                             "GBPUSD=X", soft_symbols=["ZZZ"])
    assert thin == ["ZZZ"]


# --- 5. intent order, residual, sell exemption, throughput -----------------

def _intent(symbol: str, qty: str, price: str = "100") -> OrderIntent:
    return OrderIntent(intent_id=f"i-{symbol}", symbol=symbol,
                       ticker=f"{symbol}_US_EQ", quantity=Decimal(qty),
                       ref_price_usd=Decimal(price),
                       fx_usd_per_gbp=Decimal("1.25"))


def test_a_buy_ahead_of_a_sell_is_reported_and_not_reordered():
    good = [_intent("A", "-1"), _intent("B", "1")]
    bad = [_intent("B", "1"), _intent("A", "-1")]
    assert session_cycle._warn_if_buys_precede_sells(good) is False
    assert session_cycle._warn_if_buys_precede_sells(bad) is True


def test_a_sell_leaving_dust_is_enlarged_to_a_full_exit():
    """Catches: stranding a stub that no future order could ever clear."""
    view = LedgerPortfolioView(cash_gbp=Decimal("0"),
                               available_cash_gbp=Decimal("0"),
                               positions={"A": Decimal("1")},
                               pending_signed_qty={})
    # Position is worth GBP 80; selling 0.99 leaves GBP 0.80, under the floor.
    verdict = risk_gate._check_one(_intent("A", "-0.99"), view,
                                   Decimal("1000"), Decimal("1"))
    assert verdict.quantity == Decimal("-1")


def test_the_per_order_ceiling_binds_buys_but_never_sells():
    """Catches: a grown position that can never be exited."""
    view = LedgerPortfolioView(cash_gbp=Decimal("0"),
                               available_cash_gbp=Decimal("0"),
                               positions={"A": Decimal("5")},
                               pending_signed_qty={})
    buy = risk_gate._check_one(_intent("A", "5"), view, Decimal("10"),
                               Decimal("1"))
    assert isinstance(buy, str) and "max_order_notional_gbp" in buy
    sell = risk_gate._check_one(_intent("A", "-5"), view, Decimal("10"),
                                Decimal("1"))
    assert not isinstance(sell, str)
    assert sell.quantity == Decimal("-5")


def test_capacity_and_time_needed_are_reported_against_the_close():
    """Catches: a partial batch that sends every sell and no buy.

    Ordering puts reductions first, so truncation liquidates without
    re-entering. The caller abandons the batch instead; this checks the
    arithmetic it decides on.
    """
    class _Tight:
        close_utc = pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=40)

    intents = [_intent(f"S{i}", "1") for i in range(20)]
    capacity, needed = session_cycle._fit_before_close(intents, _Tight())
    assert capacity < len(intents)          # does not fit -> caller aborts
    assert needed == pytest.approx(20 / (50 / 60 * 0.7), rel=1e-6)

    class _Roomy:
        close_utc = pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=1)

    capacity, _needed = session_cycle._fit_before_close(intents, _Roomy())
    assert capacity >= len(intents)


# --- 6 and 7. mapping and precision ---------------------------------------

def test_the_learned_step_survives_to_the_next_session(tmp_path):
    """Catches: paying the same precision rejection every session."""
    risk_gate.load_qty_steps(tmp_path)
    assert risk_gate.qty_step("INTC") == Decimal("0.001")
    assert risk_gate.qty_step("NVDA") == risk_gate.T212_QTY_STEP
    risk_gate.record_qty_step(tmp_path, "NVDA", Decimal("0.01"))
    assert json.loads((tmp_path / "qty_steps.json").read_text())["NVDA"] \
        == "0.01"
    risk_gate.QTY_STEP_OVERRIDES.clear()
    risk_gate.load_qty_steps(tmp_path)           # a fresh session
    assert risk_gate.qty_step("NVDA") == Decimal("0.01")
    # Widening only: a finer reading is not evidence that the venue changed.
    risk_gate.record_qty_step(tmp_path, "NVDA", Decimal("0.0001"))
    assert risk_gate.qty_step("NVDA") == Decimal("0.01")
    risk_gate.load_qty_steps(Path(tmp_path) / "absent")


def test_a_precision_rejection_is_parsed_into_a_step(tmp_path):
    from common.net import PermanentError

    class _Rejected(PermanentError):
        def __init__(self) -> None:
            super().__init__("rejected")
            self.detail = ('{"code":"quantity-precision-mismatch",'
                           '"context":{"maxDecimalPlaces":3}}')

    learned = order_router._learn_quantity_step(tmp_path, _intent("MU", "1"),
                                                _Rejected())
    assert learned == Decimal("0.001")
    other = order_router._learn_quantity_step(tmp_path, _intent("MU", "1"),
                                              PermanentError("no funds"))
    assert other is None
    risk_gate.QTY_STEP_OVERRIDES.clear()
    risk_gate.QTY_STEP_OVERRIDES.update(
        {s: Decimal(v) for s, v in risk_gate.QTY_STEP_DEFAULTS.items()})


def test_only_the_divergent_schedule_ids_come_back():
    def _schedule(schedule_id: int, close: str) -> dict:
        return {"id": schedule_id, "timeEvents": [
            {"date": "2026-09-15T13:30:00Z", "type": "OPEN"},
            {"date": f"2026-09-15T{close}Z", "type": "AFTER_HOURS_OPEN"}]}

    calendar = [{"id": 53, "name": "NASDAQ",
                 "workingSchedules": [_schedule(71, "20:00:00"),
                                      _schedule(72, "20:00:00"),
                                      _schedule(99, "17:00:00")]}]
    day = pd.Timestamp("2026-09-15").date()
    assert instruments.divergent_schedule_ids(calendar, {71, 72}, day) == set()
    assert instruments.divergent_schedule_ids(calendar, {71, 99}, day) == {99}


# --- 8. record streams ------------------------------------------------------

def test_the_allocation_row_carries_every_contracted_field(tmp_path):
    archive.record_b0_allocation(tmp_path, {
        "decision_date": "2026-09-15", "equity_gbp": 1234.56,
        "priority": "a1", "a0_names": ["NVDA"], "a1_names": ["PLTR"],
        "overlap": [], "a0_target_gbp": 137.2, "a1_target_gbp": 1085.0,
        "cash_target_gbp": 12.3, "attribution": {"NVDA": "a0"},
        "a0_value_gbp": 68.6, "a1_value_gbp": 1150.2, "cash_gbp": 15.8})
    rows = archive.read_stream(tmp_path, "b0_allocation", limit=5)
    assert len(rows) == 1
    assert set(rows[0]) >= {
        "decision_date", "equity_gbp", "priority", "a0_names", "a1_names",
        "overlap", "a0_target_gbp", "a1_target_gbp", "cash_target_gbp",
        "attribution", "a0_value_gbp", "a1_value_gbp", "cash_gbp"}
    archive.record_b0_allocation(tmp_path, {"decision_date": "2026-09-15"})
    assert len(archive.read_stream(tmp_path, "b0_allocation", limit=5)) == 1


def test_both_new_streams_are_declared():
    names = [name for name, _key in archive.STREAMS]
    assert "a1_plan" in names and "b0_allocation" in names


# --- 9. book adoption -------------------------------------------------------

def _a0_book(tmp_path) -> ShadowLedger:
    ledger = ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("500"))
    ledger.record_intent("i1", "NVDA", "NVDA_US_EQ", Decimal("2"),
                         Decimal("175"), Decimal("1.35"), dry_run=False)
    ledger.record_submitted("i1", 111, "NVDA", "NVDA_US_EQ", Decimal("2"),
                            Decimal("259.26"), "NEW")
    ledger.record_fill(111, 9001, Decimal("2"), Decimal("175.50"),
                       Decimal("-260.00"), [], "2026-08-20T13:30:01Z")
    ledger.record_order_terminal(111, "FILLED", Decimal("2"))
    return ledger


def test_adoption_moves_cash_and_positions_exactly(tmp_path):
    """Catches: a new book that believes it holds nothing while shares exist."""
    source = _a0_book(tmp_path)
    adopted = ShadowLedger.init_adopted(tmp_path, "b0_v0_0_1", source)
    assert adopted.cash_gbp == source.cash_gbp
    assert dict(adopted.positions) == dict(source.positions)
    reloaded = ShadowLedger.load(tmp_path, "b0_v0_0_1")
    assert reloaded.cash_gbp == source.cash_gbp
    assert dict(reloaded.positions) == dict(source.positions)


def test_adoption_refuses_a_source_with_open_orders(tmp_path):
    source = ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("500"))
    source.record_intent("i1", "NVDA", "NVDA_US_EQ", Decimal("2"),
                         Decimal("175"), Decimal("1.35"), dry_run=False)
    source.record_submitted("i1", 111, "NVDA", "NVDA_US_EQ", Decimal("2"),
                            Decimal("259.26"), "NEW")
    with pytest.raises(ValueError, match="open orders"):
        ShadowLedger.init_adopted(tmp_path, "b0_v0_0_1", source)


def test_adoption_refuses_to_overwrite_an_existing_book(tmp_path):
    source = _a0_book(tmp_path)
    ShadowLedger.init_fresh(tmp_path, "b0_v0_0_1", Decimal("1"))
    with pytest.raises(FileExistsError):
        ShadowLedger.init_adopted(tmp_path, "b0_v0_0_1", source)


def test_retire_and_restore_are_inverses(tmp_path):
    """Catches: a rollback that cannot actually roll back."""
    _a0_book(tmp_path)
    moved = ledger_store.retire_ledger(tmp_path, "a0_v0_0_1", "STAMP")
    assert len(moved) == 2
    assert not ledger_store.snapshot_path(tmp_path, "a0_v0_0_1").exists()
    ledger_store.restore_ledger(tmp_path, "a0_v0_0_1", "STAMP")
    assert ShadowLedger.load(tmp_path, "a0_v0_0_1").positions


def test_restore_refuses_to_clobber_a_live_book(tmp_path):
    _a0_book(tmp_path)
    ledger_store.retire_ledger(tmp_path, "a0_v0_0_1", "STAMP")
    ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("7"))
    with pytest.raises(FileExistsError):
        ledger_store.restore_ledger(tmp_path, "a0_v0_0_1", "STAMP")


def test_adopt_book_needs_the_confirm_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(session_cycle, "_Cycle", lambda cfg: _StubCycle(tmp_path))
    out = session_cycle.adopt_book({"_env": "paper"}, "a0_v0_0_1")
    assert out["aborted"] and "--confirm" in out["reason"]


class _StubCycle:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.strategy_id = "b0_v0_0_1"
        self.client = None

    def session_list(self):
        return []


# --- fixes from the 2026-09-03 independent review --------------------------

def test_gross_exposure_is_credited_with_approved_sells():
    """Catches: a rotation measured as buy-forty instead of sell-twenty
    buy-twenty, which rejects the second half of every rebalance."""
    view = LedgerPortfolioView(cash_gbp=Decimal("1000"),
                               available_cash_gbp=Decimal("1000"),
                               positions={"OLD": Decimal("10")},
                               pending_signed_qty={})
    cfg = {"max_order_notional_gbp": "10000", "max_gross_notional_gbp": "900",
           "max_daily_orders": "50", "min_order_value_gbp": "1",
           "fee_buffer": "0"}
    # Held value 800; sell it all, then buy 800 of something else. Without the
    # credit the buy is measured against 800 + 800 = 1600 and is rejected.
    sell = _intent("OLD", "-10", "100")
    buy = _intent("NEW", "8", "100")
    report = risk_gate.check_intents([sell, buy], view, Decimal("800"), cfg,
                                     orders_today=0, in_submit_window=True,
                                     halt_path=Path("/nonexistent-halt"))
    assert [i.symbol for i in report.approved] == ["OLD", "NEW"]
    assert report.rejected == []


def test_a_shut_gate_with_an_empty_book_still_liquidates():
    """Catches the A7 violation: {} reads as abort, so A0's own exit orders
    were never sent and the whole book rode through a closed gate."""
    b0 = load_module("b0", "0.0.1")
    a0_params = {"trade_symbols": ["NVDA", "AMD"], "state_symbol": "QQQ",
                 "fx_symbol": "GBPUSD=X", "signal_mode": "ma200",
                 "trend_ma": 999, "tsmom_lookback": 3, "warmup_bars": 4,
                 "use_vol_gate": False, "use_trend_gate": False,
                 "live_from": "2020-01-02", "slot_headroom": 0.99}
    a1_params = {"n_hold": 3, "band_multiple": 2, "rebalance_every": 21,
                 "mom_long": 3, "mom_skip": 1, "liq_window": 3,
                 "min_dollar_volume_usd": 1.0, "max_zero_volume_share": 0.99,
                 "min_history_bars": 1, "order_usd_for_participation": 1e-9,
                 "require_verified_ticker": False, "slot_headroom": 0.99,
                 "fx_symbol": "GBPUSD=X", "rebalance_anchor": "2020-01-02",
                 "live_from": "2020-01-02"}
    params = {"priority": "a1", "a1_band": 0.10, "slot_headroom": 0.99,
              "signal_view_cash_gbp": 1000000, "sells_first": True,
              "fx_symbol": "GBPUSD=X", "live_from": "2020-01-02",
              "a0_params": a0_params, "a1_params": a1_params}

    from tests.strategy.conftest import (FakeBar, FakePortfolio, FakeView,
                                         ny_ts, sessions)
    days = sessions("2020-01-02", 3)
    history = {s: [FakeBar(ts=ny_ts(d), close=100.0) for d in days]
               for s in ("NVDA", "AMD", "QQQ")}
    history["GBPUSD=X"] = [FakeBar(ts=ny_ts(days[-1]), close=1.25)]
    view = FakeView(history, ny_ts(days[-1]))
    portfolio = FakePortfolio(cash_gbp=Decimal("10"),
                              available_cash_gbp=Decimal("10"),
                              positions={"NVDA": Decimal("5")})
    injection = {"a1_rank": None, "rank_as_of": None, "a1_frozen": True,
                 "a1_book": {}, "sessions": list(days), "thin": [],
                 "a0_mode": "view"}
    targets = b0.make_strategy(injection)(view, portfolio, params)
    assert targets, "an empty mapping is read as an abort by decide()"
    assert targets["NVDA"] == Decimal("0")     # the exit A0 would have made


def test_init_ledger_refuses_to_orphan_venue_positions():
    """Catches: a fresh book owning none of the shares the account holds,
    which reconcile cannot see because its position check is one-way."""
    class _Client:
        def positions(self):
            return [{"ticker": "NVDA_US_EQ", "quantity": "2"}]

    class _Stub:
        def __init__(self, tmp):
            self.state_dir = Path(tmp)
            self.strategy_id = "b0_v0_0_1"
            self.client = _Client()

    import tempfile
    tmp = tempfile.mkdtemp()
    original = session_cycle._Cycle
    try:
        session_cycle._Cycle = lambda cfg: _Stub(tmp)
        out = session_cycle.init_ledger({"_env": "paper"}, Decimal("100"))
        assert out["aborted"] and "adopt-book" in out["reason"]
    finally:
        session_cycle._Cycle = original


def test_validate_mapping_is_fatal_only_for_the_required_names():
    """Catches: one stale wide-universe instrument stopping every session,
    including the sells needed to exit it."""
    class _Client:
        def instruments(self):
            return [{"ticker": "NVDA_US_EQ", "currencyCode": "USD",
                     "type": "STOCK", "shortName": "NVDA"}]

    ok = instruments.validate_mapping(_Client(), ["NVDA"], required=["NVDA"])
    assert set(ok) == {"NVDA"}
    # An unmapped wide-universe name degrades instead of raising.
    degraded = instruments.validate_mapping(_Client(),
                                            ["NVDA", "NOT_A_REAL_SYMBOL"],
                                            required=["NVDA"])
    assert set(degraded) == {"NVDA"}
    with pytest.raises(RuntimeError):
        instruments.validate_mapping(_Client(), ["NVDA", "NOT_A_REAL_SYMBOL"])


def test_the_rotation_is_recorded_before_any_abort_can_discard_it(tmp_path):
    """Catches: losing rotation zero to an abort in the 29-minute submit wait.

    The a1_plan row is the only memory of the A1 book, and the rotation
    counter is a pure function of the session list -- session zero is spent by
    being reached. Recording after submission meant a halt, a stop or a passed
    submission instant left the next session with no book, which liquidates
    the names just chosen.
    """
    class _Stub:
        is_b0 = True
        strategy_id = "b0_v0_0_1"
        signal_name = "b0"
        signal_version = "0.0.1"
        params = {"a1_params": {"universe_file": "u.json"}}

        def __init__(self, path):
            self.records_root = path
            self.state_dir = path

    cycle = _Stub(tmp_path)
    diagnostics = {"a1": {"eligible_count": 1475,
                          "rebalance": {"session_index": 0, "every": 21,
                                        "sessions_until_next": 0,
                                        "anchor": "2026-09-15",
                                        "last_rebalance": "2026-09-15",
                                        "rank_as_of": "2026-09-12",
                                        "rank_stale_sessions": 0},
                          "book": [{"symbol": "SNDK", "status": "entering"},
                                   {"symbol": "OLD", "status": "exiting"}]}}
    assert session_cycle._record_rotation(cycle, "2026-09-15", diagnostics,
                                          {"a1_book": {"OLD": 0.05}}) is True
    rows = archive.read_stream(tmp_path, "a1_plan", limit=5)
    assert len(rows) == 1
    assert [r["symbol"] for r in rows[0]["book"]] == ["SNDK"]
    assert rows[0]["dropped"] == ["OLD"] and rows[0]["added"] == ["SNDK"]
    # The rotation cache is written on every session, rebalance or not.
    cached = json.loads((tmp_path / "a1_rebalance_state.json").read_text())
    assert cached["session_index"] == 0

    quiet = {"a1": {"rebalance": {"session_index": 5, "every": 21,
                                  "sessions_until_next": 16}}}
    assert session_cycle._record_rotation(cycle, "2026-09-22", quiet,
                                          {}) is False
    assert len(archive.read_stream(tmp_path, "a1_plan", limit=5)) == 1
    assert json.loads(
        (tmp_path / "a1_rebalance_state.json").read_text())["session_index"] == 5
