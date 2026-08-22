"""Manual orders placed by a person from the dashboard.

Responsibility: let the account's owner send one order by hand, with the
confirmation and the audit trail that act deserves, and keep it out of the
strategy's book.

Why a separate journal: the strategy's book is the record of what the
STRATEGY owns, and reconciliation compares it against the account one way,
treating any excess as manual. Writing a hand-placed order into that book
would make the strategy believe it owns something it never decided to buy.
Manual orders therefore land in their own journal, and the account is left
to reconcile as it always did.

Selling a position the strategy owns is possible from here and will make the
next reconciliation fail, because the book will then claim more than the
account holds. That is the correct outcome: it stops the strategy rather
than letting it trade against a book that is no longer true.

Every real submission requires three separate things, none of which this
module can supply on its own: the configuration's live flag, an explicit
confirmation in the request, and the request asking for a real order rather
than a rehearsal. This mirrors CLAUDE.md section 3.1, under which no order
is ever sent without the account owner's action.

Out of scope: the strategy's own orders, which belong to
trading212/execution/order_router.py; transport, which belongs to
trading212/client.py.

Public functions:
    place(context, ticker, quantity, confirm, real)   Submit or rehearse one.
    history(limit)                                    Recent manual entries.

Constants:
    JOURNAL_NAME  str  "manual_orders.jsonl", beside the execution state so
                       one directory holds everything a recovery needs.

Inputs:
    None.
Outputs:
    data/t212/execution_state/manual_orders.jsonl

Change log:
    2026-08-22  Created.
"""

from __future__ import annotations

__all__ = ["place", "history", "JOURNAL_NAME"]

import copy
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from common.logging_setup import get_logger
from common.paths import execution_state_dir
from trading212.client import OrderSubmitAmbiguousError, T212Client

log = get_logger("t212.dashboard")

JOURNAL_NAME = "manual_orders.jsonl"


def _journal_path():
    path = execution_state_dir("t212")
    path.mkdir(parents=True, exist_ok=True)
    return path / JOURNAL_NAME


def _record(entry: dict[str, Any]) -> None:
    with open(_journal_path(), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def history(limit: int = 50) -> list[dict[str, Any]]:
    """The most recent manual entries, newest first."""
    path = _journal_path()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:][::-1]


def place(context, ticker: str, quantity: str, confirm: bool,
          real: bool) -> dict[str, Any]:
    """Submit one manual market order, or rehearse it.

    Args:
        context: The dashboard AppContext.
        ticker: Venue ticker, for example "AAPL_US_EQ".
        quantity: Signed share count as text; negative sells, per the venue's
            convention. Text rather than a float so the exact figure the
            person typed is what gets sent and journaled.
        confirm: The person confirmed this specific order in the interface.
        real: Send it for real. False rehearses and writes a journal entry
            saying so.

    Returns a result mapping with an "outcome" of one of: rehearsed,
    submitted, refused, rejected, ambiguous.
    """
    stamp = datetime.now(timezone.utc).isoformat()
    base = {"ts": stamp, "ticker": ticker, "quantity": quantity,
            "real": bool(real), "env": context.env}

    try:
        qty = Decimal(str(quantity))
    except (InvalidOperation, ValueError):
        entry = {**base, "outcome": "refused", "reason": "quantity_not_a_number"}
        _record(entry)
        return entry
    if qty == 0:
        entry = {**base, "outcome": "refused", "reason": "quantity_is_zero"}
        _record(entry)
        return entry
    if not confirm:
        entry = {**base, "outcome": "refused", "reason": "not_confirmed"}
        _record(entry)
        return entry

    if not real:
        entry = {**base, "outcome": "rehearsed",
                 "reason": "real flag not set; nothing was sent"}
        _record(entry)
        log.info("[manual] rehearsed ticker=%s qty=%s dry_run=True", ticker, qty)
        return entry

    if context.cfg.get("live") is not True:
        entry = {**base, "outcome": "refused", "reason": "config_not_live"}
        _record(entry)
        return entry

    # A manual order is an explicit override of the strategy's dry-run
    # switch, so the client is handed a copy of the configuration with that
    # switch off. The copy is local to this call: nothing the strategy later
    # reads is changed by placing a manual order.
    override = copy.deepcopy(context.cfg)
    override.setdefault("execution", {})["dry_run"] = False
    secret = (context.cfg.get("endpoints") or {}).get("secret_name",
                                                      "trading212_api_key")
    log.critical("[manual] SUBMITTING ticker=%s qty=%s env=%s dry_run=False",
                 ticker, qty, context.env)
    with T212Client(context.env, cfg=override, secret_name=secret) as client:
        try:
            order = client.place_market_order(ticker, qty, extended_hours=False)
        except OrderSubmitAmbiguousError as exc:
            entry = {**base, "outcome": "ambiguous", "reason": exc.detail}
            _record(entry)
            log.critical("[manual] AMBIGUOUS ticker=%s: %s", ticker, exc.detail)
            return entry
        except Exception as exc:
            entry = {**base, "outcome": "rejected", "reason": repr(exc)[:300]}
            _record(entry)
            log.error("[manual] rejected ticker=%s: %r", ticker, exc)
            return entry
    entry = {**base, "outcome": "submitted", "order_id": order.get("id"),
             "status": order.get("status")}
    _record(entry)
    log.critical("[manual] submitted order_id=%s ticker=%s qty=%s",
                 order.get("id"), ticker, qty)
    return entry
