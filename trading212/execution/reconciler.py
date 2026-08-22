"""Reconciliation: prove the shadow ledger against the venue, or freeze.

Responsibility: compare the book to the account (positions, pending orders,
ambiguous submits) and produce a verdict. A mismatch NEVER auto-corrects
either side: the account may hold manual trades the book must not absorb,
and the book may know of exposure the account has not shown yet. Mismatch
means no new orders until a human rules (QMT reference lesson NCL-001/002:
reconciliation that cannot pass locks the system, it does not pass itself).
Not responsible for: applying fills (order_monitor.py) or halting logic
(risk gate reads the verdict through the daily cycle).

Shared-account rule: the T212 account is also traded manually through the
app, so position checks are ONE-WAY -- the account must hold AT LEAST what
the book claims per symbol; excess is presumed manual and ignored. Orders
are attributed through the venue's initiatedFrom field (S4: Order schema
enum includes API): an API-initiated pending order unknown to the book
means another API consumer or a lost submit, both of which freeze trading.
This assumes THIS process is the only API order source on the account
(user-confirmed assumption; recorded in the final report).

Ambiguity resolution: an ambiguous submit is resolved only on positive
evidence -- the order found among pending/history API-initiated orders
matching ticker and quantity (resolved WITH the id), or proven absent from
both (resolved as never-arrived). Anything less keeps the freeze.

Public classes:
    ReconcileVerdict

Public functions:
    reconcile(client, ledger, order_tickers)      Full check, returns verdict
    resolve_ambiguities(client, ledger)           Evidence-based unfreeze
"""

from __future__ import annotations

__all__ = ["ReconcileVerdict", "reconcile", "resolve_ambiguities"]

from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from common.logging_setup import get_logger
from trading212.client import T212Client

log = get_logger("t212.execution")

# Position comparison tolerance, shares. Venue quantities arrive as floats;
# one basis point of the 4-dp order step absorbs float noise without hiding
# any real lot.
_QTY_TOL = Decimal("0.00000001")


@dataclass
class ReconcileVerdict:
    ok: bool
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        state = "CLEAN" if self.ok else "MISMATCH"
        return f"{state}; problems={self.problems or 'none'}; notes={self.notes or 'none'}"


# ============================================================================
# [1] Full reconciliation
# ============================================================================

def reconcile(client: T212Client, ledger,
              order_tickers: dict[str, str]) -> ReconcileVerdict:
    """Compare book vs account. Any problem -> not ok -> no new orders.

    Args:
        client: Read-only capable client.
        ledger: ShadowLedger.
        order_tickers: strategy symbol -> venue ticker map for the universe.
    """
    problems: list[str] = []
    notes: list[str] = []

    if ledger.is_frozen:
        problems.append(f"ledger frozen by ambiguous intents: "
                        f"{sorted(ledger.ambiguous_intents)}")

    # --- pending orders, both directions -------------------------------
    pending = client.pending_orders()
    pending_by_id = {int(order["id"]): order for order in pending}
    for order_id_str, record in ledger.open_orders.items():
        if int(order_id_str) not in pending_by_id:
            notes.append(f"book order {order_id_str} ({record['ticker']}) no "
                         f"longer pending; run settle to harvest it")
    known_ids = {int(order_id) for order_id in ledger.open_orders}
    for order_id, order in pending_by_id.items():
        if order.get("initiatedFrom") == "API" and order_id not in known_ids:
            problems.append(f"API-initiated pending order {order_id} "
                            f"({order.get('ticker')}) unknown to the book")

    # --- positions, one-way --------------------------------------------
    account_qty: dict[str, Decimal] = {}
    for position in client.positions():
        ticker = (position.get("instrument") or {}).get("ticker") \
            or position.get("ticker")
        if ticker:
            account_qty[ticker] = Decimal(str(position.get("quantity") or 0))
    for symbol, book_qty in ledger.positions.items():
        ticker = order_tickers.get(symbol)
        if ticker is None:
            problems.append(f"book position {symbol} has no ticker mapping")
            continue
        held = account_qty.get(ticker, Decimal("0"))
        if book_qty - held > _QTY_TOL:
            problems.append(f"{symbol}: book {book_qty} > account {held} "
                            f"({ticker}); fills lost or double-counted")

    verdict = ReconcileVerdict(ok=not problems, problems=problems, notes=notes)
    level = log.info if verdict.ok else log.critical
    level("[reconcile] %s", verdict.summary())
    ledger.record_note(_note_id(), "RECONCILE",
                       {"ok": verdict.ok, "problems": problems, "notes": notes})
    return verdict


# ============================================================================
# [2] Ambiguity resolution
# ============================================================================

def resolve_ambiguities(client: T212Client, ledger,
                        min_absent_age_sec: float = 600.0) -> list[str]:
    """Try to resolve every ambiguous submit from venue evidence.

    Returns descriptions of what was resolved; whatever stays ambiguous
    keeps the book frozen. Matching key: an API-initiated order with the
    same ticker and quantity that the book does not already know. The A0
    cycle produces at most one order per ticker per day, so this key cannot
    match two intents at once within the freeze window.

    Absence is only trusted once the ambiguity is at least
    min_absent_age_sec old: a POST that timed out client-side may still be
    materializing at the venue, and "not found yet" is not "never arrived"
    (QMT reference lesson: a query miss never confirms cleanliness).
    """
    resolved: list[str] = []
    if not ledger.ambiguous_intents:
        return resolved

    candidates: dict[int, dict] = {}
    for order in client.pending_orders():
        if not ledger.knows_order(int(order["id"])):
            candidates[int(order["id"])] = order
    for item in client.iter_history_orders():
        order = item.get("order") or {}
        if order.get("id") is not None and not ledger.knows_order(int(order["id"])):
            candidates.setdefault(int(order["id"]), order)

    for intent_id, record in list(ledger.ambiguous_intents.items()):
        quantity = Decimal(record["quantity"])
        intent_side = "BUY" if quantity > 0 else "SELL"
        # Matching evidence must be strict on THREE axes at once -- same
        # ticker, same side AND |quantity|, created no earlier than shortly
        # before the ambiguous POST -- so a retired same-size order from an
        # earlier day (opposite side or older createdAt) can never
        # masquerade as the lost submission. ledger.knows_order already
        # excluded everything this book ever owned.
        ambiguous_at = pd.Timestamp(record["at"])
        match_id = None
        for order_id, order in candidates.items():
            if order.get("ticker") != record["ticker"]:
                continue
            if order.get("side") not in (None, intent_side):
                continue
            venue_qty = Decimal(str(order.get("quantity") or 0))
            if abs(abs(venue_qty) - abs(quantity)) > _QTY_TOL:
                continue
            created = order.get("createdAt")
            if created is None:
                continue  # no time evidence -> not evidence
            created_ts = pd.Timestamp(created)
            if created_ts.tzinfo is None:
                created_ts = created_ts.tz_localize("UTC")
            if created_ts < ambiguous_at - pd.Timedelta(minutes=10):
                continue
            match_id = order_id
            break
        if match_id is not None:
            ledger.resolve_ambiguity(intent_id, match_id,
                                     f"found API order {match_id} matching "
                                     f"ticker+quantity")
            candidates.pop(match_id, None)
            resolved.append(f"{intent_id} -> order {match_id}")
            log.warning("[reconcile] ambiguous intent %s resolved to order %s",
                        intent_id, match_id)
            continue
        age_sec = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(record["at"])).total_seconds()
        if age_sec < min_absent_age_sec:
            log.warning("[reconcile] ambiguous intent %s only %.0fs old; too "
                        "early to trust absence, keeping the freeze",
                        intent_id, age_sec)
            continue
        ledger.resolve_ambiguity(intent_id, None,
                                 f"absent from pending and history walks after "
                                 f"{age_sec:.0f}s")
        resolved.append(f"{intent_id} -> never reached the venue")
        log.warning("[reconcile] ambiguous intent %s proven absent", intent_id)
    return resolved


def _note_id() -> str:
    return str(pd.Timestamp.now(tz="UTC").value)
