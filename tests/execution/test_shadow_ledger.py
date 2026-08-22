"""Discriminative tests for the event-sourced shadow ledger."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading212.execution.shadow_ledger import LedgerFrozenError, ShadowLedger

D = Decimal


def test_happy_path_updates_positions_and_cash(ledger):
    ledger.record_intent("i1", "NVDA", "NVDA_US_EQ", D("2"), D("175"),
                         D("1.35"), dry_run=False)
    ledger.record_submitted("i1", 111, "NVDA", "NVDA_US_EQ", D("2"),
                            D("259.26"), "NEW")
    assert ledger.pending_signed_qty("NVDA") == D("2")

    ledger.record_fill(111, 9001, D("2"), D("175.50"), D("-260.00"), [],
                       "2026-08-20T13:30:01Z")
    ledger.record_order_terminal(111, "FILLED", D("2"))

    assert ledger.positions == {"NVDA": D("2")}
    assert ledger.cash_gbp == D("740.00")
    assert ledger.open_orders == {}
    assert ledger.pending_signed_qty("NVDA") == D("0")


def test_fill_is_idempotent_by_fill_id(ledger):
    ledger.record_intent("i1", "NVDA", "NVDA_US_EQ", D("2"), D("175"),
                         D("1.35"), dry_run=False)
    ledger.record_submitted("i1", 111, "NVDA", "NVDA_US_EQ", D("2"),
                            D("259.26"), "NEW")
    assert ledger.record_fill(111, 9001, D("2"), D("175.5"), D("-260"), [],
                              "t") is True
    # A replayed bill must not double the position or the cash movement.
    assert ledger.record_fill(111, 9001, D("2"), D("175.5"), D("-260"), [],
                              "t") is False
    assert ledger.positions == {"NVDA": D("2")}
    assert ledger.cash_gbp == D("740")


def test_terminal_with_unaccounted_fills_freezes(ledger):
    ledger.record_intent("i1", "NVDA", "NVDA_US_EQ", D("2"), D("175"),
                         D("1.35"), dry_run=False)
    ledger.record_submitted("i1", 111, "NVDA", "NVDA_US_EQ", D("2"),
                            D("259.26"), "NEW")
    ledger.record_fill(111, 9001, D("1"), D("175.5"), D("-130"), [], "t")
    with pytest.raises(LedgerFrozenError):
        ledger.record_order_terminal(111, "FILLED", D("2"))
    # The order must still be open so the missing fill can be harvested.
    assert "111" in ledger.open_orders


def test_fill_for_unknown_order_freezes(ledger):
    with pytest.raises(LedgerFrozenError):
        ledger.record_fill(999, 9001, D("1"), D("10"), D("-10"), [], "t")


def test_ambiguity_freezes_and_resolution_unfreezes(ledger):
    ledger.record_intent("i1", "NVDA", "NVDA_US_EQ", D("2"), D("175"),
                         D("1.35"), dry_run=False)
    ledger.record_submit_ambiguous("i1", "NVDA", "NVDA_US_EQ", D("2"), "timeout")
    assert ledger.is_frozen
    with pytest.raises(LedgerFrozenError):
        ledger.record_intent("i2", "AAPL", "AAPL_US_EQ", D("1"), D("230"),
                             D("1.35"), dry_run=False)
    ledger.resolve_ambiguity("i1", 222, "found at the venue")
    assert not ledger.is_frozen
    assert "222" in ledger.open_orders


def test_journal_without_snapshot_refuses_to_load(tmp_path, ledger):
    # Simulate a lost snapshot: the journal alone must NOT rebuild an empty
    # book (that would erase real exposure silently).
    snapshot = tmp_path / "a0_v0_0_1_snapshot.json"
    snapshot.unlink()
    with pytest.raises(LedgerFrozenError):
        ShadowLedger.load(tmp_path, "a0_v0_0_1")


def test_load_roundtrip_preserves_state(tmp_path, funded_ledger):
    loaded = ShadowLedger.load(tmp_path, "a0_v0_0_1")
    assert loaded.positions == {"NVDA": D("2")}
    assert loaded.cash_gbp == D("740.00")


def test_init_refuses_to_overwrite(tmp_path, ledger):
    with pytest.raises(FileExistsError):
        ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", D("500"))


def test_portfolio_view_reserves_open_buys(ledger):
    ledger.record_intent("i1", "NVDA", "NVDA_US_EQ", D("2"), D("175"),
                         D("1.35"), dry_run=False)
    ledger.record_submitted("i1", 111, "NVDA", "NVDA_US_EQ", D("2"),
                            D("100"), "NEW")
    view = ledger.portfolio_view(fee_buffer=D("0.01"))
    assert view.cash_gbp == D("1000")
    assert view.available_cash_gbp == D("1000") - D("100") * D("1.01")
    assert view.pending_signed_qty == {"NVDA": D("2")}


def test_refreeze_after_resolution_is_not_deduplicated(ledger):
    # J1-class regression: a second ambiguous outcome of the SAME intent
    # (after the first was resolved as never-arrived) must freeze again;
    # a fixed event id would silently skip the second freeze.
    ledger.record_intent("i1", "NVDA", "NVDA_US_EQ", D("1"), D("100"),
                         D("1.25"), dry_run=False)
    ledger.record_submit_ambiguous("i1", "NVDA", "NVDA_US_EQ", D("1"), "t1")
    ledger.resolve_ambiguity("i1", None, "proven absent")
    assert not ledger.is_frozen
    ledger.record_submit_ambiguous("i1", "NVDA", "NVDA_US_EQ", D("1"), "t2")
    assert ledger.is_frozen


def test_resubmission_after_resolution_registers_again(ledger):
    ledger.record_intent("i1", "NVDA", "NVDA_US_EQ", D("1"), D("100"),
                         D("1.25"), dry_run=False)
    ledger.record_submit_ambiguous("i1", "NVDA", "NVDA_US_EQ", D("1"), "t1")
    ledger.resolve_ambiguity("i1", None, "proven absent")
    ledger.record_submitted("i1", 222, "NVDA", "NVDA_US_EQ", D("1"),
                            D("80"), "NEW")
    assert "222" in ledger.open_orders


def test_journal_ahead_of_snapshot_refuses_to_load(tmp_path, ledger):
    # Simulate a crash between the fsynced journal append and the snapshot
    # replace: the extra journal event must be detected at load, not lost.
    journal = tmp_path / "a0_v0_0_1_journal.jsonl"
    with open(journal, "a", encoding="utf-8") as handle:
        handle.write('{"ts_utc": "t", "event_id": "AMBIG|iX#1", '
                     '"event_type": "ORDER_SUBMIT_AMBIGUOUS", "payload": {}}\n')
    with pytest.raises(LedgerFrozenError, match="ahead of the snapshot"):
        ShadowLedger.load(tmp_path, "a0_v0_0_1")


def test_knows_order_covers_open_and_retired(funded_ledger):
    assert funded_ledger.knows_order(111)      # retired via FILL/TERMINAL
    assert not funded_ledger.knows_order(999)
    funded_ledger.record_intent("i2", "AAPL", "AAPL_US_EQ", D("1"), D("100"),
                                D("1.25"), dry_run=False)
    funded_ledger.record_submitted("i2", 333, "AAPL", "AAPL_US_EQ", D("1"),
                                   D("80"), "NEW")
    assert funded_ledger.knows_order(333)      # currently open
