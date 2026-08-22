"""Router tests: WAL ordering, dry-run short circuit, ambiguity freeze."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from trading212.client import OrderSubmitAmbiguousError
from trading212.execution.order_router import submit_intents, intent_id_for
from trading212.execution.risk_gate import OrderIntent

D = Decimal
DAY = pd.Timestamp("2026-08-20")


class FakeClient:
    """Scripted client: each call pops the next behavior."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, Decimal]] = []

    def place_market_order(self, ticker, quantity, extended_hours=False):
        self.calls.append((ticker, quantity))
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


def _intent(symbol, qty, iid=None):
    qty = D(qty)
    return OrderIntent(
        intent_id=iid or intent_id_for("a0_v0_0_1", DAY, symbol, qty),
        symbol=symbol, ticker=f"{symbol}_US_EQ", quantity=qty,
        ref_price_usd=D("100"), fx_usd_per_gbp=D("1.25"))


def test_dry_run_journals_but_never_calls_the_client(ledger):
    client = FakeClient([])
    report = submit_intents([_intent("NVDA", "1")], ledger, client, DAY,
                            dry_run=True, armed=True)
    assert client.calls == []
    assert len(report.dry_run) == 1
    assert ledger.open_orders == {}


def test_unarmed_run_downgrades_to_dry_run(ledger):
    client = FakeClient([])
    report = submit_intents([_intent("NVDA", "1")], ledger, client, DAY,
                            dry_run=False, armed=False)
    assert client.calls == []
    assert len(report.dry_run) == 1


def test_successful_submit_registers_the_order(ledger):
    client = FakeClient([{"id": 555, "status": "NEW"}])
    report = submit_intents([_intent("NVDA", "1")], ledger, client, DAY,
                            dry_run=False, armed=True)
    assert [oid for _, oid in report.submitted] == [555]
    assert ledger.open_orders["555"]["ticker"] == "NVDA_US_EQ"


def test_ambiguous_submit_freezes_and_stops_the_batch(ledger):
    client = FakeClient([OrderSubmitAmbiguousError("NVDA_US_EQ", D("1"),
                                                   "timeout")])
    intents = [_intent("NVDA", "1"), _intent("AAPL", "2")]
    report = submit_intents(intents, ledger, client, DAY,
                            dry_run=False, armed=True)
    # The second intent must never reach the venue after an ambiguous first.
    assert len(client.calls) == 1
    assert report.ambiguous is intents[0]
    assert report.not_attempted == [intents[1]]
    assert ledger.is_frozen


def test_proven_rejection_continues_the_batch(ledger):
    from common.net import PermanentError
    client = FakeClient([PermanentError("HTTP 400 insufficient funds"),
                         {"id": 777, "status": "NEW"}])
    intents = [_intent("NVDA", "1"), _intent("AAPL", "2")]
    report = submit_intents(intents, ledger, client, DAY,
                            dry_run=False, armed=True)
    assert len(report.rejected) == 1
    assert [oid for _, oid in report.submitted] == [777]
    assert not ledger.is_frozen


def test_unexpected_error_after_post_is_ambiguous_not_rejected(ledger):
    # A 200 whose payload cannot be parsed (or any post-POST failure) may
    # have left a LIVE order at the venue; classifying it as a rejection
    # would let the batch continue and the book diverge silently.
    client = FakeClient([KeyError("id"), {"id": 777, "status": "NEW"}])
    intents = [_intent("NVDA", "1"), _intent("AAPL", "2")]
    report = submit_intents(intents, ledger, client, DAY,
                            dry_run=False, armed=True)
    assert report.rejected == []
    assert report.ambiguous is intents[0]
    assert report.not_attempted == [intents[1]]
    assert ledger.is_frozen


def test_duplicate_intent_with_open_order_is_skipped(ledger):
    intent = _intent("NVDA", "1")
    ledger.record_intent(intent.intent_id, "NVDA", "NVDA_US_EQ", D("1"),
                         D("100"), D("1.25"), dry_run=False)
    ledger.record_submitted(intent.intent_id, 555, "NVDA", "NVDA_US_EQ",
                            D("1"), D("80"), "NEW")
    client = FakeClient([{"id": 999, "status": "NEW"}])
    report = submit_intents([intent], ledger, client, DAY,
                            dry_run=False, armed=True)
    # Re-running the same decision day must not create a second venue order.
    assert client.calls == []
    assert report.skipped_duplicates == [intent]


def test_live_submit_without_client_raises(ledger):
    with pytest.raises(ValueError):
        submit_intents([_intent("NVDA", "1")], ledger, None, DAY,
                       dry_run=False, armed=True)
