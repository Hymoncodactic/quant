"""Dashboard request handlers: one function per route, no HTTP details.

Responsibility: turn a parsed request into (status, payload). Every handler
returns plain data, so the routes can be exercised in tests without a socket
and the server module stays a transport shell.

Read routes never touch the venue: they answer from the sampler's latest
snapshot and from local state. Only the sampler talks to the venue on a
timer, so opening ten browser tabs cannot multiply the request rate.

Out of scope: sockets, routing and static files, which belong to server.py;
sampling, which belongs to collector.py; submission, which belongs to
manual_orders.py.

Public functions:
    get_state(ctx, collector)          Snapshot, sampler state, readiness.
    get_history(ctx, days, max_points) Downsampled sample history.
    get_settings(ctx)                  Settable fields and what is missing.
    post_settings(ctx, body)           Validate and write settings.
    post_collector(ctx, collector, body)  Start or stop sampling.
    post_ledger_init(ctx, body)        Create the strategy book once.
    get_instruments(ctx)               Strategy symbol to venue ticker map.
    get_manual(_ctx)                   Recent manual order entries.
    post_manual(ctx, body)             Place or rehearse a manual order.

Constants:
    MAX_HISTORY_DAYS   int  30. Upper bound on how much sample history one
                            request may ask for.
    MAX_HISTORY_POINTS int  4000. Upper bound on returned points, so a wide
                            request cannot make the browser unresponsive.

Inputs:
    data/t212/dashboard/  through snapshots.py
Outputs:
    trading212/config/t212.<env>.yaml     through settings.py
    data/t212/execution_state/            through session_cycle and
                                          manual_orders

Change log:
    2026-08-22  Created.
"""

from __future__ import annotations

__all__ = ["get_state", "get_history", "get_settings", "post_settings",
           "post_collector", "post_ledger_init", "get_instruments",
           "get_manual", "post_manual", "MAX_HISTORY_DAYS",
           "MAX_HISTORY_POINTS"]

from decimal import Decimal, InvalidOperation
from typing import Any

from common.logging_setup import get_logger
from trading212.dashboard import manual_orders, settings, snapshots
from trading212.execution import instruments as venue_instruments
from trading212.execution import session_cycle

log = get_logger("t212.dashboard")

MAX_HISTORY_DAYS = 30
MAX_HISTORY_POINTS = 4000

_VENUE = "t212"


def get_state(ctx, collector) -> tuple[int, dict[str, Any]]:
    """Everything the main page repaints on each poll."""
    book = ctx.book_state()
    readiness = settings.describe(ctx.cfg, ledger_ready=bool(book.get("exists")))
    snapshot = snapshots.read_snapshot(_VENUE)
    return 200, {"snapshot": snapshot,
                 "collector": collector.state(),
                 "readiness": readiness,
                 "halted": ctx.halted(),
                 "book": book,
                 "funding": _funding(book, snapshot),
                 "env": ctx.env,
                 "strategy_id": ctx.strategy_id}


def _funding(book: dict[str, Any], snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Compare the book's allocation against the account's free cash.

    The allocation is a bookkeeping figure: creating the book moves no money
    and never touches the account. Nothing reconciles the two at creation
    time, so a book allocated more than the account holds looks perfectly
    healthy right up to the moment the venue starts refusing buys. Surfacing
    the comparison is what turns that into something visible beforehand.
    """
    allocated = book.get("cash_gbp")
    account = (snapshot or {}).get("account") or {}
    if not account.get("ok") or allocated is None:
        return {"known": False, "allocated_gbp": allocated,
                "account_free_gbp": None, "over_account": False}
    cash = ((account.get("summary") or {}).get("cash") or {})
    free = cash.get("availableToTrade")
    if free is None:
        return {"known": False, "allocated_gbp": allocated,
                "account_free_gbp": None, "over_account": False}
    return {"known": True, "allocated_gbp": float(allocated),
            "account_free_gbp": float(free),
            "over_account": float(allocated) > float(free)}


def get_history(ctx, days: int, max_points: int) -> tuple[int, dict[str, Any]]:
    """Downsampled sample history for the charts."""
    days = max(1, min(int(days or 3), MAX_HISTORY_DAYS))
    max_points = max(50, min(int(max_points or 1500), MAX_HISTORY_POINTS))
    rows = snapshots.read_samples(_VENUE, days=days, max_points=max_points)
    return 200, {"days": days, "points": len(rows), "rows": rows}


def get_settings(ctx) -> tuple[int, dict[str, Any]]:
    book = ctx.book_state()
    return 200, settings.describe(ctx.cfg,
                                  ledger_ready=bool(book.get("exists")))


def post_settings(ctx, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Validate a proposed set; write it only when nothing is wrong."""
    problems = settings.validate(body or {})
    if problems:
        return 400, {"ok": False, "problems": problems}
    settings.apply(ctx.env, body)
    ctx.reload_config()
    book = ctx.book_state()
    return 200, {"ok": True,
                 "readiness": settings.describe(
                     ctx.cfg, ledger_ready=bool(book.get("exists")))}


def post_collector(ctx, collector, body: dict[str, Any]) -> tuple[int, dict]:
    """Start or stop sampling. The strategy process is never touched."""
    action = (body or {}).get("action")
    if action == "start":
        collector.start()
    elif action == "stop":
        collector.stop()
    else:
        return 400, {"ok": False, "problem": "unknown_action"}
    return 200, {"ok": True, "collector": collector.state()}


def post_ledger_init(ctx, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Create the strategy's book with an explicit allocation, once."""
    raw = (body or {}).get("cash_gbp")
    try:
        cash = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return 400, {"ok": False, "problem": "cash_not_a_number"}
    if cash <= 0:
        return 400, {"ok": False, "problem": "cash_must_be_positive"}
    try:
        result = session_cycle.init_ledger(ctx.cfg, cash)
    except FileExistsError:
        return 409, {"ok": False, "problem": "ledger_already_exists"}
    except Exception as exc:
        return 500, {"ok": False, "problem": "init_failed",
                     "detail": repr(exc)[:300]}
    return 200, {"ok": True, "result": result}


def get_instruments(ctx) -> tuple[int, dict[str, Any]]:
    """The verified symbol-to-ticker map the order page offers."""
    symbols = list(ctx.params.get("trade_symbols") or [])
    rows = []
    for symbol in symbols:
        try:
            rows.append({"symbol": symbol,
                         "ticker": venue_instruments.order_ticker(symbol)})
        except KeyError:
            continue
    return 200, {"instruments": rows}


def get_manual(_ctx) -> tuple[int, dict[str, Any]]:
    return 200, {"entries": manual_orders.history()}


def post_manual(ctx, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Place or rehearse one manual order.

    Nothing here decides to trade: the caller supplies the ticker, the signed
    quantity, an explicit confirmation and whether the order is real.
    """
    body = body or {}
    ticker = str(body.get("ticker") or "").strip()
    if not ticker:
        return 400, {"ok": False, "problem": "ticker_missing"}
    result = manual_orders.place(ctx, ticker=ticker,
                                 quantity=str(body.get("quantity") or ""),
                                 confirm=bool(body.get("confirm")),
                                 real=bool(body.get("real")))
    status = 200 if result.get("outcome") in ("submitted", "rehearsed") else 400
    return status, {"ok": status == 200, "result": result}
