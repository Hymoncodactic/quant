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
    get_history(ctx, range_id, max_points)  Chart series for one range.
    get_records(ctx, name, limit)      Archive stream contents and sizes.
    get_settings(ctx)                  Settable fields and what is missing.
    post_settings(ctx, body)           Validate and write settings.
    post_collector(ctx, collector, body)  Start or stop sampling.
    post_ledger_init(ctx, body)        Create the strategy book once.
    post_allocation(ctx, body)         Add to or take from the allocation.
    post_ledger_reset(ctx, body)       Retire the book so a new one can start.
    post_halt(ctx, body)               Raise the halt flag, or clear it when
                                       the system is provably clean.
    get_sessions(ctx, days)            Recent and upcoming session spans.
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

import contextlib
import fcntl
import os
import signal as os_signal
import subprocess
import sys
from pathlib import Path

__all__ = ["get_state", "get_history", "get_settings", "get_watch", "get_signals", "post_settings", "post_strategy", "strategy_state",
           "post_collector", "post_ledger_init", "post_allocation",
           "post_ledger_reset",
           "post_halt", "get_sessions", "get_instruments", "get_manual",
           "post_manual", "get_records", "MAX_HISTORY_DAYS",
           "MAX_HISTORY_POINTS", "SESSION_WINDOW_DAYS", "RANGES",
           "TICK_SOURCE_MAX_DAYS", "TARGET_POINTS"]

from decimal import Decimal, InvalidOperation
from typing import Any

from common.logging_setup import get_logger
import pandas as pd

from common.paths import records_dir
from trading212 import archive
from trading212.dashboard import manual_orders, settings, signal_view, snapshots
from trading212.execution import instruments as venue_instruments
from trading212.execution import daemon as daemon_mod
from trading212.execution import reconciler, session_cycle
from trading212.execution.ledger_store import LedgerFrozenError, retire_ledger

log = get_logger("t212.dashboard")

MAX_HISTORY_DAYS = 30
MAX_HISTORY_POINTS = 4000

# Sessions handed to the chart for shading. The venue publishes about six
# weeks of forward calendar, and the chart never looks back further than the
# sample history, so a fortnight either side is ample.
SESSION_WINDOW_DAYS = 14

# Selectable ranges, in the order the interface shows them, with the span
# each covers in days. None means everything on record. Modelled on the
# ranges a stock app offers, because that is the vocabulary a reader already
# has for "how far back am I looking".
RANGES: tuple[tuple[str, float | None], ...] = (
    ("1D", 1), ("1W", 7), ("1M", 31), ("3M", 92), ("6M", 183),
    ("YTD", None), ("1Y", 366), ("2Y", 731), ("5Y", 1827),
    ("10Y", 3653), ("ALL", None),
)

# Above this span the per-tick files are abandoned for the daily rollup. A
# fortnight of ticks is already tens of thousands of points; a decade would
# be tens of millions, which is why the rollup exists at all.
TICK_SOURCE_MAX_DAYS = 14

# Points handed to the browser for any range. A line chart a thousand pixels
# wide cannot show more than about one point per pixel, so sending more only
# costs transfer and drawing time without changing a single visible mark.
TARGET_POINTS = 700

_VENUE = "t212"


def get_state(ctx, collector) -> tuple[int, dict[str, Any]]:
    """Everything the main page repaints on each poll."""
    book = ctx.book_state()
    readiness = settings.describe(ctx.cfg, ledger_ready=bool(book.get("exists")))
    snapshot = snapshots.read_snapshot(_VENUE, env=ctx.env)
    return 200, {"snapshot": snapshot,
                 "collector": collector.state(),
                 "strategy": strategy_state(ctx),
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


def _range_start(range_id: str, now: "pd.Timestamp"):
    """First instant a range covers, or None for everything on record."""
    span = dict(RANGES).get(range_id)
    if range_id == "YTD":
        return pd.Timestamp(year=now.year, month=1, day=1, tz="UTC")
    if span is None:
        return None
    return now - pd.Timedelta(days=span)


def _downsample(rows: list[dict[str, Any]],
                target: int) -> list[dict[str, Any]]:
    """Thin rows to about target points, never dropping a gap marker.

    A gap is the only thing that stops the chart drawing a straight line
    across hours nobody observed, so it survives thinning even when the
    ordinary points around it do not.
    """
    if len(rows) <= target:
        return rows
    gaps = [r for r in rows if r.get("gap")]
    normal = [r for r in rows if not r.get("gap")]
    budget = max(1, target - len(gaps))
    # Ceiling division. Flooring leaves stride 1 for any count between the
    # target and twice it, so the cap silently does nothing exactly where a
    # reader would first notice the chart getting heavy.
    stride = max(1, -(-len(normal) // budget))
    kept = normal[::stride]
    if normal and kept and kept[-1] is not normal[-1]:
        kept.append(normal[-1])
    merged = kept + gaps
    merged.sort(key=lambda r: r.get("ts", ""))
    return merged


def get_history(ctx, range_id: str = "1D",
                max_points: int = TARGET_POINTS) -> tuple[int, dict[str, Any]]:
    """The chart series for one range, at a resolution worth drawing.

    Short ranges read the per-tick files, because that is where the detail
    is. Longer ones read the daily rollup instead: past a fortnight the tick
    files answer the same question with a hundred times the data, and the
    chart cannot show the difference.
    """
    known = dict(RANGES)
    if range_id not in known:
        range_id = "1D"
    target = max(50, min(int(max_points or TARGET_POINTS), MAX_HISTORY_POINTS))
    now = pd.Timestamp.now(tz="UTC")
    start = _range_start(range_id, now)
    span_days = (now - start).total_seconds() / 86400 if start is not None \
        else None

    use_ticks = span_days is not None and span_days <= TICK_SOURCE_MAX_DAYS
    if use_ticks:
        rows = snapshots.read_samples(_VENUE, days=int(span_days) + 2,
                                      env=ctx.env,
                                      max_points=MAX_HISTORY_POINTS)
        rows = [r for r in rows
                if r.get("gap") or str(r.get("ts", "")) >= start.isoformat()]
        source = "ticks"
    else:
        rows = [{"ts": r["day"] + "T00:00:00+00:00",
                 "equity_gbp": r.get("close"),
                 "cash_gbp": r.get("cash_gbp"),
                 "holdings_gbp": r.get("holdings_gbp"),
                 "account_total": r.get("account_total")}
                for r in snapshots.read_rollup(_VENUE, env=ctx.env)]
        if start is not None:
            cut = start.strftime("%Y-%m-%d")
            rows = [r for r in rows if r["ts"][:10] >= cut]
        source = "daily"
        if not rows:
            # Nothing has been rolled up yet, which is normal on the first
            # day. Fall back to the ticks so the chart is not blank.
            rows = snapshots.read_samples(_VENUE, days=MAX_HISTORY_DAYS,
                                          env=ctx.env,
                                          max_points=MAX_HISTORY_POINTS)
            source = "ticks"

    thinned = _downsample(rows, target)
    covered = thinned[0]["ts"] if thinned else None
    return 200, {"range": range_id, "source": source, "points": len(thinned),
                 "available_from": covered, "rows": thinned}


def get_records(ctx, name: str | None = None,
                limit: int = 100) -> tuple[int, dict[str, Any]]:
    """Archive stream sizes, and one stream's recent rows when asked."""
    root = records_dir(_VENUE, ctx.env)
    out: dict[str, Any] = {"streams": archive.stream_stats(root),
                           "directory": str(root)}
    if name:
        known = [n for n, _key in archive.STREAMS]
        if name not in known:
            return 400, {"problem": "unknown_stream", "known": known}
        out["name"] = name
        out["rows"] = archive.read_stream(root, name,
                                          limit=int(limit or 100))
    return 200, out


def get_watch(ctx, collector) -> tuple[int, dict[str, Any]]:
    """Per-symbol intraday quote series plus the latest quote block."""
    series = snapshots.read_quotes(_VENUE, env=ctx.env, days=1)
    return 200, {"series": series,
                 "quotes": collector.quotes() if collector else {},
                 "symbols": ctx.watch_symbols()}


def get_signals(ctx, collector) -> tuple[int, dict[str, Any]]:
    """Gate and signal distances now, plus recently decided sessions."""
    quotes = collector.quotes() if collector else {}
    try:
        live = signal_view.live_signals(ctx, quotes)
    except Exception as exc:
        log.warning("[signals] live view failed: %r", exc)
        return 200, {"ok": False, "problem": repr(exc)[:200],
                     "history": signal_view.decided_history(ctx)}
    return 200, {"ok": True, "live": live,
                 "history": signal_view.decided_history(ctx)}


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
    return 200, {"entries": manual_orders.history(env=ctx.env)}


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


def _daemon_pid_alive(ctx) -> int | None:
    """The daemon's pid when its lock is genuinely held, else None.

    The lock file persists after exit (flock semantics), so presence proves
    nothing; actually holding the flock does. A non-blocking probe that
    SUCCEEDS means nobody holds it -- the daemon is not running.
    """
    path = ctx.state_dir / "daemon.lock"
    if not path.exists():
        return None
    try:
        with open(path, "a+") as handle:
            try:
                # A SHARED probe: it fails against the daemon's exclusive
                # hold (that failure IS the liveness signal) but two
                # concurrent probes never collide with each other, and a
                # daemon acquiring its exclusive lock is not raced by the
                # probe (plus the daemon retries its acquisition briefly).
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return None
            except BlockingIOError:
                handle.seek(0)
                text = handle.read()
    except OSError:
        return None
    for token in text.split():
        if token.startswith("pid="):
            try:
                return int(token.split("=", 1)[1])
            except ValueError:
                return 0
    # Lock held but the pid line not written yet (boot instant): running,
    # holder momentarily unknown. 0 is the sentinel; no real pid is 0.
    return 0


def strategy_state(ctx) -> dict[str, Any]:
    """The daemon's liveness and last written status, for the interface."""
    pid = _daemon_pid_alive(ctx)
    status = daemon_mod.read_status(ctx.state_dir)
    return {"running": pid is not None, "pid": pid, "status": status}


def post_strategy(ctx, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Start or stop the daemon for THIS dashboard's environment.

    Start spawns a detached run_a0 daemon process (its own session, output
    to a log file) so closing the dashboard leaves it running. Stop sends
    SIGTERM to the lock holder's pid, but only after proving that pid is a
    run_a0 process -- a stale pid must never route a signal to a stranger.
    """
    action = (body or {}).get("action")
    if action == "start":
        if _daemon_pid_alive(ctx) is not None:
            return 200, {"ok": True, "running": True,
                         "note": "already_running"}
        log_dir = Path(str(records_dir(_VENUE, ctx.env))).parent.parent             / "logs"
        log_dir = Path(__file__).resolve().parents[2] / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"daemon_{ctx.env}.log"
        with open(log_path, "a") as sink:
            # caffeinate -i holds off IDLE sleep while the daemon lives, so
            # an unattended Mac stays awake through the decision window. It
            # cannot prevent lid-close sleep -- that stays on the ops card.
            process = subprocess.Popen(
                ["/usr/bin/caffeinate", "-i", sys.executable, "-m",
                 "trading212.execution.run_a0", "daemon"],
                cwd=str(Path(__file__).resolve().parents[2]),
                env={**os.environ, "QUANT_ENV": ctx.env},
                stdout=sink, stderr=sink, start_new_session=True)
        log.warning("[strategy] daemon spawned pid=%s env=%s log=%s",
                    process.pid, ctx.env, log_path)
        return 200, {"ok": True, "running": True, "pid": process.pid}
    if action == "stop":
        pid = _daemon_pid_alive(ctx)
        if pid is None:
            return 200, {"ok": True, "running": False,
                         "note": "not_running"}
        if pid == 0:
            return 409, {"ok": False, "problem": "daemon_booting",
                         "detail": "holder pid not readable yet; retry"}
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5)
            cmdline = out.stdout
        except Exception:
            cmdline = ""
        if "run_a0" not in cmdline:
            return 409, {"ok": False, "problem": "pid_not_daemon",
                         "detail": f"pid {pid} is not a run_a0 process"}
        try:
            os.kill(pid, os_signal.SIGTERM)
        except ProcessLookupError:
            return 200, {"ok": True, "running": False,
                         "note": "already_exited"}
        log.warning("[strategy] daemon pid=%s sent SIGTERM", pid)
        return 200, {"ok": True, "running": False, "stopped_pid": pid}
    return 400, {"ok": False, "problem": "unknown_action"}


@contextlib.contextmanager
def _execution_lock(ctx):
    """Hold the SAME advisory lock run_a0 holds, or refuse.

    The dashboard and run_a0 are separate processes writing the same journal
    and snapshot; without a shared lock a ledger reset or allocation change
    can interleave with a decide/settle mid-flight and corrupt the book.
    Yields True while the lock is held, False when run_a0 holds it -- the
    caller then answers 409 instead of mutating.
    """
    path = ctx.state_dir / "run_a0.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        handle.close()


def post_allocation(ctx, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Add to or take from the strategy's allocated cash.

    This is the only way the figure changes outside trading. It moves no real
    money: it records that the account owner has decided the strategy may
    work with a different slice of the account.
    """
    raw = (body or {}).get("delta_gbp")
    try:
        delta = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return 400, {"ok": False, "problem": "amount_not_a_number"}
    with _execution_lock(ctx) as held:
        if not held:
            return 409, {"ok": False, "problem": "strategy_running"}
        ledger = ctx.ledger()
        if ledger is None:
            return 409, {"ok": False, "problem": "no_ledger"}
        change_id = f"{pd.Timestamp.now(tz='UTC').value}"
        try:
            ledger.record_allocation_change(change_id, delta,
                                            str((body or {}).get("reason") or
                                                "changed from the dashboard"))
        except LedgerFrozenError as exc:
            return 409, {"ok": False, "problem": "ledger_frozen",
                         "detail": repr(exc)[:200]}
        except ValueError as exc:
            problem = "amount_is_zero" if "zero" in str(exc) \
                else "would_go_negative"
            return 400, {"ok": False, "problem": problem,
                         "detail": str(exc)[:200]}
        return 200, {"ok": True, "cash_gbp": str(ledger.cash_gbp)}


def post_ledger_reset(ctx, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Retire the current book so a fresh one can be created.

    The allocation is fixed at creation, so changing the strategy's whole
    footing means starting a new book. That is only safe when the old one
    owns nothing: a book with positions is the only record of which account
    holdings belong to this strategy, and discarding it would leave those
    holdings unattributed and the next reconciliation meaningless.

    The old files are renamed, not deleted, so the record survives.
    """
    if not bool((body or {}).get("confirm")):
        return 400, {"ok": False, "problem": "not_confirmed"}
    with _execution_lock(ctx) as held:
        if not held:
            return 409, {"ok": False, "problem": "strategy_running"}
        return _ledger_reset_locked(ctx)


def _ledger_reset_locked(ctx) -> tuple[int, dict[str, Any]]:
    """The reset body, run only while the execution lock is held."""
    try:
        ledger = ctx.ledger()
    except LedgerFrozenError as exc:
        return 409, {"ok": False, "problem": "ledger_frozen",
                     "detail": repr(exc)[:200]}
    if ledger is None:
        return 409, {"ok": False, "problem": "no_ledger"}
    blockers = []
    held = {sym: str(qty) for sym, qty in ledger.positions.items() if qty}
    if held:
        blockers.append({"check": "no_positions", "detail": str(held)})
    if ledger.open_orders:
        blockers.append({"check": "no_open_orders",
                         "detail": str(sorted(ledger.open_orders))})
    if ledger.is_frozen:
        blockers.append({"check": "no_ambiguity",
                         "detail": str(sorted(ledger.ambiguous_intents))})
    if blockers:
        return 409, {"ok": False, "problem": "not_empty", "blockers": blockers}
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
    moved = retire_ledger(ctx.state_dir, ctx.strategy_id, stamp)
    log.warning("[ledger] retired %s to %s", ctx.strategy_id, moved)
    return 200, {"ok": True, "retired_as": moved}


def post_halt(ctx, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Raise the halt flag, or clear it once the system is provably clean.

    Raising is always allowed and needs no justification: stopping is the
    safe direction. Clearing is not the mirror image of it, because a halt
    normally means something was wrong, and clearing it while that thing is
    still wrong would restart trading into the same problem. So a clear
    request runs the checks first and refuses with the reasons when any of
    them fails: the book must load, hold no unresolved ambiguity and no
    unsettled orders, and reconcile against the account.
    """
    action = (body or {}).get("action")
    if action == "raise":
        ctx.halt_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.halt_path.touch()
        log.critical("[halt] raised from the dashboard")
        return 200, {"ok": True, "halted": True}
    if action != "clear":
        return 400, {"ok": False, "problem": "unknown_action"}
    # Clearing runs reconcile, which journals a note into the same ledger
    # run_a0 writes -- so it must hold the execution lock. Raising above
    # stays lock-free on purpose: the emergency stop must never wait.
    with _execution_lock(ctx) as held:
        if not held:
            return 409, {"ok": False, "problem": "strategy_running"}
        return _halt_clear_locked(ctx)


def _halt_clear_locked(ctx) -> tuple[int, dict[str, Any]]:
    """The clear checks, run only while the execution lock is held."""
    blockers = []
    try:
        ledger = ctx.ledger()
    except LedgerFrozenError as exc:
        ledger = None
        blockers.append({"check": "ledger_loads", "detail": repr(exc)[:200]})
    if ledger is None and not blockers:
        blockers.append({"check": "ledger_exists", "detail": "no ledger"})
    if ledger is not None:
        if ledger.is_frozen:
            blockers.append({"check": "no_ambiguity",
                             "detail": str(sorted(ledger.ambiguous_intents))})
        if ledger.open_orders:
            blockers.append({"check": "no_open_orders",
                             "detail": str(sorted(ledger.open_orders))})
        try:
            tickers = {sym: venue_instruments.order_ticker(sym)
                       for sym in (ctx.params.get("trade_symbols") or [])}
            verdict = reconciler.reconcile(ctx.client(), ledger, tickers)
            if not verdict.ok:
                blockers.append({"check": "reconcile",
                                 "detail": "; ".join(verdict.problems)[:300]})
        except Exception as exc:
            blockers.append({"check": "reconcile_ran",
                             "detail": repr(exc)[:200]})
    if blockers:
        log.warning("[halt] clear refused: %s", blockers)
        return 409, {"ok": False, "problem": "not_clean", "blockers": blockers}
    ctx.halt_path.unlink(missing_ok=True)
    log.warning("[halt] cleared from the dashboard after passing every check")
    return 200, {"ok": True, "halted": False}


def get_sessions(ctx, days: int = SESSION_WINDOW_DAYS) -> tuple[int, dict]:
    """Regular session spans around now, for shading the time axis.

    Read from the cached exchange calendar so opening the page cannot burn
    the venue's one-request-per-30-seconds metadata budget.
    """
    days = max(1, min(int(days or SESSION_WINDOW_DAYS), MAX_HISTORY_DAYS))
    try:
        calendar = venue_instruments.load_calendar(ctx.calendar_cache)
        events = venue_instruments.session_events(
            calendar, venue_instruments.US_SCHEDULE_ID_NASDAQ)
        sessions = venue_instruments.sessions(events)
    except (OSError, RuntimeError, KeyError, ValueError) as exc:
        return 200, {"sessions": [], "tz": venue_instruments.EXCHANGE_TZ,
                     "unavailable": repr(exc)[:200]}
    now = pd.Timestamp.now(tz="UTC")
    lo, hi = now - pd.Timedelta(days=days), now + pd.Timedelta(days=days)
    rows = [{"date": str(sess.date_ny), "open_utc": str(sess.open_utc),
             "close_utc": str(sess.close_utc), "is_full": sess.is_full}
            for sess in sessions if lo <= sess.close_utc <= hi]
    return 200, {"sessions": rows, "tz": venue_instruments.EXCHANGE_TZ}
