"""The A0 daily cycle: decide -> submit (queued) -> settle -> reconcile.

Responsibility: sequencing one trading day of the HELD A0 strategy against
T212, with every gate in the right order. Two entry phases:

    decide  Runs while the US market is CLOSED, after midnight
            Europe/London following the last US close (so the completed
            GBPUSD daily bar exists -- the same information set the
            backtest's daily step sees). Refreshes data, reconciles,
            computes targets from the shadow book, diffs, gates, submits
            market orders that the venue queues to the next open
            (S4: market orders placed while closed are queued; OpenAPI
            mirror, market-order description).
    settle  Runs after the next US open. Polls queued orders off the
            pending set, harvests their fills with itemized taxes from the
            bills, retires them in the book, then reconciles.

Not responsible for: signal math (trading212/strategy/a0_v0_0_1.py, the
single copy), transport (client.py), or bookkeeping (shadow_ledger.py).

Fill-timing equivalence with the backtest: the engine fills a market order
submitted at daily step D at the open of D+1 (backtest/engine/matching.py);
here the same order is submitted while the market is closed after day D and
queued by the venue to the open of D+1. The decide window [London midnight
after D's close, next US open) is enforced, not assumed.

Public functions:
    decide(cfg, armed)     One decision pass; returns a report dict
    settle(cfg)            One settle pass; returns a report dict
    status(cfg)            Read-only account/book overview
    init_ledger(cfg, cash) Create the strategy's book (explicit user act)
"""

from __future__ import annotations

__all__ = ["decide", "settle", "status", "init_ledger"]

import json
from decimal import Decimal, ROUND_DOWN
from typing import Any

import pandas as pd
import yaml

from common.logging_setup import get_logger
from common.paths import config_dir, execution_state_dir
from trading212.client import T212Client
from trading212.execution import instruments, market_data, order_monitor, \
    order_router, reconciler, risk_gate
from trading212.execution.shadow_ledger import LedgerFrozenError, ShadowLedger
from trading212.execution.strategy_loader import load_strategy

log = get_logger("t212.execution")

_STATE_TZ = "Europe/London"


# ============================================================================
# [1] Shared setup
# ============================================================================

class _Cycle:
    """Everything one phase needs, constructed once per invocation."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        execution = cfg.get("execution") or {}
        strategy_cfg = execution.get("strategy") or {}
        self.strategy_name = strategy_cfg.get("name", "a0")
        self.strategy_version = strategy_cfg.get("version", "0.0.1")
        self.strategy_id = f"{self.strategy_name}_v" \
                           + self.strategy_version.replace(".", "_")
        self.dry_run = bool(execution.get("dry_run", True))
        self.history_start = str(execution.get("history_start", "2010-01-04"))
        self.state_dir = execution_state_dir("t212")
        self.halt_path = self.state_dir / "halt"
        self.calendar_cache = self.state_dir / "exchange_calendar.json"
        self.cycle_state_path = self.state_dir / f"{self.strategy_id}_cycle.json"
        self.params = self._load_params()
        self.compute_targets = load_strategy(self.strategy_name,
                                             self.strategy_version)
        self.client = T212Client(cfg["_env"], cfg=cfg,
                                 secret_name=(cfg.get("endpoints") or {})
                                 .get("secret_name", "trading212_api_key"))
        self.base_ccy = str((cfg.get("account") or {}).get("base_ccy", "GBP"))

    def _load_params(self) -> dict[str, Any]:
        """Strategy parameters, read once at the entry layer (contract:
        fixplans/framework/06_strategy_plugin.md section 2)."""
        path = config_dir("t212") / "strategies" \
            / f"{self.strategy_id}.yaml"
        params = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(params, dict):
            raise ValueError(f"{path} did not parse to a mapping")
        return params

    def ledger(self) -> ShadowLedger:
        return ShadowLedger.load(self.state_dir, self.strategy_id)

    def calendar_events(self) -> list[tuple[pd.Timestamp, str]]:
        calendar = instruments.refresh_calendar(self.client, self.calendar_cache)
        return instruments.session_events(calendar,
                                          instruments.US_SCHEDULE_ID_NASDAQ)

    def cycle_state(self) -> dict[str, Any]:
        if self.cycle_state_path.exists():
            return json.loads(self.cycle_state_path.read_text(encoding="utf-8"))
        return {"last_decide_day": None, "orders_by_day": {}}

    def save_cycle_state(self, state: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cycle_state_path.with_suffix(".writing")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        tmp.replace(self.cycle_state_path)


# ============================================================================
# [2] decide
# ============================================================================

def decide(cfg: dict[str, Any], armed: bool) -> dict[str, Any]:
    """One decision pass. Every gate must pass or nothing is submitted."""
    cycle = _Cycle(cfg)
    now_utc = pd.Timestamp.now(tz="UTC")

    if risk_gate.halt_active(cycle.halt_path):
        return _abort(f"halt flag present at {cycle.halt_path}")

    summary = cycle.client.account_summary()
    if summary.get("currency") != cycle.base_ccy:
        return _abort(f"account currency {summary.get('currency')!r} differs "
                      f"from configured {cycle.base_ccy!r}")

    events = cycle.calendar_events()
    if instruments.market_is_open(events, now_utc):
        return _abort("regular US session is open; decide runs only while closed")
    expected_day = instruments.last_completed_trading_day(events, now_utc)

    # FX completeness gate: the GBPUSD daily bar dated expected_day is only
    # final after midnight Europe/London following the US close.
    now_london_date = pd.Timestamp(now_utc.tz_convert(_STATE_TZ).date())
    if now_london_date <= expected_day:
        return _abort(f"too early: London date {now_london_date.date()} must "
                      f"pass decision day {expected_day.date()} so the FX "
                      f"daily bar is complete")

    state = cycle.cycle_state()
    if state.get("last_decide_day") == str(expected_day.date()):
        return _abort(f"already decided for {expected_day.date()}")

    try:
        ledger = cycle.ledger()
    except (FileNotFoundError, LedgerFrozenError) as exc:
        return _abort(f"ledger unavailable: {exc}")
    if ledger.open_orders:
        return _abort(f"open orders unsettled: {sorted(ledger.open_orders)}; "
                      f"run settle first")

    trade_symbols = list(cycle.params["trade_symbols"])
    instruments.validate_mapping(cycle.client, trade_symbols)
    tickers = {s: instruments.order_ticker(s) for s in trade_symbols}
    verdict = reconciler.reconcile(cycle.client, ledger, tickers)
    if not verdict.ok:
        return _abort(f"reconcile mismatch: {verdict.problems}")

    data_symbols = trade_symbols + [cycle.params["state_symbol"],
                                    cycle.params["fx_symbol"]]
    market_data.refresh_daily_bars(data_symbols)
    frames = market_data.load_daily_history(data_symbols, cycle.history_start)
    market_data.assert_fresh(frames, expected_day,
                             exact_symbols=trade_symbols
                             + [cycle.params["state_symbol"]],
                             fx_symbol=cycle.params["fx_symbol"])
    view = market_data.build_view(frames, expected_day)

    fee_buffer = Decimal(str((cfg.get("risk") or {}).get("fee_buffer", "0")))
    portfolio = ledger.portfolio_view(fee_buffer)
    targets = cycle.compute_targets(view, portfolio, cycle.params)
    intents = _diff_to_intents(cycle, targets, ledger, view, expected_day)

    held_notional = _positions_ref_notional(ledger, view, cycle.params)
    orders_today = int(cycle.cycle_state().get("orders_by_day", {})
                       .get(str(expected_day.date()), 0))
    gate = risk_gate.check_intents(
        intents, portfolio, held_notional, cfg.get("risk") or {},
        orders_today=orders_today,
        market_open=instruments.market_is_open(events, pd.Timestamp.now(tz="UTC")),
        halt_path=cycle.halt_path)
    if gate.closed:
        # An aborted run, not a decision: the day stays undecided so a
        # corrected configuration can retry before the next open.
        return _abort(f"risk gate closed: {gate.summary()}") | {
            "targets": {s: str(q) for s, q in targets.items()},
            "intents": len(intents)}

    # Safety margin before the next open: data refresh takes minutes, and a
    # decide started just before 13:30Z would otherwise pass every
    # closed-market check yet POST into the live session, filling at an
    # unmodeled price instead of queueing to the open.
    margin = pd.Timedelta(minutes=float((cfg.get("execution") or {})
                                        .get("decide_open_margin_min", 10)))
    next_open, _ = instruments.next_event(events, pd.Timestamp.now(tz="UTC"),
                                          ("OPEN",))
    if next_open - pd.Timestamp.now(tz="UTC") < margin:
        return _abort(f"next open {next_open} is under the "
                      f"{margin} safety margin; submit earlier in the window")

    report = order_router.submit_intents(gate.approved, ledger, cycle.client,
                                         expected_day, dry_run=cycle.dry_run,
                                         armed=armed)

    state["last_decide_day"] = str(expected_day.date())
    day_key = str(expected_day.date())
    state.setdefault("orders_by_day", {})[day_key] = \
        orders_today + len(report.submitted)
    cycle.save_cycle_state(state)

    return {"phase": "decide", "decision_day": str(expected_day.date()),
            "targets": {s: str(q) for s, q in targets.items()},
            "intents": len(intents), "gate": gate.summary(),
            "submit": report.summary(),
            "dry_run": cycle.dry_run or not armed,
            "ambiguous": report.ambiguous.symbol if report.ambiguous else None}


# ============================================================================
# [3] settle
# ============================================================================

def settle(cfg: dict[str, Any]) -> dict[str, Any]:
    """Harvest queued orders after the open; reconcile afterwards."""
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
# [4] status / init
# ============================================================================

def status(cfg: dict[str, Any]) -> dict[str, Any]:
    """Read-only overview of account, book and pending orders."""
    cycle = _Cycle(cfg)
    out: dict[str, Any] = {"phase": "status", "env": cfg["_env"],
                           "dry_run": cycle.dry_run,
                           "halt": risk_gate.halt_active(cycle.halt_path)}
    out["account_summary"] = cycle.client.account_summary()
    out["api_positions"] = cycle.client.positions()
    out["api_pending_orders"] = cycle.client.pending_orders()
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
                     view, decision_day) -> list[risk_gate.OrderIntent]:
    """Target shares minus (held + pending) becomes market-order intents.

    Mirrors backtest/engine/engine.BacktestEngine._diff_to_specs (deltas
    floored to the venue step, dust ignored); re-stated here because the
    execution layer must not import backtest code (ARCHITECTURE.md
    section 2 keeps the two trees independent).
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
            intent_id=order_router.intent_id_for(cycle.strategy_id,
                                                 decision_day, symbol, quantity),
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
