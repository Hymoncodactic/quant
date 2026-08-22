"""Reconciler tests: one-way position check, attribution, ambiguity rules."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from trading212.execution.reconciler import reconcile, resolve_ambiguities

D = Decimal
TICKERS = {"NVDA": "NVDA_US_EQ", "AAPL": "AAPL_US_EQ"}


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


def _position(ticker, qty):
    return {"instrument": {"ticker": ticker}, "quantity": qty}


def test_clean_when_account_holds_at_least_the_book(funded_ledger):
    client = FakeClient(positions=[_position("NVDA_US_EQ", 2.0)])
    assert reconcile(client, funded_ledger, TICKERS).ok


def test_manual_extra_position_is_tolerated(funded_ledger):
    # Account holds more than the book: presumed manual, one-way check passes.
    client = FakeClient(positions=[_position("NVDA_US_EQ", 5.0),
                                   _position("AAPL_US_EQ", 3.0)])
    assert reconcile(client, funded_ledger, TICKERS).ok


def test_book_exceeding_account_is_a_mismatch(funded_ledger):
    client = FakeClient(positions=[_position("NVDA_US_EQ", 1.0)])
    verdict = reconcile(client, funded_ledger, TICKERS)
    assert not verdict.ok
    assert "book 2 > account 1" in verdict.problems[0]


def test_unknown_api_pending_order_is_a_mismatch(funded_ledger):
    client = FakeClient(pending=[{"id": 999, "ticker": "AAPL_US_EQ",
                                  "initiatedFrom": "API"}],
                        positions=[_position("NVDA_US_EQ", 2.0)])
    verdict = reconcile(client, funded_ledger, TICKERS)
    assert not verdict.ok
    assert "unknown to the book" in verdict.problems[0]


def test_manual_app_pending_order_is_ignored(funded_ledger):
    client = FakeClient(pending=[{"id": 999, "ticker": "AAPL_US_EQ",
                                  "initiatedFrom": "IOS"}],
                        positions=[_position("NVDA_US_EQ", 2.0)])
    assert reconcile(client, funded_ledger, TICKERS).ok


def _make_ambiguous(ledger, at=None):
    ledger.record_intent("iX", "NVDA", "NVDA_US_EQ", D("1"), D("100"),
                         D("1.25"), dry_run=False)
    ledger.record_submit_ambiguous("iX", "NVDA", "NVDA_US_EQ", D("1"), "timeout")
    if at is not None:  # age the record for the absence rule
        snap = ledger._snap["ambiguous_intents"]["iX"]
        snap["at"] = at


def _fresh_created_at():
    return str(pd.Timestamp.now(tz="UTC"))


def test_ambiguity_resolved_to_found_order(ledger):
    _make_ambiguous(ledger)
    client = FakeClient(pending=[{"id": 555, "ticker": "NVDA_US_EQ",
                                  "quantity": 1.0, "side": "BUY",
                                  "createdAt": _fresh_created_at(),
                                  "initiatedFrom": "API"}])
    resolved = resolve_ambiguities(client, ledger)
    assert resolved == ["iX -> order 555"]
    assert not ledger.is_frozen
    assert "555" in ledger.open_orders


def test_own_retired_order_is_never_matched(funded_ledger):
    # Yesterday's terminal BUY of 2 shares (order 111, already in the book's
    # history) must not masquerade as evidence for a lost SELL of 2.
    funded_ledger.record_intent("iS", "NVDA", "NVDA_US_EQ", D("-2"), D("100"),
                                D("1.25"), dry_run=False)
    funded_ledger.record_submit_ambiguous("iS", "NVDA", "NVDA_US_EQ", D("-2"),
                                          "timeout")
    client = FakeClient(history=[{"order": {
        "id": 111, "ticker": "NVDA_US_EQ", "quantity": 2.0, "side": "BUY",
        "createdAt": _fresh_created_at(), "initiatedFrom": "API"}}])
    assert resolve_ambiguities(client, funded_ledger) == []
    assert funded_ledger.is_frozen


def test_opposite_side_candidate_is_not_evidence(ledger):
    _make_ambiguous(ledger)  # ambiguous BUY of 1
    client = FakeClient(pending=[{"id": 556, "ticker": "NVDA_US_EQ",
                                  "quantity": -1.0, "side": "SELL",
                                  "createdAt": _fresh_created_at()}])
    assert resolve_ambiguities(client, ledger) == []
    assert ledger.is_frozen


def test_stale_created_at_is_not_evidence(ledger):
    _make_ambiguous(ledger)
    old_ts = str(pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1))
    client = FakeClient(pending=[{"id": 557, "ticker": "NVDA_US_EQ",
                                  "quantity": 1.0, "side": "BUY",
                                  "createdAt": old_ts}])
    assert resolve_ambiguities(client, ledger) == []
    assert ledger.is_frozen


def test_young_ambiguity_absence_keeps_the_freeze(ledger):
    _make_ambiguous(ledger)  # just created -> too young to trust absence
    client = FakeClient()
    assert resolve_ambiguities(client, ledger) == []
    assert ledger.is_frozen


def test_old_ambiguity_absence_resolves_to_never_arrived(ledger):
    old = str(pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=1))
    _make_ambiguous(ledger, at=old)
    client = FakeClient()
    resolved = resolve_ambiguities(client, ledger)
    assert resolved == ["iX -> never reached the venue"]
    assert not ledger.is_frozen
    assert ledger.open_orders == {}
