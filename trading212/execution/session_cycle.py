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
    adopt_book(cfg, from_id, confirm)  Move cash and positions from another
                                       strategy's book into this one.
    assemble_params(cfg)               Seam S1: the one parameter mapping the
                                       decision and the dashboard share.

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
    trading212/records/signals.jsonl        one row per decision
    data/t212/execution_state/<strategy_id>_cycle.json
    data/t212/execution_state/exchange_calendar.json
    the shadow ledger journal and snapshot, through shadow_ledger.py

Change log:
    2026-08-21  Created as daily_cycle.py for the daily A0 arm.
    2026-08-22  Renamed and rewritten for the hourly arm: session-based
                gating from the venue calendar, the 15:30 decision key, the
                intraday shim with injected daily history, a wait until the
                pre-close submission instant, and per-session deduplication.
    2026-08-23  Hard submission deadline: past the grace period, or past
                the close, the batch is abandoned rather than sent, since
                a late market order fills at the next open and silently
                swaps the caliber. The risk gate's submission window is
                now read from the clock instead of passed as a constant,
                and settle verifies where each fill landed.
    2026-08-23  Every decision is now recorded to the archive, including
                the ones that placed no order: a session with no trade is
                itself a fact about the strategy, and nothing else keeps it.
"""

from __future__ import annotations

__all__ = ["decide", "settle", "status", "init_ledger", "adopt_book",
           "assemble_params",
           "DEFAULT_SUBMIT_LEAD_SEC", "DEFAULT_MAX_WAIT_SEC",
           "DECISION_PARAM_OVERRIDES"]

import fcntl
import json
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

import pandas as pd
import yaml

from common.alerts import notify
from common.net import SAFETY_RATIO
from common.logging_setup import get_logger
from common.paths import config_dir, execution_state_dir, records_dir
from trading212 import archive
from trading212.client import RATE_LIMITS, T212Client
from trading212.execution import (instruments, market_data, order_monitor,
                                  order_router, reconciler, risk_gate)
from trading212.execution import ledger_store
from trading212.execution.shadow_ledger import LedgerFrozenError, ShadowLedger
from trading212.execution.strategy_loader import (load_intraday_strategy,
                                                  load_module)

log = get_logger("t212.execution")

DEFAULT_SUBMIT_LEAD_SEC = 60
DEFAULT_MAX_WAIT_SEC = 2400
DEFAULT_SUBMIT_GRACE_SEC = 30

DECISION_PARAM_OVERRIDES = {
    "decision_time_local": instruments.DECISION_TIME_NY,
    "exchange_tz": instruments.EXCHANGE_TZ,
    "bars_per_session": 7,
}

_INTERVAL = "1h"


# ============================================================================
# [1] Shared setup
# ============================================================================

def _strategy_params(strategy_id: str) -> dict[str, Any]:
    """One strategy's parameter file, parsed. The strategy never reads it."""
    path = config_dir("t212") / "strategies" / f"{strategy_id}.yaml"
    params = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(params, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return params


def assemble_params(cfg: dict[str, Any]) -> dict[str, Any]:
    """Seam S1: the parameter mapping a decision and the dashboard share.

    The strategy layer never reads configuration itself
    (docs/backtest/framework/06_strategy_plugin.md section 2), so the whole
    mapping is built here, once. Two callers exist -- the decision cycle and
    the dashboard's signal view -- and they MUST see the same values or the
    panel will explain a decision that was taken on different numbers.

    For a single-signal configuration (name: a0) the result is the strategy's
    own file plus DECISION_PARAM_OVERRIDES, exactly as before this seam
    existed. For name: b0 it also carries:

        a0_params   A0's file, with live_from overridden to
                    execution.b0_live_from. The yaml's own 2018-01-01 would
                    let A0 trade inside the merged book before B0 started.
        a1_params   A1's file, with live_from AND rebalance_anchor overridden
                    to the same date. The anchor is what session 0 of the
                    21-session rotation counts from, so B0's start date must
                    also be a rebalance date; a different anchor would mean
                    the first session has no book to hold.
        trade_symbols / state_symbol
                    Copied up from a0_params. They are what the execution
                    layer feeds, refreshes and reconciles against; the B0
                    module itself reads them out of a0_params and never off
                    the top level.

    Both nested mappings receive DECISION_PARAM_OVERRIDES too, because the
    synthetic daily view A0's signal is computed on is built from them.
    """
    execution = cfg.get("execution") or {}
    strategy_cfg = execution.get("strategy") or {}
    name = str(strategy_cfg.get("name", "a0"))
    version = str(strategy_cfg.get("version", "0.0.1"))
    strategy_id = f"{name}_v" + version.replace(".", "_")

    params = _strategy_params(strategy_id)
    params.update(DECISION_PARAM_OVERRIDES)
    if name != "b0":
        return params

    live_from = str(execution.get("b0_live_from") or "")
    if not live_from:
        # Deliberately NOT falling back to the yaml's own live_from. That
        # value is the reproduction arm's date; inheriting it silently would
        # anchor the 21-session rotation on 2020-01-02, so the go-live day
        # would land on an arbitrary point in the cycle instead of on session
        # zero, and A1 would have no book on its first day.
        raise ValueError("execution.b0_live_from must be set for strategy b0; "
                         "it is B0's start date AND A1's rebalance anchor, "
                         "and the two have to be the same day")
    _assert_anchor_is_a_session(live_from)
    a0_params = _strategy_params("a0_v0_0_1")
    a0_params.update(DECISION_PARAM_OVERRIDES)
    a0_params["live_from"] = live_from
    a1_params = _strategy_params("a1_v0_0_1")
    a1_params.update(DECISION_PARAM_OVERRIDES)
    a1_params["live_from"] = live_from
    a1_params["rebalance_anchor"] = live_from

    params["live_from"] = live_from
    # history_start is load bearing: the volatility gate's expanding
    # percentile moves with the daily start date. Under A0 it reaches the
    # strategy through _Cycle.history_start; under B0 the daily rows are built
    # inside load_b0_injection, so it has to travel in params or the live gate
    # would quietly differ from the configured one.
    params["history_start"] = str(execution.get("history_start", "2010-01-04"))
    params["a0_params"] = a0_params
    params["a1_params"] = a1_params
    params["trade_symbols"] = list(a0_params["trade_symbols"])
    params["state_symbol"] = a0_params["state_symbol"]
    params.setdefault("fx_symbol", a0_params["fx_symbol"])
    return params


def _assert_anchor_is_a_session(live_from: str) -> None:
    """Refuse a rotation anchor that cannot be session 0.

    The anchor is index 0 of the rotation by construction, so whatever date is
    written here becomes the first rebalance. A date that is not a session
    shifts every rebalance after it, and a date a few sessions in the PAST
    makes the go-live day land mid-cycle -- no rotation for up to 21 sessions,
    with an A1 leg that contributes nothing while looking healthy.

    Only two things are checkable here. A go-live date is today or later, and
    the stored calendar cannot confirm a session whose own daily bar is not
    published yet, so such a date is only rejected when it falls on a weekend.
    A date strictly in the PAST must be a stored session. The decisive check --
    that today really is rotation 0 -- happens in decide(), where the session
    list and the book are both known.
    """
    day = pd.Timestamp(live_from).date()
    if day.weekday() >= 5:
        raise ValueError(
            f"execution.b0_live_from {live_from!r} is a weekend. It is "
            f"rotation session 0, so it must be the session B0 actually "
            f"starts trading on.")
    # Today is the normal go-live date, and its own daily bar is not published
    # until after the close, so the stored calendar cannot confirm it either.
    # Only a date strictly in the past is checkable here.
    if day >= pd.Timestamp.now(tz=instruments.EXCHANGE_TZ).date():
        return
    try:
        sessions_ = market_data.us_sessions(live_from, live_from)
    except FileNotFoundError:                      # no lake: nothing to check
        return
    if day not in sessions_:
        raise ValueError(
            f"execution.b0_live_from {live_from!r} is not a US trading "
            f"session in the stored calendar. It is rotation session 0, so a "
            f"non-session date shifts every rebalance after it.")


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
        # B0 is the only configuration whose universe is not fixed, so it is
        # the only one that needs the injection path, the wide ticker map and
        # the per-name schedule check.
        self.is_b0 = self.signal_name == "b0"
        self.records_root = records_dir("t212", cfg["_env"])
        self.dry_run = bool(execution.get("dry_run", True))
        self.history_start = str(execution.get("history_start", "2010-01-04"))
        self.submit_lead_sec = int(execution.get("submit_lead_sec",
                                                 DEFAULT_SUBMIT_LEAD_SEC))
        self.max_wait_sec = int(execution.get("max_wait_sec",
                                              DEFAULT_MAX_WAIT_SEC))
        self.submit_grace_sec = int(execution.get("submit_grace_sec",
                                                  DEFAULT_SUBMIT_GRACE_SEC))
        self.state_dir = execution_state_dir("t212", cfg["_env"])
        self.halt_path = self.state_dir / "halt"
        self.calendar_cache = self.state_dir / "exchange_calendar.json"
        self.cycle_state_path = self.state_dir / f"{self.strategy_id}_cycle.json"
        self.params = self._load_params()
        self.client = T212Client(cfg["_env"], cfg=cfg,
                                 secret_name=(cfg.get("endpoints") or {})
                                 .get("secret_name", "trading212_api_key"))
        self.base_ccy = str((cfg.get("account") or {}).get("base_ccy", "GBP"))

    def _load_params(self) -> dict[str, Any]:
        """Strategy parameters for this cycle; see assemble_params()."""
        return assemble_params(self.cfg)

    def ledger(self) -> ShadowLedger:
        return ShadowLedger.load(self.state_dir, self.strategy_id)

    def session_list(self) -> list[instruments.Session]:
        calendar = instruments.refresh_calendar(self.client, self.calendar_cache)
        events = instruments.session_events(calendar,
                                            instruments.US_SCHEDULE_ID_NASDAQ)
        return instruments.sessions(events)

    def halted(self) -> bool:
        """Whether the halt flag file is present."""
        return risk_gate.halt_active(self.halt_path)

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

# Maximum tolerated gap between this machine's clock and the venue's HTTP
# Date header before a cycle refuses to run. The submit instant is placed
# submit_lead_sec (60s) before the close with submit_grace_sec (30s) of
# slack; a clock off by more than 10s already distorts that placement
# materially, while HTTP Date resolution (1s) plus round-trip latency stay
# well under 10s on this network, so the bound cannot false-trip.
MAX_CLOCK_SKEW_SEC = 10.0


def decide(cfg: dict[str, Any], armed: bool,
           now_utc: pd.Timestamp | None = None,
           stop_check=None) -> dict[str, Any]:
    """One decision pass. Every gate must pass or nothing is submitted."""
    cycle = _Cycle(cfg)
    now = now_utc or pd.Timestamp.now(tz="UTC")

    if risk_gate.halt_active(cycle.halt_path):
        return _abort(f"halt flag present at {cycle.halt_path}")

    summary = cycle.client.account_summary()
    if summary.get("currency") != cycle.base_ccy:
        return _abort(f"account currency {summary.get('currency')!r} differs "
                      f"from configured {cycle.base_ccy!r}")
    skew = cycle.client.last_clock_skew_sec()
    if skew is not None and abs(skew) > MAX_CLOCK_SKEW_SEC:
        # Submission timing is computed on the local clock against the
        # venue's schedule; a drifted clock submits into the wrong session
        # phase (a slow clock sends the "60s before close" order after the
        # close, filling at the NEXT open). Refusing is the only safe move.
        return _abort(f"system clock differs from the venue by {skew:+.1f}s "
                      f"(bound {MAX_CLOCK_SKEW_SEC}s); fix the clock first")

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

    # Learned venue precisions, before any quantity is floored. A step
    # learned from a rejection last session is worthless if it is not
    # installed before this session sizes the same name.
    risk_gate.load_qty_steps(cycle.state_dir)

    try:
        ledger = cycle.ledger()
    except (FileNotFoundError, LedgerFrozenError) as exc:
        return _abort(f"ledger unavailable: {exc}")
    dangling = ledger.freeze_dangling_live_intents()
    if dangling:
        notify(f"{cycle.strategy_id} dangling intent -- book frozen",
               f"{len(dangling)} live intent(s) with no recorded outcome; "
               f"run settle to resolve against the venue")
        return _abort(f"dangling live intents frozen as ambiguous: "
                      f"{dangling}; run settle to resolve them")
    if ledger.is_frozen:
        return _abort(f"ledger frozen by ambiguous intents: "
                      f"{sorted(ledger.ambiguous_intents)}; run settle")
    if ledger.open_orders:
        return _abort(f"open orders unsettled: {sorted(ledger.open_orders)}; "
                      f"run settle first")
    shortfall = _venue_cash_shortfall(summary, ledger.cash_gbp)
    if shortfall is not None:
        venue_free, book_cash = shortfall
        log.critical("[decide] venue free cash %s below ledger cash %s",
                     venue_free, book_cash)
        notify(f"{cycle.strategy_id} cash shortfall -- no orders",
               f"account free {venue_free} < strategy book {book_cash} "
               f"GBP; lower the allocation or fund the account")
        return _abort(f"venue availableToTrade {venue_free} is below ledger "
                      f"cash {book_cash}; allocation no longer covered")

    trade_symbols = list(cycle.params["trade_symbols"])

    # Everything this session may touch: A0's fixed eighteen, A1's current
    # book, and whatever the ledger still holds. Mapping and reconciliation
    # both have to cover that set. Proving eighteen tickers while the book
    # holds forty positions would report a clean reconciliation that is not
    # one, and it is the positions outside the eighteen that a wide-universe
    # strategy accumulates.
    a1_book = (market_data.a1_book_from_records(cycle.records_root)
               if cycle.is_b0 else {})
    in_play = sorted(set(trade_symbols) | set(a1_book) | set(ledger.positions))
    # A0's eighteen must validate or the session stops; a wide-universe name
    # that fails only loses its own orders.
    mapped = instruments.validate_mapping(cycle.client, in_play,
                                          required=trade_symbols)
    unmapped = sorted(set(in_play) - set(mapped))
    if unmapped:
        log.warning("[decide] %d name(s) failed instrument validation and are "
                    "dropped from this session: %s", len(unmapped), unmapped)
    schedule_ids = {meta.get("workingScheduleId") for meta in mapped.values()
                    if meta.get("workingScheduleId") is not None}
    divergent_ids = instruments.divergent_schedule_ids(
        instruments.load_calendar(cycle.calendar_cache), schedule_ids,
        session.date_ny)
    schedule_divergent = sorted(
        symbol for symbol, meta in mapped.items()
        if meta.get("workingScheduleId") in divergent_ids)
    if set(schedule_divergent) & set(trade_symbols):
        # The cycle times ONE close for the whole universe. A0's eighteen are
        # the caliber every recorded number was measured on, so a divergence
        # among them still costs the whole session.
        return _abort(f"exchange schedules disagree for {session.date_ny} on "
                      f"A0 names: {sorted(set(schedule_divergent) & set(trade_symbols))}")
    if schedule_divergent:
        # One wide-universe name on an odd schedule loses its own order, not
        # everyone else's decision.
        log.warning("[decide] dropping %d name(s) on a divergent schedule: %s",
                    len(schedule_divergent), schedule_divergent)

    tickers = instruments.ticker_map_for(in_play)
    verdict = reconciler.reconcile(cycle.client, ledger, tickers)
    if not verdict.ok:
        return _abort(f"reconcile mismatch: {verdict.problems}")

    view, injected, thin = _assemble_market(cycle, session, key, ledger,
                                            a1_book)

    fee_buffer = Decimal(str((cfg.get("risk") or {}).get("fee_buffer", "0")))
    portfolio = ledger.portfolio_view(fee_buffer)
    strategy = load_intraday_strategy(cycle.shim_name, cycle.shim_version,
                                      injected)
    targets = strategy(view, portfolio, cycle.params)
    for symbol in set(schedule_divergent) | set(unmapped):
        targets.pop(symbol, None)
    stranded = _stranded_holdings(cycle, ledger, view)
    if stranded:
        # A held name with no bar at all is frozen by the sizing rule, counts
        # as zero in equity and in the gross check, and never produces an
        # order -- so it can sit at the venue indefinitely while looking like
        # nothing. Freezing is still the right target (there is no price to
        # sell at), but it must not be silent.
        log.critical("[decide] %d held name(s) have no bar in the loaded "
                     "window and cannot be priced or sold: %s",
                     len(stranded), stranded)
        notify(f"{cycle.strategy_id} holdings cannot be priced",
               f"{', '.join(stranded[:6])}: no bar in the loaded window; "
               f"they are frozen and invisible to the risk gate until the "
               f"feed returns or you sell them manually")

    diagnostics = _signal_diagnostics(cycle, view, portfolio, injected)
    # Persist the rotation NOW, before any of the abort paths below. Session
    # zero is consumed by being reached; a book decided and then not recorded
    # is a book the next session will liquidate.
    rebalance = _record_rotation(cycle, session_id, diagnostics, injected)
    if cycle.is_b0 and isinstance(injected, dict):
        if injected.get("session_calendar_stale"):
            return _abort(
                f"the stored session list ends {injected.get('sessions')[-2] if len(injected.get('sessions') or []) > 1 else None} "
                f"and trails {session.date_ny} by "
                f"{injected.get('session_lag_days')} calendar days; the A1 "
                f"rotation counter would be wrong. Run the daily update "
                f"(scripts/update_data.py) and retry")
        a1_tree = (diagnostics or {}).get("a1") or {}
        tree = a1_tree.get("rebalance") or {}
        if not injected.get("a1_book") and not rebalance \
                and not injected.get("a1_frozen"):
            return _abort(
                f"the A1 book is empty and {session.date_ny} is not rotation "
                f"session 0 (index {tree.get('session_index')} of every "
                f"{tree.get('every')}); the A1 half would contribute nothing "
                f"for {tree.get('sessions_until_next')} more sessions. Check "
                f"execution.b0_live_from -- it must be the session B0 starts")
    if not targets:
        return _abort(f"strategy returned no targets at key {key}; the shim's "
                      f"own decision gate did not fire")

    intents = _diff_to_intents(cycle, targets, ledger, view, session)
    _warn_if_buys_precede_sells(intents)
    held_notional = _positions_ref_notional(ledger, view, cycle.params)
    orders_done = int(state.get("orders_by_session", {}).get(session_id, 0))
    gate_now = pd.Timestamp.now(tz="UTC")
    in_window = (key <= gate_now
                 < session.close_utc + pd.Timedelta(seconds=0))
    gate = risk_gate.check_intents(
        intents, portfolio, held_notional, cfg.get("risk") or {},
        orders_today=orders_done, in_submit_window=in_window,
        halt_path=cycle.halt_path)
    if gate.closed:
        return _abort(f"risk gate closed: {gate.summary()}") | {
            "targets": {s: str(q) for s, q in targets.items()},
            "intents": len(intents)}

    waited = _wait_for_submit_instant(submit_at, session.close_utc,
                                      cycle.submit_grace_sec,
                                      cycle.max_wait_sec, cycle.halt_path,
                                      stop_check=stop_check)
    if isinstance(waited, str):
        return _abort(waited) | {
            "targets": {s: str(q) for s, q in targets.items()},
            "intents": len(intents)}

    # Throughput, measured at the instant orders actually go out. Market
    # orders clear at about 0.58 per second after the client's own headroom,
    # so a rotation batch can be longer than the runway that is left.
    #
    # A partial batch is NOT an option here. Reductions are ordered first, so
    # truncating sends every sell and none of the buys: the account is
    # liquidated and not re-entered, and the strategy re-enters next session at
    # a different price having paid the spread twice. The whole batch is
    # therefore abandoned, which leaves the book exactly as it was and costs
    # one session. Nothing has been submitted at this point -- the ledger is
    # untouched -- so the abort is clean.
    capacity, needed = _fit_before_close(gate.approved, session)
    if len(gate.approved) > capacity:
        notify(f"{cycle.strategy_id} session abandoned -- batch too large",
               f"{len(gate.approved)} orders need {needed:.0f}s but only "
               f"{capacity} fit before the close; nothing was sent")
        return _abort(
            f"{len(gate.approved)} approved order(s) need about {needed:.0f}s "
            f"at the venue's market-order rate, but only {capacity} fit "
            f"before {session.close_utc}. Sending part of the batch would "
            f"submit every sell and no buy, so nothing was sent. Raise "
            f"execution.submit_lead_sec (currently {cycle.submit_lead_sec}s) "
            f"if this batch size is expected -- that is a caliber change") | {
            "targets": {s: str(q) for s, q in targets.items()},
            "intents": len(intents), "orders_needed_sec": round(needed, 1),
            "orders_capacity": capacity}

    report = order_router.submit_intents(gate.approved, ledger, cycle.client,
                                         pd.Timestamp(session.date_ny),
                                         dry_run=cycle.dry_run, armed=armed,
                                         halt_path=cycle.halt_path,
                                         state_dir=cycle.state_dir)

    archive.record_signals(records_dir("t212", cfg["_env"]), {
        "strategy_id": cycle.strategy_id,
        "session": session_id,
        "decision_key_utc": str(key),
        "close_utc": str(session.close_utc),
        "targets": {sym: str(qty) for sym, qty in targets.items()},
        "intents": [{"symbol": i.symbol, "ticker": i.ticker,
                     "quantity": str(i.quantity),
                     "ref_price_usd": str(i.ref_price_usd),
                     "fx_usd_per_gbp": str(i.fx_usd_per_gbp),
                     "ref_notional_gbp": str(i.ref_notional_gbp)}
                    for i in intents],
        "gate": {"approved": [i.symbol for i in gate.approved],
                 "rejected": [{"symbol": i.symbol, "reason": r}
                              for i, r in gate.rejected],
                 "closed": gate.closed},
        "submit": {"submitted": [{"symbol": i.symbol, "order_id": oid}
                                 for i, oid in report.submitted],
                   "dry_run": [i.symbol for i in report.dry_run],
                   "rejected": [{"symbol": i.symbol, "reason": r}
                                for i, r in report.rejected],
                   "ambiguous": report.ambiguous.symbol
                   if report.ambiguous else None},
        "book_before": {"cash_gbp": str(portfolio.cash_gbp),
                        "positions": {s: str(q)
                                      for s, q in portfolio.positions.items()}},
        "attribution": (diagnostics.get("attribution") or {}).get("positions",
                                                                  {}),
        "rebalance": bool(((diagnostics.get("a1") or {}).get("rebalance")
                           or {}).get("sessions_until_next") == 0),
        "symbols_without_decision_bar": thin,
        "dry_run": cycle.dry_run or not armed,
    })

    _record_b0_streams(cycle, session_id, diagnostics, injected)

    state["last_decide_session"] = session_id
    state.setdefault("orders_by_session", {})[session_id] = \
        orders_done + len(report.submitted)
    cycle.save_cycle_state(state)

    return {"phase": "decide", "session": session_id,
            "decide_finished_utc": str(pd.Timestamp.now(tz="UTC")),
            "rebalance": rebalance,
            "diagnostics": diagnostics,
            "decision_key_utc": str(key),
            "submitted_at_utc": str(pd.Timestamp.now(tz="UTC")),
            "close_utc": str(session.close_utc),
            "targets": {s: str(q) for s, q in targets.items()},
            "intents": len(intents), "gate": gate.summary(),
            "symbols_without_decision_bar": thin,
            "stranded_holdings": stranded,
            "submit": report.summary(),
            "dry_run": cycle.dry_run or not armed,
            "ambiguous": report.ambiguous.symbol if report.ambiguous else None}


def _assemble_market(cycle: _Cycle, session, key: pd.Timestamp, ledger,
                     a1_book: dict):
    """Refresh, load and gate the market data for one decision.

    Returns (view, injected, thin). `injected` is whatever this strategy's
    factory binds: the adjusted daily rows for the A0 shim, the whole
    injection object for B0. The caller passes it straight to
    load_intraday_strategy, so the call site is the same either way.

    The daily series is refreshed too, not only the hourly one: adjustment is
    retroactive, so an ex-dividend date rewrites the whole history and with
    it the splice factor the shim applies to today's session.
    """
    trade_symbols = list(cycle.params["trade_symbols"])
    state_symbol = cycle.params["state_symbol"]
    fx_symbol = cycle.params["fx_symbol"]
    end = str(session.date_ny)
    intraday_start = str((pd.Timestamp(session.date_ny)
                          - pd.Timedelta(days=market_data.INTRADAY_SESSIONS_LOADED
                                         * 2)).date())

    if not cycle.is_b0:
        feed_symbols = trade_symbols + [state_symbol, fx_symbol]
        market_data.refresh_bars(feed_symbols, _INTERVAL)
        market_data.refresh_bars(trade_symbols + [state_symbol], "1d")
        frames = market_data.load_frames(feed_symbols, _INTERVAL,
                                         intraday_start, end)
        thin = market_data.assert_intraday_ready(frames, key, trade_symbols,
                                                 state_symbol, fx_symbol)
        view = market_data.build_view(frames, key)
        history = market_data.daily_rows(trade_symbols + [state_symbol],
                                         cycle.history_start, end)
        return view, history, thin

    # B0: the A1 half of the universe is decided at decision time, and it is
    # the names this session is ABOUT TO PICK, not only the ones the last
    # rotation chose. Resolving the pick first is what makes the refresh fetch
    # them; building the list from the previous book alone left every new name
    # priceless, and a priceless name is sized to zero, so the first rotation
    # would have sold and bought nothing.
    universe = market_data.a1_universe_for(
        cycle.params, session.date_ny, held=list(ledger.positions),
        records_root=cycle.records_root)
    a1_names = sorted(set(universe["names"]) - set(trade_symbols)
                      - {state_symbol, fx_symbol})
    thin = list(market_data.refresh_for_decision(cycle.params, session, key,
                                                 a1_names))
    injection = market_data.load_b0_injection(
        cycle.params, session.date_ny, held=list(ledger.positions),
        records_root=cycle.records_root)
    feed_symbols = injection["view_symbols"]
    # missing="skip": 1,477 of the 1,501 stored equities have no 1h partition
    # at all, so a name entering the book for the first time, or one whose
    # short-window fetch failed, has nothing on disk. Raising there would kill
    # the session over exactly the case `thin` exists to tolerate.
    frames = market_data.load_frames(feed_symbols, _INTERVAL, intraday_start,
                                     end, missing="skip")
    absent = [s for s in feed_symbols if s not in frames]
    hard_absent = [s for s in absent
                   if s in set(trade_symbols) | {state_symbol, fx_symbol}]
    if hard_absent:
        raise FileNotFoundError(
            f"no {_INTERVAL} partitions for {hard_absent}; these are the "
            f"caliber symbols and the session cannot be priced without them")
    thin += absent
    thin += market_data.assert_intraday_ready(
        frames, key, trade_symbols, state_symbol, fx_symbol,
        soft_symbols=[s for s in a1_names if s in frames])
    thin = sorted(set(thin))
    injection["thin"] = thin
    view = market_data.build_view(frames, key)
    return view, injection, thin


def _signal_diagnostics(cycle: _Cycle, view, portfolio, injected) -> dict:
    """Seam S6, computed BEFORE anything is submitted.

    After submission the book already carries this session's pending
    quantities, so every held/entering/exiting status and the whole
    attribution would describe a state the decision never saw. Failures are
    swallowed on purpose: a diagnostics panel must never be able to stop a
    decision that has already passed every gate.
    """
    if not cycle.is_b0:
        return {}
    try:
        module = load_module(cycle.shim_name, cycle.shim_version)
        return module.signal_diagnostics(view, portfolio, cycle.params,
                                         injected)
    except Exception as exc:                       # noqa: BLE001
        log.warning("[decide] diagnostics unavailable: %s", exc)
        return {"error": str(exc)}


def _wait_for_submit_instant(submit_at: pd.Timestamp, close_utc: pd.Timestamp,
                             grace_sec: int, max_wait_sec: int,
                             halt_path, stop_check=None) -> None | str:
    """Sleep until the submission instant; return an abort reason instead of
    submitting when that instant has already gone by.

    This is the last gate before real orders, and the only one evaluated at
    the moment they actually go out. Everything before it ran on a clock
    reading taken at the top of decide(), with data refreshes and several
    REST round trips in between; being late by then is entirely possible.

    Late is not a smaller version of on time. A market order that misses the
    close is queued by the venue to the NEXT session's open, which is the
    next_open timing the ruling explicitly did not choose. Sending it anyway
    would swap the caliber silently, so past the grace period the whole
    batch is abandoned and the session simply goes undecided.

    The halt flag is re-read while waiting, so a halt raised between the
    decision and the submission still stops the orders. stop_check, when
    given, is polled the same way: an operator stopping the daemon while
    this wait is parked must abandon the pending submission, not have it
    fire half an hour after the stop.
    """
    while True:
        now = pd.Timestamp.now(tz="UTC")
        remaining = (submit_at - now).total_seconds()
        if remaining <= 0:
            late = -remaining
            if now >= close_utc:
                return (f"the session closed at {close_utc}; an order sent now "
                        f"would fill at the next open, which is not the "
                        f"authoritative timing")
            if late > grace_sec:
                return (f"submission instant {submit_at} passed {late:.0f}s ago, "
                        f"beyond submit_grace_sec {grace_sec}; refusing to send "
                        f"orders that may miss the close")
            log.warning("[cycle] submitting %.0fs late, inside the %ds grace",
                        late, grace_sec)
            return None
        if remaining > max_wait_sec:
            return (f"submission instant {submit_at} is {remaining:.0f}s away, "
                    f"beyond max_wait_sec {max_wait_sec}")
        if risk_gate.halt_active(halt_path):
            return f"halt flag raised while waiting to submit at {submit_at}"
        if stop_check is not None and stop_check():
            return (f"stop requested while waiting to submit at {submit_at}; "
                    f"abandoning this session's submission")
        log.info("[cycle] waiting %.0fs to submit at %s", remaining, submit_at)
        time.sleep(min(remaining, 15.0))


def _venue_cash_shortfall(summary: dict[str, Any],
                          book_cash: Decimal) -> tuple[Decimal, Decimal] | None:
    """Whether the account's free cash no longer covers the book's cash.

    The account is shared with manual trading; when its free cash has fallen
    below what the book believes the strategy may spend, buys the gate would
    approve die at the venue instead -- a silent drift from the baseline.
    Cash reserved for pending orders (a manual GTC limit in the shared
    account, say) is counted as still present: it has not left the account,
    and aborting every session over it would stop even risk-reducing sells
    while misnaming the cause. When free cash alone is short but reserved
    cash covers the gap, a warning names the reservation instead. A missing
    or unparsable field counts as zero, which fails closed. One penny of
    tolerance absorbs float noise in the venue's cash figures. Returns
    (available_total, book_cash) on shortfall, None when covered.
    """
    cash = summary.get("cash") or {}

    def _dec(raw: Any) -> Decimal:
        try:
            return Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")

    venue_free = _dec(cash.get("availableToTrade"))
    reserved = _dec(cash.get("reservedForOrders"))
    available_total = venue_free + reserved
    if available_total < book_cash - Decimal("0.01"):
        return available_total, book_cash
    if venue_free < book_cash - Decimal("0.01"):
        log.warning("[decide] venue free cash %s below ledger cash %s but "
                    "%s is reserved for pending orders; buys may be "
                    "rejected at the venue until they clear",
                    venue_free, book_cash, reserved)
    return None


# ============================================================================
# [3] settle
# ============================================================================

def settle(cfg: dict[str, Any], stop_check=None) -> dict[str, Any]:
    """Harvest the session's orders after the close, then reconcile."""
    cycle = _Cycle(cfg)
    execution = cfg.get("execution") or {}
    try:
        ledger = cycle.ledger()
    except (FileNotFoundError, LedgerFrozenError) as exc:
        return _abort(f"ledger unavailable: {exc}")

    dangling = ledger.freeze_dangling_live_intents()
    if dangling:
        log.critical("[settle] dangling live intents frozen: %s", dangling)

    resolved = []
    if ledger.is_frozen:
        resolved = reconciler.resolve_ambiguities(cycle.client, ledger)

    report = order_monitor.poll_until_settled(
        cycle.client, ledger, expected_ccy=cycle.base_ccy,
        max_wait_sec=float(execution.get("settle_max_wait_min", 90)) * 60,
        poll_sec=float(execution.get("settle_poll_sec", 30)),
        stop_check=stop_check)

    # Reconcile against every ticker the BOOK holds, not only the configured
    # eighteen: a wide-universe strategy accumulates positions outside that
    # list, and a table that omits them reports a clean book that is not.
    trade_symbols = list(cycle.params["trade_symbols"])
    tickers = instruments.ticker_map_for(
        sorted(set(trade_symbols) | set(ledger.positions)))
    verdict = reconciler.reconcile(cycle.client, ledger, tickers)

    # The authoritative timing fills at the decision session's close. A fill
    # that arrived hours later came from the next session's open instead,
    # which is the timing the ruling did not choose; from that point the live
    # book is no longer comparable to the baseline. Raising the halt flag
    # only STOPS trading, never starts it, and clearing it is a manual act,
    # so stopping on the spot is the conservative response
    # (fixplans/t212/a0/02_execution.md section 8 item 4).
    breaches = order_monitor.fill_timing_breaches(ledger)
    if breaches and not cycle.halted():
        cycle.halt_path.parent.mkdir(parents=True, exist_ok=True)
        cycle.halt_path.touch()
        log.critical("[settle] fill timing breach, halt raised: %s", breaches)
        notify(f"{cycle.strategy_id} fill timing breach -- halted",
               f"{len(breaches)} fill(s) landed far after submission; "
               f"trading stops until the halt is cleared")
        ledger.record_note(str(pd.Timestamp.now(tz="UTC").value),
                           "FILL_TIMING_BREACH", {"breaches": breaches})

    if ledger.cash_gbp < 0:
        # The fill already happened at the venue; the book must record the
        # truth and alarm, never block. Negative strategy cash means the
        # strategy has spent account money outside its allocation.
        log.critical("[settle] ledger cash is NEGATIVE: %s", ledger.cash_gbp)
        notify(f"{cycle.strategy_id} strategy cash negative",
               f"book cash {ledger.cash_gbp} GBP; the strategy overspent "
               f"its allocation -- review before the next session")
        ledger.record_note(str(pd.Timestamp.now(tz="UTC").value),
                           "NEGATIVE_CASH", {"cash_gbp": str(ledger.cash_gbp)})

    return {"phase": "settle", "resolved_ambiguities": resolved,
            "settle": report.summary(),
            "still_open": sorted(ledger.open_orders),
            "frozen": ledger.is_frozen,
            "reconcile_ok": verdict.ok,
            "reconcile": verdict.summary(),
            "fill_timing_breaches": breaches,
            "halted": cycle.halted(),
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


def adopt_book(cfg: dict[str, Any], from_strategy_id: str,
               confirm: bool = False) -> dict[str, Any]:
    """Hand one strategy's cash and positions to the configured strategy.

    A0's live book holds real shares. Starting B0 with init_ledger would leave
    those shares owned by a retired book while B0 believed it held nothing,
    and the first reconciliation would report venue positions no book claims.
    This is the only supported path from one book to the other.

    Every precondition below exists because violating it loses money or
    truth, not because it is tidy:

      confirm     The account owner has to say so in this invocation. Moving
                  the ownership of live positions is not a routine command.
      no daemon   A running daemon may be inside a decision at this instant;
                  it would submit against the book it loaded before the
                  handover.
      quiet hour  Between the previous settle and the next decision key. Doing
                  this mid-session means a fill can arrive for an order the
                  old book owns while the new book already claims the shares.
      settled     The source has no open orders and is not frozen, enforced by
                  ShadowLedger.init_adopted.

    The source book is retired, not deleted: it is the only record of what
    that strategy did, and ledger_store.restore_ledger puts it back.
    """
    cycle = _Cycle(cfg)
    if not confirm:
        return _abort("adopt-book moves ownership of live positions; re-run "
                      "with --confirm")
    lock_path = cycle.state_dir / "daemon.lock"
    if _lock_is_held(lock_path):
        return _abort(f"the daemon holds {lock_path}; stop it before moving "
                      f"the book, or it will trade the book it already loaded")
    now = pd.Timestamp.now(tz="UTC")
    sessions_ = cycle.session_list()
    live = instruments.current_session(sessions_, now)
    if live is not None:
        return _abort(f"session {live.date_ny} is open; hand the book over "
                      f"between the close and the next decision key")
    try:
        source = ShadowLedger.load(cycle.state_dir, from_strategy_id)
    except (FileNotFoundError, LedgerFrozenError) as exc:
        return _abort(f"source book unavailable: {exc}")
    try:
        adopted = ShadowLedger.init_adopted(cycle.state_dir,
                                            cycle.strategy_id, source)
    except (FileExistsError, LedgerFrozenError, ValueError) as exc:
        return _abort(f"adoption refused: {exc}")

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    moved = ledger_store.retire_ledger(cycle.state_dir, from_strategy_id,
                                       stamp)
    tickers = instruments.ticker_map_for(sorted(adopted.positions))
    verdict = reconciler.reconcile(cycle.client, adopted, tickers)
    return {"phase": "adopt-book", "from": from_strategy_id,
            "to": cycle.strategy_id,
            "cash_gbp": str(adopted.cash_gbp),
            "positions": {s: str(q) for s, q in adopted.positions.items()},
            "retired": moved, "retired_stamp": stamp,
            "reconcile_ok": verdict.ok, "reconcile": verdict.summary(),
            "rollback": f"ledger_store.restore_ledger(state_dir, "
                        f"{from_strategy_id!r}, {stamp!r}) after retiring "
                        f"{cycle.strategy_id}"}


def _lock_is_held(path) -> bool:
    """Whether another process holds an exclusive flock on a lock file."""
    if not path.exists():
        return False
    try:
        with open(path, "a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def init_ledger(cfg: dict[str, Any], cash_gbp: Decimal,
                force: bool = False) -> dict[str, Any]:
    """Create the strategy's book with an explicit cash allocation.

    Refuses when the account already holds shares. A fresh book starts with no
    positions, so those shares end up owned by nothing: reconciliation would
    not catch it, because the position check is deliberately one-way (the
    account must hold AT LEAST what the book claims; excess is presumed manual
    and ignored). The strategy would then size fresh slots against cash alone,
    buy names it already owns, and never issue a sell for the orphaned ones.

    The supported path from one strategy's book to another is adopt_book,
    which carries the positions across. force is the deliberate override for
    the case where the venue holdings genuinely are not this strategy's.
    """
    cycle = _Cycle(cfg)
    if not force:
        try:
            held = [p for p in (cycle.client.positions() or [])
                    if Decimal(str(p.get("quantity", 0))) != 0]
        except Exception as exc:                   # noqa: BLE001
            return _abort(f"cannot read venue positions to check for orphans: "
                          f"{exc}; pass force=True only if you are sure")
        if held:
            return _abort(
                f"the account holds {len(held)} position(s) "
                f"({', '.join(sorted(str(p.get('ticker')) for p in held)[:6])}"
                f"...) and a fresh book would own none of them. Use "
                f"`run_a0 adopt-book --from <old_strategy_id> --confirm` to "
                f"carry them across, or pass force to create an empty book "
                f"anyway.")
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
        magnitude = abs(delta).quantize(risk_gate.qty_step(symbol),
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


def _stranded_holdings(cycle: _Cycle, ledger, view) -> list[str]:
    """Held names with no bar at all in the loaded window.

    Different from `thin`, which is "no bar at THIS session's decision key" and
    is an ordinary, recoverable condition. This is "no bar in eighty calendar
    days", which is what a delisting, a halt or a ticker change looks like. The
    position cannot be priced, so it cannot be sized, valued or sold, and
    nothing downstream will ever mention it again.
    """
    if not cycle.is_b0:
        return []
    return sorted(symbol for symbol, qty in ledger.positions.items()
                  if qty > 0 and view.bar(symbol) is None)


def _warn_if_buys_precede_sells(intents) -> bool:
    """Warn, without reordering, when a purchase is queued before a sale.

    The order comes from the strategy (decision A6) and the engine and the
    venue both consume it as given, so silently re-sorting here would hide a
    strategy defect and change which orders the cash check accepts relative to
    the backtest. Reporting it is the honest response: the batch still goes as
    the strategy asked, and the log says the ordering guarantee was not met.
    """
    first_buy = next((n for n, i in enumerate(intents) if i.quantity > 0), None)
    if first_buy is None:
        return False
    late_sell = next((i.symbol for i in intents[first_buy:] if i.quantity < 0),
                     None)
    if late_sell is None:
        return False
    log.warning("[decide] target order puts a buy before the sell of %s; "
                "the cash freed by that sell is not visible to the buys "
                "ahead of it", late_sell)
    return True


SUBMIT_SAFETY_SEC = 10


def _fit_before_close(intents, session, safety_sec: int = SUBMIT_SAFETY_SEC):
    """(capacity, seconds needed) for a batch against the runway to the close.

    The venue accepts market orders at RATE_LIMITS["order_market"] and
    common.net applies SAFETY_RATIO on top; 16 orders measured 26 seconds on
    2026-08-31, the same 0.58 per second. The runway is measured against the
    CLOSE, because that is the instant a market order has to reach; the margin
    is small on purpose, since SAFETY_RATIO already discounts the published
    ceiling by 30%.
    """
    rate = RATE_LIMITS["order_market"] * SAFETY_RATIO
    runway = (session.close_utc - pd.Timestamp.now(tz="UTC")).total_seconds()
    capacity = int(max(0.0, runway - safety_sec) * rate)
    needed = len(intents) / rate if rate > 0 else float("inf")
    return capacity, needed


def _record_rotation(cycle: _Cycle, session_id: str, diagnostics: dict,
                     injected) -> bool:
    """Persist the rotation the moment it is decided, before anything can abort.

    The a1_plan row is the ONLY memory of the A1 book: the buffer band is
    defined against it and positions are not a substitute. The rotation
    counter, meanwhile, is a pure function of the session list -- session zero
    is consumed by being reached, whether or not an order goes out.

    Writing both after submission therefore loses a rotation on every abort
    between the decision and the send: a closed risk gate, a passed submission
    instant, a halt raised while parked, an operator stopping the daemon
    inside the twenty-nine minute wait. The next session would then find no
    book, size the A1 half to nothing, and liquidate the names it had just
    chosen. Recording a rotation the session failed to execute is strictly
    safer than executing one it failed to record, and the row is keyed by date
    so a replay cannot duplicate it.

    Returns whether this session is a rotation.
    """
    if not cycle.is_b0 or not diagnostics:
        return False
    a1_tree = diagnostics.get("a1") or {}
    rebalance_tree = a1_tree.get("rebalance") or {}
    is_rebalance = rebalance_tree.get("sessions_until_next") == 0
    _save_rebalance_state(cycle, rebalance_tree)
    if not is_rebalance:
        return False
    book = [row for row in (a1_tree.get("book") or [])
            if row.get("status") != "exiting"]
    names = {row["symbol"] for row in book}
    previous = set((injected or {}).get("a1_book") or {})
    archive.record_a1_plan(cycle.records_root, {
        "rebalance_date": session_id,
        "strategy_id": cycle.strategy_id,
        "session_index": rebalance_tree.get("session_index"),
        "eligible_count": a1_tree.get("eligible_count"),
        "book": book,
        "dropped": sorted(previous - names),
        "added": sorted(names - previous),
        "rank_as_of": rebalance_tree.get("rank_as_of"),
        "universe_file": (cycle.params.get("a1_params") or {})
        .get("universe_file"),
        "code_version": f"{cycle.signal_name}_v"
        + cycle.signal_version.replace(".", "_"),
    })
    return True


def _record_b0_streams(cycle: _Cycle, session_id: str, diagnostics: dict,
                       injected) -> bool:
    """Write b0_allocation after submission; the rotation is already recorded.

    Keyed by decision_date, so replaying a session cannot duplicate the row.
    Built from the diagnostics captured BEFORE submission, because the
    statuses describe the book the decision saw.
    """
    if not cycle.is_b0 or not diagnostics or "allocation" not in diagnostics:
        return False
    allocation = diagnostics.get("allocation") or {}
    attribution = diagnostics.get("attribution") or {}
    a1_tree = diagnostics.get("a1") or {}
    rebalance_tree = a1_tree.get("rebalance") or {}
    is_rebalance = rebalance_tree.get("sessions_until_next") == 0

    archive.record_b0_allocation(cycle.records_root, {
        "decision_date": session_id,
        "strategy_id": cycle.strategy_id,
        "equity_gbp": allocation.get("equity_gbp"),
        "priority": diagnostics.get("priority"),
        "a0_names": allocation.get("a0_names"),
        "a1_names": allocation.get("a1_names"),
        "overlap": allocation.get("overlap"),
        "a0_target_gbp": allocation.get("a0_target_gbp"),
        "a1_target_gbp": allocation.get("a1_target_gbp"),
        "cash_target_gbp": allocation.get("cash_target_gbp"),
        "attribution": attribution.get("positions", {}),
        "a0_value_gbp": attribution.get("a0_value_gbp"),
        "a1_value_gbp": attribution.get("a1_value_gbp"),
        "cash_gbp": attribution.get("cash_gbp"),
    })

    return bool(is_rebalance)


def _save_rebalance_state(cycle: _Cycle, rebalance_tree: dict) -> None:
    """Cache the rotation counters so the dashboard need not recompute them.

    A CACHE, not the truth: the truth is the session list and the anchor, and
    both are pure functions the decision recomputes every time. A session the
    cycle aborted still advances the rotation, which is why nothing may be
    derived from this file's presence or absence.
    """
    if not rebalance_tree:
        return
    path = cycle.state_dir / "a1_rebalance_state.json"
    payload = {"anchor": rebalance_tree.get("anchor"),
               "session_index": rebalance_tree.get("session_index"),
               "every": rebalance_tree.get("every"),
               "sessions_until_next": rebalance_tree.get(
                   "sessions_until_next"),
               "last_rebalance": rebalance_tree.get("last_rebalance"),
               "rank_as_of": rebalance_tree.get("rank_as_of"),
               "rank_stale_sessions": rebalance_tree.get(
                   "rank_stale_sessions"),
               "written_utc": str(pd.Timestamp.now(tz="UTC"))}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".writing")
        tmp.write_text(json.dumps(payload, indent=1, default=str),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("[decide] could not cache the rotation state: %s", exc)


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
