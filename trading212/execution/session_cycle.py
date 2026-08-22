"""The A0 hourly session cycle: decide at 15:30, submit before the close.

Responsibility: sequence one US trading session of the A0 hourly arm against
Trading 212, with every gate in the order the backtest implies. Two phases:

    decide  Runs inside a full regular session. Refreshes bars, reconciles,
            computes targets on the 15:30 decision bar's information set,
            diffs against the book, runs the risk gate, waits until shortly
            before the close, and submits market orders there.
    settle  Runs after the close. Polls the submitted orders off the pending
            set, harvests their fills and itemized taxes from the venue's
            bills, retires them in the book, then reconciles.

Timing, from fixplans/t212/a0/02_execution.md sections 2 and 3:
    information  bars strictly before the decision bar, so through the 14:30
                 bar, which closed at 15:30:00. Thirty minutes of lag to the
                 close, by construction, and never re-computed later.
    submission   about one minute before the close, so the fill lands at the
                 session's own closing price rather than the next open.
    fill         the decision bar's close. The backtest charges an extra
                 close_gap_bps for this; live pays whatever it pays and the
                 difference is what reconciliation measures.

The decision is computed once, on the 15:30 information set, and is NOT
recomputed at submission time. Recomputing at 15:59 with fresher bars would
use information the backtest never had.

Out of scope: the signal, which belongs to
trading212/strategy/a0_v0_0_1.py and its intraday shim; transport, which
belongs to trading212/client.py; bookkeeping, which belongs to
trading212/execution/shadow_ledger.py; the calendar, which belongs to
trading212/execution/instruments.py.

Public functions:
    decide(cfg, armed, now_utc=None)   One decision pass; returns a report.
    settle(cfg)                        One settle pass; returns a report.
    status(cfg)                        Read-only account and book overview.
    init_ledger(cfg, cash_gbp)         Create the strategy book, once.

Constants:
    DEFAULT_SUBMIT_LEAD_SEC   int  60. Seconds before the close at which
                                   orders are sent, matching the backtest's
                                   close_window_sec latency budget
                                   (backtest/t212/costs.py).
    DEFAULT_MAX_WAIT_SEC      int  2400. Longest the process will wait
                                   between computing and submitting, which
                                   covers the whole 15:30 to 15:59 span.
    DECISION_PARAM_OVERRIDES  dict  Params the entry layer must inject for
                                   the hourly arm. The strategy module's own
                                   default decision time is 15:59, the minute
                                   arm's value; leaving it unset makes the
                                   shim silently return no targets.

Inputs:
    trading212/config/t212.<env>.yaml
    trading212/config/strategies/<strategy_id>.yaml
    data/t212/curated/... through trading212/execution/market_data.py
Outputs:
    data/t212/execution_state/<strategy_id>_cycle.json
    data/t212/execution_state/exchange_calendar.json
    the shadow ledger journal and snapshot, through shadow_ledger.py

Change log:
    2026-08-21  Created as daily_cycle.py for the daily A0 arm.
    2026-08-22  Renamed and rewritten for the hourly arm: session-based
                gating from the venue calendar, the 15:30 decision key, the
                intraday shim with injected daily history, a wait until the
                pre-close submission instant, and per-session deduplication.
"""

from __future__ import annotations

__all__ = ["decide", "settle", "status", "init_ledger",
           "DEFAULT_SUBMIT_LEAD_SEC", "DEFAULT_MAX_WAIT_SEC",
           "DECISION_PARAM_OVERRIDES"]

import json
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

import pandas as pd
import yaml

from common.logging_setup import get_logger
from common.paths import config_dir, execution_state_dir
from trading212.client import T212Client
from trading212.execution import (instruments, market_data, order_monitor,
                                  order_router, reconciler, risk_gate)
from trading212.execution.shadow_ledger import LedgerFrozenError, ShadowLedger
from trading212.execution.strategy_loader import load_intraday_strategy

log = get_logger("t212.execution")

DEFAULT_SUBMIT_LEAD_SEC = 60
DEFAULT_MAX_WAIT_SEC = 2400

DECISION_PARAM_OVERRIDES = {
    "decision_time_local": instruments.DECISION_TIME_NY,
    "exchange_tz": instruments.EXCHANGE_TZ,
    "bars_per_session": 7,
}

_INTERVAL = "1h"


# ============================================================================
# [1] Shared setup
# ============================================================================

class _Cycle:
    """Everything one phase needs, constructed once per invocation."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        execution = cfg.get("execution") or {}
        strategy_cfg = execution.get("strategy") or {}
        self.signal_name = strategy_cfg.get("name", "a0")
        self.signal_version = strategy_cfg.get("version", "0.0.1")
        self.shim_name = strategy_cfg.get("intraday_name", "a0_intraday")
        self.shim_version = strategy_cfg.get("intraday_version", "0.0.1")
        self.strategy_id = f"{self.signal_name}_v" \
                           + self.signal_version.replace(".", "_")
        self.dry_run = bool(execution.get("dry_run", True))
        self.history_start = str(execution.get("history_start", "2010-01-04"))
        self.submit_lead_sec = int(execution.get("submit_lead_sec",
                                                 DEFAULT_SUBMIT_LEAD_SEC))
        self.max_wait_sec = int(execution.get("max_wait_sec",
                                              DEFAULT_MAX_WAIT_SEC))
        self.state_dir = execution_state_dir("t212")
        self.halt_path = self.state_dir / "halt"
        self.calendar_cache = self.state_dir / "exchange_calendar.json"
        self.cycle_state_path = self.state_dir / f"{self.strategy_id}_cycle.json"
        self.params = self._load_params()
        self.client = T212Client(cfg["_env"], cfg=cfg,
                                 secret_name=(cfg.get("endpoints") or {})
                                 .get("secret_name", "trading212_api_key"))
        self.base_ccy = str((cfg.get("account") or {}).get("base_ccy", "GBP"))

    def _load_params(self) -> dict[str, Any]:
        """Strategy parameters, read once here and passed in as params.

        The strategy layer never reads configuration itself
        (docs/backtest/framework/06_strategy_plugin.md section 2). The
        hourly overrides are applied on top of the shared A0 baseline.
        """
        path = config_dir("t212") / "strategies" / f"{self.strategy_id}.yaml"
        params = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(params, dict):
            raise ValueError(f"{path} did not parse to a mapping")
        params.update(DECISION_PARAM_OVERRIDES)
        return params

    def ledger(self) -> ShadowLedger:
        return ShadowLedger.load(self.state_dir, self.strategy_id)

    def session_list(self) -> list[instruments.Session]:
        calendar = instruments.refresh_calendar(self.client, self.calendar_cache)
        events = instruments.session_events(calendar,
                                            instruments.US_SCHEDULE_ID_NASDAQ)
        return instruments.sessions(events)

    def cycle_state(self) -> dict[str, Any]:
        if self.cycle_state_path.exists():
            return json.loads(self.cycle_state_path.read_text(encoding="utf-8"))
        return {"last_decide_session": None, "orders_by_session": {}}

    def save_cycle_state(self, state: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cycle_state_path.with_suffix(".writing")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        tmp.replace(self.cycle_state_path)


# ============================================================================
# [2] decide
# ============================================================================

def decide(cfg: dict[str, Any], armed: bool,
           now_utc: pd.Timestamp | None = None) -> dict[str, Any]:
    """One decision pass. Every gate must pass or nothing is submitted."""
    cycle = _Cycle(cfg)
    now = now_utc or pd.Timestamp.now(tz="UTC")

    if risk_gate.halt_active(cycle.halt_path):
        return _abort(f"halt flag present at {cycle.halt_path}")

    summary = cycle.client.account_summary()
    if summary.get("currency") != cycle.base_ccy:
        return _abort(f"account currency {summary.get('currency')!r} differs "
                      f"from configured {cycle.base_ccy!r}")

    session = instruments.current_session(cycle.session_list(), now)
    if session is None:
        return _abort("no regular US session is open right now; the hourly "
                      "arm decides inside the session it trades in")
    if not session.is_full:
        return _abort(f"session {session.date_ny} is a half day with no "
                      f"{instruments.DECISION_TIME_NY} bar; no decision is taken")

    key = instruments.decision_key(session)
    submit_at = session.close_utc - pd.Timedelta(seconds=cycle.submit_lead_sec)
    if now < key:
        return _abort(f"too early: the {instruments.DECISION_TIME_NY} bar opens "
                      f"at {key}, now {now}")
    if now > submit_at:
        return _abort(f"too late: orders must be sent by {submit_at} "
                      f"({cycle.submit_lead_sec}s before the close), now {now}")

    state = cycle.cycle_state()
    session_id = str(session.date_ny)
    if state.get("last_decide_session") == session_id:
        return _abort(f"already decided for session {session_id}")

    try:
        ledger = cycle.ledger()
    except (FileNotFoundError, LedgerFrozenError) as exc:
        return _abort(f"ledger unavailable: {exc}")
    if ledger.open_orders:
        return _abort(f"open orders unsettled: {sorted(ledger.open_orders)}; "
                      f"run settle first")

    trade_symbols = list(cycle.params["trade_symbols"])
    state_symbol = cycle.params["state_symbol"]
    fx_symbol = cycle.params["fx_symbol"]
    instruments.validate_mapping(cycle.client, trade_symbols)
    tickers = {s: instruments.order_ticker(s) for s in trade_symbols}
    verdict = reconciler.reconcile(cycle.client, ledger, tickers)
    if not verdict.ok:
        return _abort(f"reconcile mismatch: {verdict.problems}")

    view, history = _assemble_market(cycle, session, key, trade_symbols,
                                     state_symbol, fx_symbol)

    fee_buffer = Decimal(str((cfg.get("risk") or {}).get("fee_buffer", "0")))
    portfolio = ledger.portfolio_view(fee_buffer)
    strategy = load_intraday_strategy(cycle.shim_name, cycle.shim_version,
                                      history)
    targets = strategy(view, portfolio, cycle.params)
    if not targets:
        return _abort(f"strategy returned no targets at key {key}; the shim's "
                      f"own decision gate did not fire")

    intents = _diff_to_intents(cycle, targets, ledger, view, session)
    held_notional = _positions_ref_notional(ledger, view, cycle.params)
    orders_done = int(state.get("orders_by_session", {}).get(session_id, 0))
    gate = risk_gate.check_intents(
        intents, portfolio, held_notional, cfg.get("risk") or {},
        orders_today=orders_done, in_submit_window=True,
        halt_path=cycle.halt_path)
    if gate.closed:
        return _abort(f"risk gate closed: {gate.summary()}") | {
            "targets": {s: str(q) for s, q in targets.items()},
            "intents": len(intents)}

    waited = _wait_for_submit_instant(submit_at, cycle.max_wait_sec,
                                      cycle.halt_path)
    if isinstance(waited, str):
        return _abort(waited)

    report = order_router.submit_intents(gate.approved, ledger, cycle.client,
                                         pd.Timestamp(session.date_ny),
                                         dry_run=cycle.dry_run, armed=armed)

    state["last_decide_session"] = session_id
    state.setdefault("orders_by_session", {})[session_id] = \
        orders_done + len(report.submitted)
    cycle.save_cycle_state(state)

    return {"phase": "decide", "session": session_id,
            "decision_key_utc": str(key),
            "submitted_at_utc": str(pd.Timestamp.now(tz="UTC")),
            "close_utc": str(session.close_utc),
            "targets": {s: str(q) for s, q in targets.items()},
            "intents": len(intents), "gate": gate.summary(),
            "submit": report.summary(),
            "dry_run": cycle.dry_run or not armed,
            "ambiguous": report.ambiguous.symbol if report.ambiguous else None}


def _assemble_market(cycle: _Cycle, session, key: pd.Timestamp,
                     trade_symbols: list[str], state_symbol: str,
                     fx_symbol: str):
    """Refresh, load and gate the market data for one decision.

    The daily series is refreshed too, not only the hourly one: adjustment is
    retroactive, so an ex-dividend date rewrites the whole history and with
    it the splice factor the shim applies to today's session.
    """
    feed_symbols = trade_symbols + [state_symbol, fx_symbol]
    market_data.refresh_bars(feed_symbols, _INTERVAL)
    market_data.refresh_bars(trade_symbols + [state_symbol], "1d")

    end = str(session.date_ny)
    intraday_start = str((pd.Timestamp(session.date_ny)
                          - pd.Timedelta(days=market_data.INTRADAY_SESSIONS_LOADED
                                         * 2)).date())
    frames = market_data.load_frames(feed_symbols, _INTERVAL, intraday_start, end)
    market_data.assert_intraday_ready(frames, key,
                                      trade_symbols + [state_symbol], fx_symbol)
    view = market_data.build_view(frames, key)
    history = market_data.daily_rows(trade_symbols + [state_symbol],
                                     cycle.history_start, end)
    return view, history


def _wait_for_submit_instant(submit_at: pd.Timestamp, max_wait_sec: int,
                             halt_path) -> None | str:
    """Sleep until the pre-close submission instant; abort reason on failure.

    The halt flag is re-checked while waiting, so a halt raised between the
    decision and the submission still stops the orders.
    """
    while True:
        now = pd.Timestamp.now(tz="UTC")
        remaining = (submit_at - now).total_seconds()
        if remaining <= 0:
            return None
        if remaining > max_wait_sec:
            return (f"submission instant {submit_at} is {remaining:.0f}s away, "
                    f"beyond max_wait_sec {max_wait_sec}")
        if risk_gate.halt_active(halt_path):
            return f"halt flag raised while waiting to submit at {submit_at}"
        log.info("[cycle] waiting %.0fs to submit at %s", remaining, submit_at)
        time.sleep(min(remaining, 15.0))


# ============================================================================
# [3] settle
# ============================================================================

def settle(cfg: dict[str, Any]) -> dict[str, Any]:
    """Harvest the session's orders after the close, then reconcile."""
    cycle = _Cycle(cfg)
    execution = cfg.get("execution") or {}
    try:
        ledger = cycle.ledger()
    except (FileNotFoundError, LedgerFrozenError) as exc:
        return _abort(f"ledger unavailable: {exc}")

    resolved = []
    if ledger.is_frozen:
        resolved = reconciler.resolve_ambiguities(cycle.client, ledger)

    report = order_monitor.poll_until_settled(
        cycle.client, ledger, expected_ccy=cycle.base_ccy,
        max_wait_sec=float(execution.get("settle_max_wait_min", 90)) * 60,
        poll_sec=float(execution.get("settle_poll_sec", 30)))

    trade_symbols = list(cycle.params["trade_symbols"])
    tickers = {s: instruments.order_ticker(s) for s in trade_symbols}
    verdict = reconciler.reconcile(cycle.client, ledger, tickers)

    return {"phase": "settle", "resolved_ambiguities": resolved,
            "settle": report.summary(), "reconcile": verdict.summary(),
            "positions": {s: str(q) for s, q in ledger.positions.items()},
            "cash_gbp": str(ledger.cash_gbp)}


# ============================================================================
# [4] status and init
# ============================================================================

def status(cfg: dict[str, Any]) -> dict[str, Any]:
    """Read-only overview of account, book, pending orders and next session."""
    cycle = _Cycle(cfg)
    now = pd.Timestamp.now(tz="UTC")
    out: dict[str, Any] = {"phase": "status", "env": cfg["_env"],
                           "dry_run": cycle.dry_run,
                           "halt": risk_gate.halt_active(cycle.halt_path)}
    out["account_summary"] = cycle.client.account_summary()
    out["api_positions"] = cycle.client.positions()
    out["api_pending_orders"] = cycle.client.pending_orders()
    sessions_ = cycle.session_list()
    live = instruments.current_session(sessions_, now)
    out["session_now"] = None if live is None else {
        "date": str(live.date_ny), "close_utc": str(live.close_utc),
        "is_full": live.is_full,
        "decision_key_utc": str(instruments.decision_key(live))
        if live.is_full else None}
    upcoming = [s for s in sessions_ if s.open_utc > now and s.is_full]
    out["next_full_session"] = None if not upcoming else {
        "date": str(upcoming[0].date_ny),
        "decision_key_utc": str(instruments.decision_key(upcoming[0])),
        "submit_at_utc": str(upcoming[0].close_utc
                             - pd.Timedelta(seconds=cycle.submit_lead_sec))}
    try:
        ledger = cycle.ledger()
        out["book"] = {"cash_gbp": str(ledger.cash_gbp),
                       "positions": {s: str(q) for s, q in ledger.positions.items()},
                       "open_orders": ledger.open_orders,
                       "frozen": ledger.is_frozen,
                       "ambiguous": ledger.ambiguous_intents}
    except (FileNotFoundError, LedgerFrozenError) as exc:
        out["book"] = f"unavailable: {exc}"
    out["cycle_state"] = cycle.cycle_state()
    return out


def init_ledger(cfg: dict[str, Any], cash_gbp: Decimal) -> dict[str, Any]:
    """Create the strategy's book with an explicit cash allocation."""
    cycle = _Cycle(cfg)
    ShadowLedger.init_fresh(cycle.state_dir, cycle.strategy_id, cash_gbp)
    return {"phase": "init-ledger", "strategy_id": cycle.strategy_id,
            "allocated_cash_gbp": str(cash_gbp),
            "state_dir": str(cycle.state_dir)}


# ============================================================================
# [5] Internals
# ============================================================================

def _abort(reason: str) -> dict[str, Any]:
    log.error("[cycle] aborted: %s", reason)
    return {"aborted": True, "reason": reason}


def _diff_to_intents(cycle: _Cycle, targets: dict[str, Decimal], ledger,
                     view, session) -> list[risk_gate.OrderIntent]:
    """Target shares minus held and pending, as market-order intents.

    Mirrors backtest/engine/engine.py _diff_to_specs: the delta's magnitude
    is floored to the venue step and sub-step dust is dropped rather than
    resubmitted every session. Restated here rather than imported because
    the execution layer must not depend on backtest code
    (ARCHITECTURE.md section 2).

    Iteration order is the targets mapping's order, which is the configured
    trade_symbols order. That order is load bearing: sells submitted first
    free cash that later buys can use, so reordering changes which buys the
    venue accepts.
    """
    fx_bar = view.bar(cycle.params["fx_symbol"])
    intents: list[risk_gate.OrderIntent] = []
    for symbol, target in targets.items():
        bar = view.bar(symbol)
        if bar is None or fx_bar is None:
            continue
        current = ledger.positions.get(symbol, Decimal("0")) \
            + ledger.pending_signed_qty(symbol)
        delta = target - current
        sign = 1 if delta > 0 else -1
        magnitude = abs(delta).quantize(risk_gate.T212_QTY_STEP,
                                        rounding=ROUND_DOWN)
        if magnitude == 0:
            continue
        quantity = sign * magnitude
        intents.append(risk_gate.OrderIntent(
            intent_id=order_router.intent_id_for(
                cycle.strategy_id, pd.Timestamp(session.date_ny), symbol,
                quantity),
            symbol=symbol, ticker=instruments.order_ticker(symbol),
            quantity=quantity,
            ref_price_usd=Decimal(str(bar.close)),
            fx_usd_per_gbp=Decimal(str(fx_bar.close))))
    return intents


def _positions_ref_notional(ledger, view, params) -> Decimal:
    """GBP value of held strategy positions at decision prices."""
    fx_bar = view.bar(params["fx_symbol"])
    if fx_bar is None:
        return Decimal("0")
    fx = Decimal(str(fx_bar.close))
    total = Decimal("0")
    for symbol, qty in ledger.positions.items():
        bar = view.bar(symbol)
        if bar is not None:
            total += qty * Decimal(str(bar.close)) / fx
    return total
