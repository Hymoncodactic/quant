"""Event-sourced shadow ledger: the strategy's own book, independent of the
account.

Responsibility: track strategy-owned cash, positions and open orders as an
append-only event journal plus an atomic snapshot, idempotent per event.
Not responsible for: talking to the venue (client.py), deciding anything
(daily_cycle.py), or comparing itself to the account (reconciler.py).

Why a shadow book when T212 returns itemized bills: the account is shared
state (manual app trades land in the same positions endpoint) and the API
has no client order id, so attribution of account state to THIS strategy
must be kept locally. The bills (GET /equity/history/orders fills with
walletImpact.taxes) are the authoritative statement this book is
reconciled against -- the book is the claim, the bill is the proof.

Design borrowed from the QMT reference script (Zhang, V6.8.29), adapted:
    - Event-id idempotency table + JSONL journal + atomic snapshot pair
      (its apply_strategy_position_delta). T212 fills carry a unique id, so
      the event key FILL|order|fill is exact, no cumulative-quantity keys.
    - Refuse to rebuild from an empty base: journal present but snapshot
      missing/corrupt freezes the book for manual recovery instead of
      silently starting from zero (its V6.8.21-FR4).
    - Write-ahead intent: the journal records an order intent BEFORE the
      POST, the venue order id after; a crash between the two is detected
      by reconciliation, not lost (its F7 "submit is the least reversible
      action, persist first").
    - Ambiguity is sticky: an unresolved ambiguous submit blocks every new
      order until resolved (its ledger-replay gate: a book in doubt admits
      no new exposure).

Money and quantity are Decimal end to end; floats appear only in JSON
serialization of reference prices.

Public classes:
    ShadowLedger        The book; construct via ShadowLedger.load / init_fresh
    LedgerFrozenError   The book refuses new exposure until manual recovery

Public functions (methods):
    load(state_dir, strategy_id)             Load an existing book
    init_fresh(state_dir, strategy_id, cash) Create a brand-new book
    record_intent / record_submitted / record_submit_rejected
    record_submit_ambiguous / resolve_ambiguity
    record_fill / record_order_terminal / record_note
    portfolio_view(fee_buffer)               Strategy-facing PortfolioView duck type
    pending_signed_qty(symbol)               Signed open-order quantity
"""

from __future__ import annotations

__all__ = ["ShadowLedger", "LedgerFrozenError"]

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from common.logging_setup import get_logger
# LedgerFrozenError is defined beside the persistence rules that raise it
# first (load-time integrity) and re-exported here as the public name.
from trading212.execution.ledger_store import (LedgerFrozenError, append_event,
                                               journal_path, read_snapshot,
                                               snapshot_path, write_snapshot)

log = get_logger("t212.execution")

# ============================================================================
# [1] Constants
# ============================================================================

_SCHEMA_VERSION = 1
ZERO = Decimal("0")


@dataclass(frozen=True)
class LedgerPortfolioView:
    """Duck type of backtest/engine/engine.PortfolioView for the strategy."""
    cash_gbp: Decimal
    available_cash_gbp: Decimal
    positions: dict[str, Decimal]
    pending_signed_qty: dict[str, Decimal]


# ============================================================================
# [2] Ledger
# ============================================================================

class ShadowLedger:
    """Strategy-owned book: cash, positions, open orders, event journal."""

    def __init__(self, state_dir: Path, strategy_id: str,
                 snapshot: dict[str, Any]) -> None:
        self._dir = state_dir
        self._strategy_id = strategy_id
        self._snap = snapshot

    # ------------------------------------------------------------------
    # [2.1] Construction
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, state_dir: Path, strategy_id: str) -> "ShadowLedger":
        """Load an existing book.

        Raises:
            FileNotFoundError: Nothing exists yet; call init_fresh explicitly.
            LedgerFrozenError: Any load-time integrity rule fails (missing or
                unreadable snapshot, identity mismatch, journal ahead of the
                snapshot); rules and rationale live in ledger_store.py.
        """
        snap = read_snapshot(state_dir, strategy_id, _SCHEMA_VERSION)
        return cls(state_dir, strategy_id, snap)

    @classmethod
    def init_fresh(cls, state_dir: Path, strategy_id: str,
                   allocated_cash_gbp: Decimal) -> "ShadowLedger":
        """Create a brand-new book with an explicit cash allocation.

        Refuses to overwrite an existing book; deleting a book is a manual,
        deliberate act outside this module.
        """
        if snapshot_path(state_dir, strategy_id).exists() \
                or journal_path(state_dir, strategy_id).exists():
            raise FileExistsError(f"ledger for {strategy_id} already exists "
                                  f"under {state_dir}")
        if allocated_cash_gbp <= ZERO:
            raise ValueError("allocated_cash_gbp must be positive")
        state_dir.mkdir(parents=True, exist_ok=True)
        snap = {"schema_version": _SCHEMA_VERSION, "strategy_id": strategy_id,
                "updated_at_utc": None, "cash_gbp": str(allocated_cash_gbp),
                "positions": {}, "open_orders": {}, "ambiguous_intents": {},
                "applied_event_ids": {}}
        ledger = cls(state_dir, strategy_id, snap)
        ledger._apply(f"INIT|{strategy_id}", "INIT",
                      {"allocated_cash_gbp": str(allocated_cash_gbp)})
        return ledger

    # ------------------------------------------------------------------
    # [2.2] Read side
    # ------------------------------------------------------------------

    @property
    def cash_gbp(self) -> Decimal:
        return Decimal(self._snap["cash_gbp"])

    @property
    def positions(self) -> dict[str, Decimal]:
        return {s: Decimal(q) for s, q in self._snap["positions"].items()}

    @property
    def open_orders(self) -> dict[str, dict[str, Any]]:
        """Open orders keyed by venue order id (string keys in JSON)."""
        return dict(self._snap["open_orders"])

    @property
    def ambiguous_intents(self) -> dict[str, dict[str, Any]]:
        return dict(self._snap["ambiguous_intents"])

    @property
    def is_frozen(self) -> bool:
        """A book with unresolved ambiguity admits no new exposure."""
        return bool(self._snap["ambiguous_intents"])

    def pending_signed_qty(self, symbol: str) -> Decimal:
        """Signed unfilled quantity across this symbol's open orders."""
        total = ZERO
        for record in self._snap["open_orders"].values():
            if record["symbol"] == symbol:
                total += Decimal(record["quantity"]) - Decimal(record["accounted_qty"])
        return total

    def knows_order(self, order_id: int) -> bool:
        """Whether this book has ever owned the venue order (open or retired).

        Used by the reconciler to keep this strategy's own RETIRED orders out
        of the ambiguity-evidence candidate pool: yesterday's terminal buy of
        the same quantity must never masquerade as today's lost sell.
        """
        key = str(order_id)
        if key in self._snap["open_orders"]:
            return True
        terminal_prefix = f"TERMINAL|{order_id}|"
        fill_prefix = f"FILL|{order_id}|"
        return any(event_id.startswith((terminal_prefix, fill_prefix))
                   for event_id in self._snap["applied_event_ids"])

    def portfolio_view(self, fee_buffer: Decimal) -> LedgerPortfolioView:
        """Snapshot for the strategy. available cash nets buy reservations.

        fee_buffer is the fraction (e.g. 0.002) reserved on top of a buy's
        reference notional for FX fee and slippage; the same value the risk
        gate uses, injected by the caller so it is defined once in config.
        """
        reserved = ZERO
        for record in self._snap["open_orders"].values():
            qty = Decimal(record["quantity"])
            if qty > ZERO:
                reserved += Decimal(record["ref_notional_gbp"]) * (Decimal("1") + fee_buffer)
        pending = {}
        for record in self._snap["open_orders"].values():
            symbol = record["symbol"]
            pending[symbol] = self.pending_signed_qty(symbol)
        return LedgerPortfolioView(
            cash_gbp=self.cash_gbp,
            available_cash_gbp=self.cash_gbp - reserved,
            positions={s: q for s, q in self.positions.items() if q != ZERO},
            pending_signed_qty={s: q for s, q in pending.items() if q != ZERO})

    # ------------------------------------------------------------------
    # [2.3] Write side (every mutation is one journaled, idempotent event)
    # ------------------------------------------------------------------

    def record_intent(self, intent_id: str, symbol: str, ticker: str,
                      quantity: Decimal, ref_price_usd: Decimal,
                      fx_usd_per_gbp: Decimal, dry_run: bool) -> None:
        """Journal an order intent BEFORE any POST (write-ahead)."""
        if self.is_frozen:
            raise LedgerFrozenError(f"unresolved ambiguous intents "
                                    f"{sorted(self._snap['ambiguous_intents'])}")
        ref_notional = abs(quantity) * ref_price_usd / fx_usd_per_gbp
        self._apply(f"INTENT|{intent_id}", "ORDER_INTENT", {
            "intent_id": intent_id, "symbol": symbol, "ticker": ticker,
            "quantity": str(quantity), "ref_price_usd": str(ref_price_usd),
            "fx_usd_per_gbp": str(fx_usd_per_gbp),
            "ref_notional_gbp": str(ref_notional), "dry_run": dry_run})

    def record_submitted(self, intent_id: str, order_id: int, symbol: str,
                         ticker: str, quantity: Decimal,
                         ref_notional_gbp: Decimal, status: str) -> None:
        """Register the venue's order id for a submitted intent.

        The event id carries an attempt counter: after an ambiguity was
        resolved as never-arrived, the SAME intent may legitimately be
        submitted again, and a fixed id would make the second registration a
        silently skipped duplicate.
        """
        self._apply(self._attempt_id(f"SUBMIT|{intent_id}"), "ORDER_SUBMITTED", {
            "intent_id": intent_id, "order_id": order_id, "status": status},
            mutate=lambda snap: snap["open_orders"].__setitem__(str(order_id), {
                "intent_id": intent_id, "symbol": symbol, "ticker": ticker,
                "quantity": str(quantity), "accounted_qty": "0",
                "ref_notional_gbp": str(ref_notional_gbp),
                "submitted_at_utc": _now_iso(), "last_status": status,
                "accounted_fill_ids": []}))

    def record_submit_rejected(self, intent_id: str, reason: str) -> None:
        """The venue answered and refused; the intent is dead, book unchanged."""
        self._apply(self._attempt_id(f"REJECT|{intent_id}"),
                    "ORDER_SUBMIT_REJECTED",
                    {"intent_id": intent_id, "reason": reason[:300]})

    def record_submit_ambiguous(self, intent_id: str, symbol: str, ticker: str,
                                quantity: Decimal, detail: str,
                                ref_notional_gbp: Decimal = ZERO) -> None:
        """Outcome unknown: freeze the book until a human or the reconciler
        proves what happened at the venue. Attempt-counted event id: a second
        ambiguous outcome of a re-submitted intent must freeze AGAIN, not be
        skipped as a duplicate."""
        self._apply(self._attempt_id(f"AMBIG|{intent_id}"),
                    "ORDER_SUBMIT_AMBIGUOUS",
                    {"intent_id": intent_id, "detail": detail[:300]},
                    mutate=lambda snap: snap["ambiguous_intents"].__setitem__(
                        intent_id, {"symbol": symbol, "ticker": ticker,
                                    "quantity": str(quantity),
                                    "ref_notional_gbp": str(ref_notional_gbp),
                                    "detail": detail[:300], "at": _now_iso()}))

    def resolve_ambiguity(self, intent_id: str, order_id: int | None,
                          evidence: str) -> None:
        """Resolve one ambiguous intent after reconciliation.

        order_id None means the order provably never reached the venue;
        otherwise the found order is registered as submitted.
        """
        pending = self._snap["ambiguous_intents"].get(intent_id)
        if pending is None:
            raise KeyError(f"no ambiguous intent {intent_id}")

        def mutate(snap: dict[str, Any]) -> None:
            record = snap["ambiguous_intents"].pop(intent_id)
            if order_id is not None:
                snap["open_orders"][str(order_id)] = {
                    "intent_id": intent_id, "symbol": record["symbol"],
                    "ticker": record["ticker"], "quantity": record["quantity"],
                    "accounted_qty": "0",
                    "ref_notional_gbp": record.get("ref_notional_gbp", "0"),
                    "submitted_at_utc": record["at"], "last_status": "NEW",
                    "accounted_fill_ids": []}

        self._apply(self._attempt_id(f"RESOLVE|{intent_id}"),
                    "AMBIGUITY_RESOLVED",
                    {"intent_id": intent_id, "order_id": order_id,
                     "evidence": evidence[:300]}, mutate=mutate)

    def record_fill(self, order_id: int, fill_id: int, quantity: Decimal,
                    price: Decimal, cash_delta_gbp: Decimal,
                    taxes: list[dict[str, Any]], filled_at: str) -> bool:
        """Apply one fill from the venue's bill. Idempotent by fill id.

        cash_delta_gbp is SIGNED (negative for buys) and computed by the
        harvester from the bill's walletImpact; the ledger stores what it is
        given and never re-derives money. Returns False when the fill was
        already applied.
        """
        key = str(order_id)
        if key not in self._snap["open_orders"]:
            raise LedgerFrozenError(
                f"fill {fill_id} references order {order_id} unknown to the "
                f"book; attribution is broken, manual reconciliation required")

        def mutate(snap: dict[str, Any]) -> None:
            record = snap["open_orders"][key]
            record["accounted_qty"] = str(Decimal(record["accounted_qty"]) + quantity)
            record["accounted_fill_ids"].append(fill_id)
            symbol = record["symbol"]
            held = Decimal(snap["positions"].get(symbol, "0")) + quantity
            if held == ZERO:
                snap["positions"].pop(symbol, None)
            else:
                snap["positions"][symbol] = str(held)
            snap["cash_gbp"] = str(Decimal(snap["cash_gbp"]) + cash_delta_gbp)

        return self._apply(f"FILL|{order_id}|{fill_id}", "FILL", {
            "order_id": order_id, "fill_id": fill_id, "quantity": str(quantity),
            "price": str(price), "cash_delta_gbp": str(cash_delta_gbp),
            "taxes": taxes, "filled_at": filled_at}, mutate=mutate)

    def record_order_terminal(self, order_id: int, status: str,
                              venue_filled_qty: Decimal) -> None:
        """Retire one order after every fill is accounted.

        Raises:
            LedgerFrozenError: The venue's cumulative filled quantity does
                not match what the book accounted; fills are missing and
                closing the order would silently drop them.
        """
        key = str(order_id)
        record = self._snap["open_orders"].get(key)
        if record is None:
            return  # already retired; terminal notifications may repeat
        accounted = Decimal(record["accounted_qty"])
        if abs(accounted) != abs(venue_filled_qty):
            raise LedgerFrozenError(
                f"order {order_id} terminal {status}: venue filled "
                f"{venue_filled_qty}, book accounted {accounted}; harvest the "
                f"missing fills before retiring the order")
        self._apply(f"TERMINAL|{order_id}|{status}", "ORDER_TERMINAL",
                    {"order_id": order_id, "status": status,
                     "filled_qty": str(venue_filled_qty)},
                    mutate=lambda snap: snap["open_orders"].pop(key, None))

    def record_note(self, note_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Journal a bookkeeping note (cycle markers, reconcile verdicts)."""
        self._apply(f"NOTE|{kind}|{note_id}", f"NOTE_{kind}", payload)

    # ------------------------------------------------------------------
    # [2.4] Persistence core
    # ------------------------------------------------------------------

    def _attempt_id(self, prefix: str) -> str:
        """Event id with a per-occurrence attempt counter.

        Lifecycle events that may legitimately recur for one intent (submit,
        reject, ambiguity, resolution) get "prefix#n" so a recurrence is a
        NEW event, while true replays of the same occurrence still dedupe
        (the QMT reference's J1 lesson: an idempotency key without a
        lifecycle factor collides across cycles and silently drops events).
        """
        n = sum(1 for event_id in self._snap["applied_event_ids"]
                if event_id.startswith(prefix + "#") or event_id == prefix)
        return f"{prefix}#{n + 1}"

    def _apply(self, event_id: str, event_type: str, payload: dict[str, Any],
               mutate=None) -> bool:
        """Journal-then-snapshot application of one idempotent event.

        The journal line is written and fsynced BEFORE the snapshot is
        replaced, so a crash between the two leaves a journal ahead of the
        snapshot -- detectable, replayable, never silently lost. Returns
        False when the event was already applied.
        """
        if event_id in self._snap["applied_event_ids"]:
            log.info("[ledger] duplicate event skipped: %s", event_id)
            return False
        append_event(self._dir, self._strategy_id,
                     {"ts_utc": _now_iso(), "event_id": event_id,
                      "event_type": event_type, "payload": payload})
        if mutate is not None:
            mutate(self._snap)
        self._snap["applied_event_ids"][event_id] = True
        self._snap["updated_at_utc"] = _now_iso()
        write_snapshot(self._dir, self._strategy_id, self._snap)
        return True


def _now_iso() -> str:
    return str(pd.Timestamp.now(tz="UTC"))
