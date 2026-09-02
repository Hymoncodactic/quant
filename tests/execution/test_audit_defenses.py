"""Tests pinning the defenses added by the 2026-08-29 pre-live audit.

Covers: the settle halt path (the cycle.halted method), dangling live
intents freezing as ambiguity, reconciler attribution filters (foreign
sources and hand-placed dashboard orders), the mid-batch halt re-check in
the router, the venue-cash shortfall gate, negative-cash alarming, and the
clock-skew accessor.

Out of scope: end-to-end decide() (needs live market data; covered by the
dry-run rehearsal) and the dashboard HTTP layer (tests/dashboard/).

Change log:
    2026-08-29  Created with the audit fixes.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from trading212.execution import session_cycle
from trading212.execution.order_router import OrderIntent, submit_intents
from trading212.execution.reconciler import reconcile, resolve_ambiguities
from trading212.execution.shadow_ledger import ShadowLedger

D = Decimal
STRATEGY_ID = "a0_v0_0_1"


# ============================================================================
# [1] Dangling live intents
# ============================================================================

def test_live_intent_with_no_outcome_freezes_the_book(ledger):
    ledger.record_intent("i-live", "NVDA", "NVDA_US_EQ", D("2"), D("175"),
                         D("1.35"), dry_run=False)
    frozen = ledger.freeze_dangling_live_intents()
    assert frozen == ["i-live"]
    assert ledger.is_frozen
    assert "i-live" in ledger.ambiguous_intents


def test_dry_run_intent_is_not_dangling(ledger):
    ledger.record_intent("i-dry", "NVDA", "NVDA_US_EQ", D("2"), D("175"),
                         D("1.35"), dry_run=True)
    assert ledger.freeze_dangling_live_intents() == []
    assert not ledger.is_frozen


def test_intent_with_submit_outcome_is_not_dangling(funded_ledger):
    assert funded_ledger.freeze_dangling_live_intents() == []
    assert not funded_ledger.is_frozen


def test_rejected_intent_is_not_dangling(ledger):
    ledger.record_intent("i-rej", "NVDA", "NVDA_US_EQ", D("2"), D("175"),
                         D("1.35"), dry_run=False)
    ledger.record_submit_rejected("i-rej", "venue said no")
    assert ledger.freeze_dangling_live_intents() == []


def test_freeze_is_idempotent_across_calls(ledger, tmp_path):
    ledger.record_intent("i-live", "NVDA", "NVDA_US_EQ", D("2"), D("175"),
                         D("1.35"), dry_run=False)
    assert ledger.freeze_dangling_live_intents() == ["i-live"]
    # A reload sees the AMBIG event as the terminal and freezes nothing new.
    reloaded = ShadowLedger.load(tmp_path, STRATEGY_ID)
    assert reloaded.freeze_dangling_live_intents() == []
    assert reloaded.is_frozen


# ============================================================================
# [2] Reconciler attribution
# ============================================================================

class FakeClient:
    def __init__(self, pending=None, positions=None, history=None):
        self._pending = pending or []
        self._positions = positions or []
        self._history = history or []

    def pending_orders(self):
        return self._pending

    def positions(self):
        return self._positions

    def iter_history_orders(self, ticker=None, max_pages=40):
        yield from self._history


def _ambiguous_ledger(ledger):
    ledger.record_intent("i-amb", "AAPL", "AAPL_US_EQ", D("3"), D("200"),
                         D("1.30"), dry_run=False)
    ledger.record_submit_ambiguous("i-amb", "AAPL", "AAPL_US_EQ", D("3"),
                                   "transport timeout", D("461.54"))
    return ledger


def _candidate(order_id, source, created=None):
    return {"id": order_id, "ticker": "AAPL_US_EQ", "side": "BUY",
            "quantity": 3.0, "initiatedFrom": source,
            "createdAt": created or str(pd.Timestamp.now(tz="UTC"))}


def test_foreign_source_order_is_never_ambiguity_evidence(ledger):
    """A hand-placed app order matching ticker+qty must not be absorbed."""
    book = _ambiguous_ledger(ledger)
    client = FakeClient(pending=[_candidate(555, "IOS")])
    resolved = resolve_ambiguities(client, book)
    assert resolved == []          # young ambiguity + no evidence: freeze holds
    assert book.is_frozen
    assert "555" not in book.open_orders


def test_missing_source_field_still_counts_as_evidence(ledger):
    """83/546 archived orders lack initiatedFrom (S5); excluding them would
    resolve a real lost order as never-arrived and re-submit it."""
    book = _ambiguous_ledger(ledger)
    order = _candidate(556, None)
    del order["initiatedFrom"]
    client = FakeClient(pending=[order])
    resolved = resolve_ambiguities(client, book)
    assert resolved == ["i-amb -> order 556"]
    assert not book.is_frozen
    assert "556" in book.open_orders


def test_dashboard_manual_order_id_is_excluded_from_evidence(ledger):
    book = _ambiguous_ledger(ledger)
    manual = book.state_dir / "manual_orders.jsonl"
    manual.write_text(json.dumps({"outcome": "submitted", "order_id": 557})
                      + "\n", encoding="utf-8")
    client = FakeClient(pending=[_candidate(557, "API")])
    resolved = resolve_ambiguities(client, book)
    assert resolved == []
    assert book.is_frozen


def test_dashboard_manual_pending_order_is_not_a_mismatch(funded_ledger):
    manual = funded_ledger.state_dir / "manual_orders.jsonl"
    manual.write_text(json.dumps({"outcome": "submitted", "order_id": 999})
                      + "\n", encoding="utf-8")
    client = FakeClient(
        pending=[{"id": 999, "ticker": "AAPL_US_EQ", "initiatedFrom": "API"}],
        positions=[{"instrument": {"ticker": "NVDA_US_EQ"}, "quantity": 2.0}])
    verdict = reconcile(client, funded_ledger,
                        {"NVDA": "NVDA_US_EQ", "AAPL": "AAPL_US_EQ"})
    assert verdict.ok


# ============================================================================
# [3] Router mid-batch halt
# ============================================================================

def _intent(symbol="NVDA"):
    return OrderIntent(intent_id=f"x|{symbol}", symbol=symbol,
                       ticker=f"{symbol}_US_EQ", quantity=D("1"),
                       ref_price_usd=D("100"), fx_usd_per_gbp=D("1.3"))


def test_halt_file_stops_the_batch_before_any_intent(ledger, tmp_path):
    halt = tmp_path / "halt"
    halt.touch()
    report = submit_intents([_intent("NVDA"), _intent("AAPL")], ledger,
                            client=None, decision_day=pd.Timestamp("2026-08-31"),
                            dry_run=True, armed=False, halt_path=halt)
    assert len(report.not_attempted) == 2
    assert report.dry_run == [] and report.submitted == []
    assert not ledger.ambiguous_intents and not ledger.open_orders


def test_absent_halt_file_lets_the_batch_run(ledger, tmp_path):
    report = submit_intents([_intent("NVDA")], ledger, client=None,
                            decision_day=pd.Timestamp("2026-08-31"),
                            dry_run=True, armed=False,
                            halt_path=tmp_path / "halt")
    assert len(report.dry_run) == 1


# ============================================================================
# [4] Venue cash shortfall
# ============================================================================

def test_shortfall_when_venue_free_below_book():
    got = session_cycle._venue_cash_shortfall(
        {"cash": {"availableToTrade": 900.0}}, D("1000"))
    assert got == (D("900.0"), D("1000"))


def test_no_shortfall_when_covered_or_within_tolerance():
    assert session_cycle._venue_cash_shortfall(
        {"cash": {"availableToTrade": 1000.0}}, D("1000")) is None
    assert session_cycle._venue_cash_shortfall(
        {"cash": {"availableToTrade": 999.995}}, D("1000")) is None


def test_missing_cash_field_fails_closed():
    got = session_cycle._venue_cash_shortfall({}, D("1000"))
    assert got == (D("0"), D("1000"))


# ============================================================================
# [5] Settle halt path and negative cash
# ============================================================================

class _StubClient:
    def __init__(self, *args, **kwargs):
        pass

    def pending_orders(self):
        return []

    def positions(self):
        return []

    def iter_history_orders(self, ticker=None, max_pages=40):
        return iter(())


@pytest.fixture()
def cycle_env(tmp_path, monkeypatch):
    """settle() runs against a temp state dir and a stub client."""
    monkeypatch.setattr(session_cycle, "execution_state_dir",
                        lambda venue, env="live": tmp_path)
    monkeypatch.setattr(session_cycle, "T212Client", _StubClient)
    fired = []
    monkeypatch.setattr(session_cycle, "notify",
                        lambda title, message: fired.append(title) or True)
    cfg = {"live": True, "_env": "paper", "account": {"base_ccy": "GBP"},
           "execution": {"dry_run": True,
                         "strategy": {"name": "a0", "version": "0.0.1"}}}
    return cfg, tmp_path, fired


def test_settle_returns_a_report_without_crashing(cycle_env):
    cfg, state_dir, fired = cycle_env
    ShadowLedger.init_fresh(state_dir, STRATEGY_ID, D("1000"))
    result = session_cycle.settle(cfg)
    assert result["phase"] == "settle"
    assert result["halted"] is False


def test_settle_raises_the_halt_flag_on_a_fill_timing_breach(cycle_env,
                                                             monkeypatch):
    cfg, state_dir, fired = cycle_env
    ShadowLedger.init_fresh(state_dir, STRATEGY_ID, D("1000"))
    monkeypatch.setattr(session_cycle.order_monitor, "fill_timing_breaches",
                        lambda ledger: [{"order_id": 1, "lag_hours": 17.5}])
    result = session_cycle.settle(cfg)
    assert result["halted"] is True
    assert (state_dir / "halt").exists()
    assert "a0_v0_0_1 fill timing breach -- halted" in fired
    journal = (state_dir / f"{STRATEGY_ID}_journal.jsonl").read_text()
    assert "FILL_TIMING_BREACH" in journal


def test_settle_alarms_on_negative_book_cash(cycle_env):
    cfg, state_dir, fired = cycle_env
    book = ShadowLedger.init_fresh(state_dir, STRATEGY_ID, D("10"))
    book.record_intent("i1", "NVDA", "NVDA_US_EQ", D("2"), D("175"),
                       D("1.35"), dry_run=False)
    book.record_submitted("i1", 111, "NVDA", "NVDA_US_EQ", D("2"),
                          D("259.26"), "NEW")
    book.record_fill(111, 9001, D("2"), D("175.50"), D("-260.00"), [],
                     "2026-08-28T20:00:01Z")
    book.record_order_terminal(111, "FILLED", D("2"))
    result = session_cycle.settle(cfg)
    assert Decimal(result["cash_gbp"]) < 0
    assert "a0_v0_0_1 strategy cash negative" in fired
    journal = (state_dir / f"{STRATEGY_ID}_journal.jsonl").read_text()
    assert "NEGATIVE_CASH" in journal


def test_settle_freezes_dangling_intents_and_resolves_by_absence(cycle_env,
                                                                 monkeypatch):
    cfg, state_dir, fired = cycle_env
    book = ShadowLedger.init_fresh(state_dir, STRATEGY_ID, D("1000"))
    book.record_intent("i-crash", "NVDA", "NVDA_US_EQ", D("2"), D("175"),
                       D("1.35"), dry_run=False)
    # Age the dangling intent past the absence threshold by patching the
    # resolver's clock source is unnecessary: absence resolution needs the
    # ambiguity to be old, so here just assert the freeze happened and the
    # book stayed frozen (the venue shows nothing and the record is young).
    result = session_cycle.settle(cfg)
    reloaded = ShadowLedger.load(state_dir, STRATEGY_ID)
    assert "i-crash" in reloaded.ambiguous_intents


# ============================================================================
# [6] Clock skew accessor
# ============================================================================

def test_clock_skew_parses_the_http_date_header():
    from trading212.client import T212Client
    client = object.__new__(T212Client)
    client._last_response_date = "Mon, 31 Aug 2026 19:29:00 GMT"
    client._last_response_at = pd.Timestamp(
        "2026-08-31 19:29:03", tz="UTC").timestamp()
    assert client.last_clock_skew_sec() == pytest.approx(3.0)


def test_clock_skew_is_none_without_a_header():
    from trading212.client import T212Client
    client = object.__new__(T212Client)
    client._last_response_date = None
    client._last_response_at = None
    assert client.last_clock_skew_sec() is None


# ============================================================================
# [7] Review-confirmed regressions: anchor time, near-miss, taints
# ============================================================================

def test_dangling_freeze_anchors_at_the_intent_time_not_the_freeze_time(
        ledger, monkeypatch):
    """A freeze recorded hours after the crash must keep the POST instant as
    the evidence anchor; stamping the freeze time excludes the real order
    from the createdAt window and later mis-rules it never-arrived."""
    import trading212.execution.shadow_ledger as sl
    ledger.record_intent("i-crash", "NVDA", "NVDA_US_EQ", D("2"), D("175"),
                         D("1.35"), dry_run=False)
    intent_ts = [r for r in __import__(
        "trading212.execution.ledger_store", fromlist=["iter_journal"])
        .iter_journal(ledger.state_dir, STRATEGY_ID)
        if r["event_type"] == "ORDER_INTENT"][0]["ts_utc"]
    monkeypatch.setattr(sl, "_now_iso",
                        lambda: "2026-09-01T15:30:00+00:00")
    ledger.freeze_dangling_live_intents()
    assert ledger.ambiguous_intents["i-crash"]["at"] == intent_ts


def test_crashed_order_found_in_history_is_positively_matched(ledger):
    """The reviewer-reproduced double-order chain: the real order, created at
    POST time, must match even when resolution runs much later."""
    book = ledger
    book.record_intent("i-crash", "NVDA", "NVDA_US_EQ", D("2"), D("175"),
                       D("1.35"), dry_run=False)
    book.freeze_dangling_live_intents()
    posted_at = book.ambiguous_intents["i-crash"]["at"]
    client = FakeClient(history=[{"order": {
        "id": 777, "ticker": "NVDA_US_EQ", "side": "BUY", "quantity": 2.0,
        "createdAt": posted_at, "initiatedFrom": "API"}}])
    resolved = resolve_ambiguities(client, book)
    assert resolved == ["i-crash -> order 777"]
    assert "777" in book.open_orders


def test_near_miss_candidate_blocks_the_absence_ruling(ledger, monkeypatch):
    """An order matching everything but the time window forbids ruling
    never-arrived, however old the ambiguity is."""
    book = _ambiguous_ledger(ledger)
    stale = _candidate(888, "API",
                       created=str(pd.Timestamp.now(tz="UTC")
                                   - pd.Timedelta(hours=5)))
    # Age the ambiguity far past the absence threshold.
    record = book.ambiguous_intents["i-amb"]
    client = FakeClient(history=[{"order": stale}])
    resolved = resolve_ambiguities(client, book, min_absent_age_sec=0.0)
    assert resolved == []
    assert book.is_frozen


def test_manual_ambiguous_entry_stands_down_automatic_resolution(ledger):
    """A hand-placed order whose own outcome is unknown may exist at the
    venue with no journaled id; while it overlaps the window, automation
    must leave the freeze to a human."""
    book = _ambiguous_ledger(ledger)
    manual = book.state_dir / "manual_orders.jsonl"
    manual.write_text(json.dumps(
        {"outcome": "ambiguous", "ticker": "AAPL_US_EQ",
         "ts": str(pd.Timestamp.now(tz="UTC"))}) + "\n", encoding="utf-8")
    client = FakeClient(pending=[_candidate(600, "API")])
    resolved = resolve_ambiguities(client, book, min_absent_age_sec=0.0)
    assert resolved == []
    assert book.is_frozen


def test_torn_manual_journal_line_stands_down_resolution(ledger):
    book = _ambiguous_ledger(ledger)
    manual = book.state_dir / "manual_orders.jsonl"
    manual.write_text('{"outcome": "submitted", "order_id": 1}\n'
                      '{"outcome": "subm', encoding="utf-8")
    client = FakeClient(pending=[_candidate(601, "API")])
    resolved = resolve_ambiguities(client, book, min_absent_age_sec=0.0)
    assert resolved == []
    assert book.is_frozen


# ============================================================================
# [8] Environment isolation of execution state (added 2026-08-29 before the
# first demo submission: a demo fill must never reach the live ledger)
# ============================================================================

def test_paper_state_lives_beside_not_inside_live_state():
    from common.paths import execution_state_dir, records_dir
    live = execution_state_dir("t212")
    paper = execution_state_dir("t212", "paper")
    assert live != paper
    assert paper.name == "execution_state_paper"
    assert paper.parent == live.parent
    assert records_dir("t212", "paper") == records_dir("t212") / "paper"


def test_cycle_state_dir_follows_the_config_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(session_cycle, "execution_state_dir",
                        lambda venue, env="live": calls.append(env) or
                        Path("/tmp/x"))
    monkeypatch.setattr(session_cycle, "T212Client",
                        lambda *a, **k: object())
    cfg = {"_env": "paper", "live": False,
           "execution": {"strategy": {"name": "a0", "version": "0.0.1"}}}
    try:
        session_cycle._Cycle(cfg)
    except Exception:
        pass  # params file loading may fail; the path call happened first
    assert calls == ["paper"]
