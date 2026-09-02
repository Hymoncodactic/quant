"""Event-sourced shadow ledger: the strategy's own book, independent of the
account.

Responsibility: track strategy-owned cash, positions and open orders as an
append-only event journal plus an atomic snapshot, idempotent per event.
Not responsible for: talking to the venue (client.py), deciding anything
(session_cycle.py), or comparing itself to the account
(reconciler.py).

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
    init_adopted(state_dir, id, source)      Create a book that takes over
                                             another one's cash and positions
                                             (BOOK_ADOPTED)
    record_allocation_change(change_id, delta_gbp, reason)
                                             Resize the strategy's own cash
    portfolio_view(fee_buffer)               Strategy-facing PortfolioView duck type
    pending_signed_qty(symbol)               Signed open-order quantity
    knows_order(order_id)                    Has the book ever owned this order

Constants:
    _SCHEMA_VERSION  int      1. Bumping it makes an older snapshot refuse to
                              load rather than be misread.
    ZERO             Decimal  Decimal("0"), so comparisons never mix in a float.

Inputs:
    data/t212/execution_state/<strategy_id>_snapshot.json  (through ledger_store)
Outputs:
    data/t212/execution_state/<strategy_id>_journal.jsonl
    data/t212/execution_state/<strategy_id>_snapshot.json

Change log:
    2026-08-21  Created.
    2026-08-22  Attempt-counted event ids for recurring lifecycle events, and
                knows_order() so reconciliation stops matching the book's own
                retired orders.
    2026-08-23  Added record_allocation_change(): the allocation has to be
                adjustable once the account is funded further, and editing the
                snapshot directly would break the book's ability to explain
                its own cash from its own history.
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
from trading212.execution.ledger_store import (LedgerFrozenError, append_event, iter_journal,
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

    @property
    def strategy_id(self) -> str:
        """The book this ledger belongs to; alerts name it."""
        return self._strategy_id

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
    def state_dir(self) -> Path:
        """Directory holding this book's journal and snapshot."""
        return self._dir

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
                                ref_notional_gbp: Decimal = ZERO,
                                at: str | None = None) -> None:
        """Outcome unknown: freeze the book until a human or the reconciler
        proves what happened at the venue. Attempt-counted event id: a second
        ambiguous outcome of a re-submitted intent must freeze AGAIN, not be
        skipped as a duplicate.

        at is the instant the POST could have happened, defaulting to now.
        The reconciler anchors BOTH its evidence window (venue createdAt no
        earlier than at minus 10 minutes) and its absence clock to this
        value, so a freeze recorded long after the fact (the dangling-intent
        path) must pass the original intent's journal time here -- stamping
        the freeze time instead would exclude the real order from the
        evidence window and later mis-resolve it as never-arrived.
        """
        self._apply(self._attempt_id(f"AMBIG|{intent_id}"),
                    "ORDER_SUBMIT_AMBIGUOUS",
                    {"intent_id": intent_id, "detail": detail[:300]},
                    mutate=lambda snap: snap["ambiguous_intents"].__setitem__(
                        intent_id, {"symbol": symbol, "ticker": ticker,
                                    "quantity": str(quantity),
                                    "ref_notional_gbp": str(ref_notional_gbp),
                                    "detail": detail[:300],
                                    "at": at or _now_iso()}))

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

    def record_allocation_change(self, change_id: str, delta_gbp: Decimal,
                                 reason: str) -> bool:
        """Add to or take from the cash this strategy is allocated.

        The allocation is a bookkeeping figure, so this moves no money: it
        records that the account owner has decided the strategy may work with
        more or less of the account than before. It is journaled like every
        other event rather than edited into the snapshot, so the book still
        explains every penny of its own cash from its own history.

        Raises:
            LedgerFrozenError: The book is frozen. Resizing a book whose
                exposure is in doubt would bury the doubt.
            ValueError: The change is zero, or would drive cash negative.
        """
        if self.is_frozen:
            raise LedgerFrozenError(
                f"unresolved ambiguous intents "
                f"{sorted(self._snap['ambiguous_intents'])}; resolve them "
                f"before changing the allocation")
        if delta_gbp == ZERO:
            raise ValueError("allocation change of zero")
        new_cash = self.cash_gbp + delta_gbp
        if new_cash < ZERO:
            raise ValueError(
                f"taking {abs(delta_gbp)} would leave cash {new_cash}; the "
                f"book cannot hold negative cash")

        def mutate(snap: dict[str, Any]) -> None:
            snap["cash_gbp"] = str(Decimal(snap["cash_gbp"]) + delta_gbp)

        applied = self._apply(f"ALLOC|{change_id}", "ALLOCATION_CHANGED",
                              {"delta_gbp": str(delta_gbp),
                               "cash_after_gbp": str(new_cash),
                               "reason": reason[:200]}, mutate=mutate)
        if applied:
            log.warning("[ledger] allocation changed by %s to %s (%s)",
                        delta_gbp, new_cash, reason[:120])
        return applied

    def freeze_dangling_live_intents(self) -> list[str]:
        """Freeze every LIVE intent whose submission outcome never landed.

        A process killed between the order POST leaving and record_submitted
        (or record_submit_ambiguous) writing leaves exactly one trace: an
        ORDER_INTENT journal event with dry_run false and no SUBMIT, REJECT,
        or AMBIG event for that intent id. Such an order may be live and
        even filled at the venue; recomputing the same decision would then
        buy the exposure twice. Converting the dangling intent into a
        recorded ambiguity freezes the book and routes it through the same
        evidence-based resolution as any other ambiguous submit.

        Dry-run intents legitimately have no terminal event and are skipped.
        Returns the intent ids frozen by this call (empty on a clean book).

        Known limit: an intent RE-submitted after a never-arrived resolution
        is masked by its first attempt's terminal event, so a crash inside
        the second POST window goes undetected here. Unreachable today --
        the same intent id only recurs inside one session's submit window,
        and an absence resolution cannot complete within it -- but do not
        rely on this scan for re-submission flows without adding per-attempt
        intent events first.
        """
        terminal_prefixes: dict[str, tuple[str, ...]] = {}
        frozen: list[str] = []
        applied = self._snap["applied_event_ids"]
        # Materialized before the loop: freezing appends to the same journal
        # file, and reading a file while appending to it is behavior this
        # code must not depend on.
        records = list(iter_journal(self._dir, self._strategy_id))
        for record in records:
            if record.get("event_type") != "ORDER_INTENT":
                continue
            payload = record.get("payload") or {}
            if payload.get("dry_run", True):
                continue
            intent_id = payload.get("intent_id")
            if not intent_id or intent_id in terminal_prefixes:
                continue
            prefixes = tuple(f"{kind}|{intent_id}#"
                             for kind in ("SUBMIT", "REJECT", "AMBIG"))
            terminal_prefixes[intent_id] = prefixes
            if any(event_id.startswith(prefixes) for event_id in applied):
                continue
            if intent_id in self._snap["ambiguous_intents"]:
                continue
            self.record_submit_ambiguous(
                intent_id, payload.get("symbol", "?"),
                payload.get("ticker", "?"),
                Decimal(payload.get("quantity", "0")),
                "dangling live intent: the process died between the POST "
                "and the outcome record; the order may be live at the venue",
                Decimal(payload.get("ref_notional_gbp", "0")),
                at=record.get("ts_utc"))
            frozen.append(intent_id)
            log.critical("[ledger] dangling live intent %s frozen as "
                         "ambiguous", intent_id)
        return frozen

    @classmethod
    def init_adopted(cls, state_dir: Path, strategy_id: str,
                     source: "ShadowLedger") -> "ShadowLedger":
        """Create a book that takes over another one's cash and positions.

        The account is one account. When B0 replaces A0, the shares A0 holds do
        not move at the venue and neither does the cash; what has to move is
        the RECORD of which strategy owns them. init_fresh cannot express that
        -- it starts from cash alone, so the positions would be left unowned
        and reconciliation would report venue holdings that no book claims.

        Cash may legitimately be zero here, which is why this is a separate
        constructor rather than a flag on init_fresh: a fully invested book has
        no cash, and that is a normal state to inherit, whereas a book created
        from nothing with zero cash is a configuration mistake.

        Refuses when the source still has open orders or is frozen. A fill
        landing after the handover would be applied to a book that no longer
        owns the intent, and no later reconciliation could untangle it.

        The whole thing is ONE journaled event, so a crash midway leaves either
        no new book or a complete one.
        """
        if source.is_frozen:
            raise LedgerFrozenError(
                f"{source.strategy_id} is frozen by ambiguous intents "
                f"{sorted(source.ambiguous_intents)}; resolve them before "
                f"handing the book over")
        if source.open_orders:
            raise ValueError(
                f"{source.strategy_id} still has open orders "
                f"{sorted(source.open_orders)}; settle them first, or their "
                f"fills would land in a book that no longer owns the intent")
        if snapshot_path(state_dir, strategy_id).exists() \
                or journal_path(state_dir, strategy_id).exists():
            raise FileExistsError(f"ledger for {strategy_id} already exists "
                                  f"under {state_dir}")
        positions = {symbol: str(qty) for symbol, qty in
                     source.positions.items() if qty != ZERO}
        cash = source.cash_gbp
        state_dir.mkdir(parents=True, exist_ok=True)
        snap = {"schema_version": _SCHEMA_VERSION, "strategy_id": strategy_id,
                "updated_at_utc": None, "cash_gbp": str(cash),
                "positions": dict(positions), "open_orders": {},
                "ambiguous_intents": {}, "applied_event_ids": {}}
        ledger = cls(state_dir, strategy_id, snap)
        ledger._apply(f"ADOPT|{source.strategy_id}", "BOOK_ADOPTED",
                      {"from_strategy_id": source.strategy_id,
                       "positions": positions, "cash_gbp": str(cash),
                       "at_utc": _now_iso()})
        return ledger

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
