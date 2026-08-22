"""Order monitoring: poll open orders to rest, harvest fills from the bills.

Responsibility: after submission, watch the ledger's open orders until they
leave the venue's pending set, then pull their fills -- with itemized taxes
-- from GET /equity/history/orders (the venue's authoritative statement)
and settle them into the shadow ledger.
Not responsible for: submitting (order_router.py) or judging account-level
consistency (reconciler.py).

Authority rule (borrowed from the QMT reference script and kept even though
T212 also lacks push channels): the pending-orders listing and the history
bills are the single source of truth; nothing is inferred from the POST
response beyond the order id. The venue's 11 order statuses have no
documented semantics (spec gap), so "active" is defined operationally:
present in GET /equity/orders, which the spec describes as "all orders that
are currently active (i.e., not yet filled, cancelled, or expired)".

Fill money semantics: walletImpact.netValue's sign convention is NOT
documented. The harvester applies the sign from the order side (buy spends,
sell receives) over abs(netValue) and asserts the wallet currency equals
the account currency. First real (or demo) fills must confirm this against
the account cash movement -- registered as an open verification item in
WORKING_MEMORY.md.

Public classes:
    SettleReport

Public functions:
    poll_until_settled(client, ledger, expected_ccy, max_wait_sec, poll_sec)
    harvest_order(client, ledger, order_id, expected_ccy)
"""

from __future__ import annotations

__all__ = ["SettleReport", "poll_until_settled", "harvest_order"]

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from common.logging_setup import get_logger
from trading212.client import T212Client

log = get_logger("t212.execution")

_POLL_MIN_SEC = 5.0     # pending-orders endpoint allows 1 req / 5 s


@dataclass
class SettleReport:
    """Outcome of one settle pass."""
    settled: list[int] = field(default_factory=list)
    still_open: list[int] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.still_open and not self.problems

    def summary(self) -> str:
        return (f"settled={self.settled} still_open={self.still_open} "
                f"problems={self.problems or 'none'}")


# ============================================================================
# [1] Polling
# ============================================================================

def poll_until_settled(client: T212Client, ledger, expected_ccy: str,
                       max_wait_sec: float, poll_sec: float) -> SettleReport:
    """Poll until every ledger open order has left the venue's pending set,
    harvesting each one as it goes; give up (loudly) at max_wait_sec.

    An order still pending at the deadline is left alone: canceling a
    queued market order automatically is a trading decision this layer does
    not take. The report's still_open blocks the next decide phase until a
    human resolves it (or a later settle run finds it done).
    """
    report = SettleReport()
    poll_sec = max(poll_sec, _POLL_MIN_SEC)
    deadline = time.monotonic() + max_wait_sec
    while True:
        open_ids = {int(order_id) for order_id in ledger.open_orders}
        if not open_ids:
            break
        pending_ids = {int(order["id"]) for order in client.pending_orders()}
        for order_id in sorted(open_ids - pending_ids):
            try:
                harvest_order(client, ledger, order_id, expected_ccy)
                report.settled.append(order_id)
            except Exception as exc:
                report.problems.append(f"order {order_id}: {exc!r}")
                log.error("[settle] harvest failed for %s: %r", order_id, exc)
        open_ids = {int(order_id) for order_id in ledger.open_orders}
        if not open_ids:
            break
        if time.monotonic() >= deadline:
            report.still_open = sorted(open_ids)
            log.error("[settle] deadline reached with orders still open: %s",
                      report.still_open)
            break
        time.sleep(poll_sec)
    log.info("[settle] %s", report.summary())
    return report


# ============================================================================
# [2] Harvesting
# ============================================================================

def harvest_order(client: T212Client, ledger, order_id: int,
                  expected_ccy: str) -> None:
    """Settle one no-longer-active order from the history bills.

    Reads every HistoricalOrder item for the order (one item per fill),
    applies each fill idempotently (fill id is the event key), then retires
    the order against the venue's cumulative filled quantity.

    Raises:
        RuntimeError: The order cannot be found in history, or its wallet
            currency is not the account currency. Nothing is guessed.
    """
    record = ledger.open_orders.get(str(order_id))
    if record is None:
        return
    items = _history_items_for(client, order_id, ticker=record["ticker"])
    if not items:
        raise RuntimeError(f"order {order_id} left the pending set but has no "
                           f"history items yet; retry later instead of guessing")

    status = items[0]["order"].get("status", "UNKNOWN")
    side = items[0]["order"].get("side")
    filled_qty = Decimal(str(items[0]["order"].get("filledQuantity") or 0))

    # The venue's side is validated against the book's own intent sign;
    # a mismatch means the harvested order is not what the book thinks it
    # is, and booking it would corrupt positions AND cash at once.
    book_side = "BUY" if Decimal(record["quantity"]) > 0 else "SELL"
    if side not in ("BUY", "SELL"):
        raise RuntimeError(f"order {order_id}: history side {side!r} is not "
                           f"BUY/SELL; refusing to guess the fill direction")
    if side != book_side:
        raise RuntimeError(f"order {order_id}: venue side {side} contradicts "
                           f"the book's {book_side}; attribution is broken")

    for item in items:
        fill = item.get("fill") or {}
        if fill.get("id") is None:
            continue  # e.g. a rejected order's item carries no fill
        _apply_fill(ledger, order_id, side, fill, expected_ccy)

    ledger.record_order_terminal(order_id, str(status), filled_qty)
    log.info("[settle] order_id=%s status=%s filled=%s side=%s dry_run=False",
             order_id, status, filled_qty, side)


def _history_items_for(client: T212Client, order_id: int,
                       ticker: str) -> list[dict[str, Any]]:
    """All history items belonging to one order.

    The full ticker-filtered walk is scanned WITHOUT an early stop: a manual
    app trade on the same ticker can interleave between two of the order's
    fills in the newest-first listing, and stopping at the first
    non-matching item would then drop the older fills (the terminal
    quantity check would freeze the book on a phantom mismatch).
    """
    return [item for item in client.iter_history_orders(ticker=ticker)
            if int(item.get("order", {}).get("id", -1)) == order_id]


def _apply_fill(ledger, order_id: int, side: str, fill: dict[str, Any],
                expected_ccy: str) -> None:
    """Translate one bill fill into a signed ledger event.

    Every money-bearing field must be PRESENT: a null quantity or netValue
    (eventual consistency on a fresh bill) books zeros that no later
    harvest would correct, so the fill is refused and retried later.
    """
    wallet = fill.get("walletImpact") or {}
    currency = wallet.get("currency")
    if currency != expected_ccy:
        raise RuntimeError(f"fill {fill.get('id')} wallet currency {currency!r} "
                           f"differs from account currency {expected_ccy!r}")
    if fill.get("quantity") is None or wallet.get("netValue") is None:
        raise RuntimeError(f"fill {fill.get('id')} has null quantity/netValue; "
                           f"the bill is not final yet, retry later")
    quantity = Decimal(str(fill["quantity"]))
    if quantity == 0:
        raise RuntimeError(f"fill {fill.get('id')} has zero quantity")
    quantity = -abs(quantity) if side == "SELL" else abs(quantity)
    net_value = Decimal(str(wallet["netValue"]))
    cash_delta = -abs(net_value) if quantity > 0 else abs(net_value)
    ledger.record_fill(order_id=order_id, fill_id=int(fill["id"]),
                       quantity=quantity,
                       price=Decimal(str(fill.get("price") or 0)),
                       cash_delta_gbp=cash_delta,
                       taxes=wallet.get("taxes") or [],
                       filled_at=str(fill.get("filledAt")))
