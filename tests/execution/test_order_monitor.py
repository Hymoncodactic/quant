"""Monitor tests: harvesting bills into the book with correct signs."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading212.execution.order_monitor import (harvest_order,
                                                poll_until_settled)

D = Decimal


class FakeClient:
    def __init__(self, pending_pages, history_items):
        self._pending_pages = list(pending_pages)
        self._history_items = history_items

    def pending_orders(self):
        return self._pending_pages.pop(0) if self._pending_pages else []

    def iter_history_orders(self, ticker=None, max_pages=40):
        yield from self._history_items


def _submitted(ledger, order_id, symbol, qty):
    iid = f"i-{order_id}"
    ledger.record_intent(iid, symbol, f"{symbol}_US_EQ", D(qty), D("100"),
                         D("1.25"), dry_run=False)
    ledger.record_submitted(iid, order_id, symbol, f"{symbol}_US_EQ", D(qty),
                            D("80"), "NEW")


def _item(order_id, side, status, filled, fill_id, qty, net, ccy="GBP"):
    return {"order": {"id": order_id, "side": side, "status": status,
                      "filledQuantity": filled},
            "fill": {"id": fill_id, "price": 100.0, "quantity": qty,
                     "filledAt": "2026-08-21T13:30:01Z",
                     "walletImpact": {"currency": ccy, "netValue": net,
                                      "fxRate": 1.25,
                                      "taxes": [{"name": "CURRENCY_CONVERSION_FEE",
                                                 "quantity": 0.12}]}}}


def test_buy_fill_decreases_cash_and_adds_position(ledger):
    _submitted(ledger, 111, "NVDA", "1")
    client = FakeClient([], [_item(111, "BUY", "FILLED", 1.0, 9001, 1.0, 80.12)])
    harvest_order(client, ledger, 111, "GBP")
    assert ledger.positions == {"NVDA": D("1")}
    assert ledger.cash_gbp == D("1000") - D("80.12")
    assert ledger.open_orders == {}


def test_sell_fill_increases_cash_and_flattens(funded_ledger):
    _submitted(funded_ledger, 222, "NVDA", "-2")
    client = FakeClient([], [_item(222, "SELL", "FILLED", 2.0, 9002, 2.0, 259.5)])
    harvest_order(client, funded_ledger, 222, "GBP")
    assert funded_ledger.positions == {}
    assert funded_ledger.cash_gbp == D("740.00") + D("259.5")


def test_wrong_wallet_currency_refuses(ledger):
    _submitted(ledger, 111, "NVDA", "1")
    client = FakeClient([], [_item(111, "BUY", "FILLED", 1.0, 9001, 1.0, 80.12,
                                   ccy="USD")])
    with pytest.raises(RuntimeError):
        harvest_order(client, ledger, 111, "GBP")


def test_missing_history_refuses_to_guess(ledger):
    _submitted(ledger, 111, "NVDA", "1")
    client = FakeClient([], [])
    with pytest.raises(RuntimeError):
        harvest_order(client, ledger, 111, "GBP")
    assert "111" in ledger.open_orders  # nothing was invented


def test_rejected_order_with_no_fill_retires_cleanly(ledger):
    _submitted(ledger, 111, "NVDA", "1")
    item = {"order": {"id": 111, "side": "BUY", "status": "REJECTED",
                      "filledQuantity": 0}, "fill": {}}
    client = FakeClient([], [item])
    harvest_order(client, ledger, 111, "GBP")
    assert ledger.open_orders == {}
    assert ledger.positions == {}
    assert ledger.cash_gbp == D("1000")


def test_missing_side_refuses_to_guess(ledger):
    _submitted(ledger, 111, "NVDA", "1")
    item = _item(111, None, "FILLED", 1.0, 9001, 1.0, 80.12)
    item["order"].pop("side")
    client = FakeClient([], [item])
    with pytest.raises(RuntimeError, match="side"):
        harvest_order(client, ledger, 111, "GBP")
    assert "111" in ledger.open_orders


def test_side_contradicting_the_book_freezes_harvest(ledger):
    # The book thinks order 111 is a BUY; a SELL-side history item means the
    # attribution is wrong and nothing may be booked from it.
    _submitted(ledger, 111, "NVDA", "1")
    client = FakeClient([], [_item(111, "SELL", "FILLED", 1.0, 9001, 1.0, 80.12)])
    with pytest.raises(RuntimeError, match="contradicts"):
        harvest_order(client, ledger, 111, "GBP")


def test_null_net_value_is_not_booked_as_zero(ledger):
    _submitted(ledger, 111, "NVDA", "1")
    client = FakeClient([], [_item(111, "BUY", "FILLED", 1.0, 9001, 1.0, None)])
    with pytest.raises(RuntimeError, match="null"):
        harvest_order(client, ledger, 111, "GBP")
    assert ledger.cash_gbp == Decimal("1000")  # nothing booked


def test_interleaved_manual_fill_does_not_truncate_harvest(ledger):
    # A manual app trade on the same ticker lands BETWEEN the order's two
    # fills in the newest-first listing; both strategy fills must still be
    # collected or the terminal quantity check would freeze on a phantom
    # mismatch.
    _submitted(ledger, 111, "NVDA", "2")
    manual = _item(999, "BUY", "FILLED", 5.0, 8000, 5.0, 400.0)
    fill2 = _item(111, "BUY", "FILLED", 2.0, 9002, 1.0, 80.0)
    fill1 = _item(111, "BUY", "FILLED", 2.0, 9001, 1.0, 80.0)
    client = FakeClient([], [fill2, manual, fill1])
    harvest_order(client, ledger, 111, "GBP")
    assert ledger.positions == {"NVDA": Decimal("2")}
    assert ledger.cash_gbp == Decimal("1000") - Decimal("160")


def test_poll_settles_when_order_leaves_pending_set(ledger):
    _submitted(ledger, 111, "NVDA", "1")
    client = FakeClient(
        pending_pages=[[]],  # first poll: order already gone
        history_items=[_item(111, "BUY", "FILLED", 1.0, 9001, 1.0, 80.12)])
    report = poll_until_settled(client, ledger, "GBP",
                                max_wait_sec=1, poll_sec=0)
    assert report.settled == [111]
    assert report.clean


def test_poll_reports_still_open_at_deadline(ledger):
    _submitted(ledger, 111, "NVDA", "1")
    client = FakeClient(
        pending_pages=[[{"id": 111}], [{"id": 111}]],
        history_items=[])
    report = poll_until_settled(client, ledger, "GBP",
                                max_wait_sec=0, poll_sec=0)
    assert report.still_open == [111]
    assert not report.clean
