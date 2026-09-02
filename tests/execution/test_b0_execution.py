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
    pd.DataFrame({"symbol": ["AAA"]}).to_parquet(tmp_path / "2026-09-02.parquet")
    params = session_cycle.assemble_params(_cfg("b0"))

    near = market_data.load_b0_injection(params, days[2], records_root=tmp_path)
    assert near["rank_as_of"] == days[1] and near["rank_stale_sessions"] == 0
    assert near["a1_frozen"] is False

    far = market_data.load_b0_injection(params, days[6], records_root=tmp_path)
    assert far["rank_as_of"] == days[1]
    assert far["rank_stale_sessions"] > market_data.RANK_STALE_FREEZE_SESSIONS
    assert far["a1_frozen"] is True


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


def test_the_batch_is_cut_to_what_fits_before_the_close():
    """Catches: sending orders that fill at the next open instead."""
    class _Session:
        close_utc = pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=40)

    intents = [_intent(f"S{i}", "1") for i in range(20)]
    send, deferred = session_cycle._fit_before_close(intents, _Session())
    # ~0.58 orders per second over the 10 seconds left after the safety margin.
    assert 0 < len(send) < len(intents)
    assert len(send) + len(deferred) == len(intents)

    class _Roomy:
        close_utc = pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=1)

    send, deferred = session_cycle._fit_before_close(intents, _Roomy())
    assert deferred == [] and len(send) == len(intents)


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
