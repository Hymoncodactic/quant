"""Read-only live probes and rehearsals against the Trading 212 account.

Responsibility: exercise every part of the live path that can be exercised
WITHOUT submitting an order, and measure the numbers the operator needs
before and during a live session: request latency per endpoint, realized
cost per trade from the archived fills, instrument tradability and
precision, clock agreement with the venue, and a full decision rehearsal at
a chosen session's decision instant.

Out of scope: submitting, canceling or modifying any order (this file never
calls an order endpoint -- grep for place_market_order returns nothing);
the live decision itself, which belongs to trading212/execution/run_a0.py;
unit tests, which belong to tests/ and must not reach the network.

Public functions:
    probe_latency(client, rounds)      Per-endpoint latency distribution.
    probe_clock(client, rounds)        Local clock versus the venue's clock.
    probe_instruments(client, params)  Universe tradability and precision.
    probe_costs()                      Realized cost per fill from records/.
    rehearse(cfg, session_date)        Full dry-run decision on real data.
    drills(cfg)                        Emergency-stop and recovery drills.

Parameters / Constants:
    DEFAULT_ROUNDS   int  6. Latency samples per endpoint. The venue meters
                          account_summary at one request per five seconds,
                          so more rounds mostly measure our own pacing.
    COST_SAMPLE_MIN  int  20. Below this many usable fills the realized-cost
                          summary is reported as not decisive.

Inputs:
    The venue's read endpoints; trading212/records/orders.jsonl;
    data/t212/curated/ bars; trading212/config/t212.<env>.yaml.
Outputs:
    A JSON report on stdout. Writes nothing under the project directory
    except, in rehearsal mode, the market-data refresh that the live cycle
    performs anyway (data/t212/curated/).

Change log:
    2026-08-29  Created for the pre-live test batches; the trading-hours
                procedure that drives it is tests/live/01_session_test_plan.md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from common.config import load_config
from common.logging_setup import get_logger
from common.paths import records_dir
from trading212.client import T212Client
from trading212.execution import (instruments, market_data, order_router,
                                  risk_gate, session_cycle)
from trading212.execution.shadow_ledger import ShadowLedger
from trading212.execution.strategy_loader import load_intraday_strategy

log = get_logger("t212.probe")

DEFAULT_ROUNDS = 6
COST_SAMPLE_MIN = 20


# ============================================================================
# [1] Latency
# ============================================================================

def probe_latency(client: T212Client, rounds: int = DEFAULT_ROUNDS
                  ) -> dict[str, Any]:
    """Wall-clock latency per read endpoint, as the live cycle experiences it.

    Every sample includes the token bucket's pacing, because that is the
    real cost inside a decision window. min_ms is the closest thing to a
    pure network-plus-venue figure: it is the sample where a token was
    already available. Sample counts differ per endpoint because the venue
    meters them differently -- hammering the 50-second instruments endpoint
    to fill a histogram would burn the budget the live cycle needs.
    """
    calls = {
        "positions": (lambda: client.positions(), rounds),
        "pending_orders": (lambda: client.pending_orders(), max(2, rounds // 2)),
        "account_summary": (lambda: client.account_summary(),
                            max(2, rounds // 2)),
        "exchanges": (lambda: client.exchanges(), 2),
        "instruments": (lambda: client.instruments(), 1),
    }
    out: dict[str, Any] = {}
    for name, (call, count) in calls.items():
        paced: list[float] = []
        errors: list[str] = []
        for _ in range(count):
            started = time.perf_counter()
            try:
                call()
            except Exception as exc:  # a probe reports failures, never dies
                errors.append(repr(exc)[:160])
            paced.append((time.perf_counter() - started) * 1000.0)
        out[name] = _summarize_ms(paced) | {
            "errors": errors,
            "venue_meter_per_sec": _RATE_HINT.get(name)}
    return out


_RATE_HINT = {"account_summary": "1/5s", "positions": "1/1s",
              "pending_orders": "1/5s", "exchanges": "1/30s",
              "instruments": "1/50s"}


def _summarize_ms(samples: list[float]) -> dict[str, Any]:
    ordered = sorted(samples)
    return {"n": len(ordered),
            "min_ms": round(ordered[0], 1),
            "median_ms": round(statistics.median(ordered), 1),
            "max_ms": round(ordered[-1], 1)}


# ============================================================================
# [2] Clock
# ============================================================================

def probe_clock(client: T212Client, rounds: int = 3) -> dict[str, Any]:
    """Local clock minus venue clock, sampled a few times.

    The submit instant sits 60 seconds before the close with 30 seconds of
    slack, and session_cycle refuses to decide beyond
    MAX_CLOCK_SKEW_SEC of drift, so this is the reading that gate acts on.
    """
    skews: list[float] = []
    for _ in range(rounds):
        client.account_summary()
        skew = client.last_clock_skew_sec()
        if skew is not None:
            skews.append(skew)
        time.sleep(5.5)  # account_summary is metered at one per five seconds
    if not skews:
        return {"measured": False,
                "note": "no Date header seen; the gate treats this as "
                        "unevaluable and does not block on it"}
    return {"measured": True,
            "samples_sec": [round(s, 2) for s in skews],
            "median_sec": round(statistics.median(skews), 2),
            "bound_sec": session_cycle.MAX_CLOCK_SKEW_SEC,
            "would_block": abs(statistics.median(skews))
            > session_cycle.MAX_CLOCK_SKEW_SEC}


# ============================================================================
# [3] Instruments
# ============================================================================

def probe_instruments(client: T212Client, params: dict[str, Any]
                      ) -> dict[str, Any]:
    """Every traded symbol resolves, is tradable, and its precision is known.

    A symbol that fails here cannot be ordered on the live day, and the
    failure would otherwise surface for the first time inside the 29-minute
    decision window.
    """
    symbols = list(params["trade_symbols"])
    try:
        mapped = instruments.validate_mapping(client, symbols)
    except Exception as exc:
        return {"ok": False, "problem": repr(exc)[:300]}

    rows = {}
    for symbol in symbols:
        meta = mapped.get(symbol) or {}
        rows[symbol] = {
            "ticker": instruments.order_ticker(symbol),
            "name": meta.get("name"),
            "currency": meta.get("currencyCode"),
            "max_open_qty": meta.get("maxOpenQuantity"),
            "type": meta.get("type"),
            "schedule_id": meta.get("workingScheduleId"),
            "extended_hours": meta.get("extendedHours"),
        }
    missing = [s for s, r in rows.items() if r["name"] is None]
    # The session model derives the decision key and close from ONE schedule
    # (US_SCHEDULE_ID_NASDAQ). Symbols on other schedules are fine as long
    # as those schedules AGREE with it session by session -- the same test
    # decide() runs through instruments.schedule_divergences. Different ids
    # alone are informational; an actual divergence fails the probe.
    other_schedules = {s: r["schedule_id"] for s, r in rows.items()
                       if r["schedule_id"] != instruments.US_SCHEDULE_ID_NASDAQ}
    divergences: list[str] = []
    if other_schedules:
        try:
            calendar = client.exchanges()
            schedule_ids = {r["schedule_id"] for r in rows.values()
                            if r["schedule_id"] is not None}
            upcoming = instruments.sessions(instruments.session_events(
                calendar, instruments.US_SCHEDULE_ID_NASDAQ))
            for sess in upcoming[-5:]:
                divergences += instruments.schedule_divergences(
                    calendar, schedule_ids, sess.date_ny)
        except Exception as exc:
            divergences = [f"divergence check itself failed: {exc!r}"]
    non_usd = {s: r["currency"] for s, r in rows.items()
               if r["currency"] != "USD"}
    return {
        "ok": not missing and not divergences and not non_usd,
        "count": len(rows), "unresolved": missing,
        "other_schedules": other_schedules,
        "schedule_divergences": divergences, "non_usd": non_usd,
        "schedule_expected": instruments.US_SCHEDULE_ID_NASDAQ,
        "min_quantity_published": False,
        "note": "the venue publishes maxOpenQuantity only; minimum order "
                "value and quantity precision are NOT in the API and stay "
                "unverified until a live order tests them",
        "rows": rows}


# ============================================================================
# [4] Realized cost from the archive
# ============================================================================

def probe_costs() -> dict[str, Any]:
    """Realized per-trade cost from the archived fills, as basis points.

    Cost is measured the way the account actually pays it: the itemized
    charges on each fill divided by the fill's gross value. Charges arrive
    NEGATIVE in walletImpact.taxes (they reduce the wallet), and the FX
    fee IS itemized there as CURRENCY_CONVERSION_FEE -- so the venue's own
    numbers, not a model, are what this reports. Figures are split by
    market because the charge set differs: a US equity pays the currency
    conversion fee on a GBP account, a UK share additionally pays stamp
    duty. History, not a forecast, so it is reported per charge type and
    per market rather than as one blended number.
    """
    path = records_dir("t212") / "orders.jsonl"
    if not path.exists():
        return {"ok": False, "problem": f"no archive at {path}"}

    tax_totals: dict[str, Decimal] = {}
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        fill = record.get("fill") or {}
        impact = fill.get("walletImpact") or {}
        gross = impact.get("netValue")
        if gross in (None, 0):
            continue
        taxes = impact.get("taxes") or []
        charged = Decimal("0")
        for tax in taxes:
            name = tax.get("name") or "UNKNOWN"
            amount = Decimal(str(tax.get("quantity") or 0))
            charged += amount
            tax_totals[name] = tax_totals.get(name, Decimal("0")) + amount
        gross_dec = Decimal(str(gross))
        ticker = record.get("ticker") or ""
        rows.append({
            "ticker": ticker,
            "market": "us" if ticker.endswith("_US_EQ") else "other",
            "side": record.get("side"),
            "gross_gbp": gross_dec,
            "charge_gbp": -charged,  # positive = money paid out
            "bps": (-charged / gross_dec * Decimal("10000"))
            if gross_dec else None,
            "charges": {t.get("name"): str(t.get("quantity"))
                        for t in taxes},
            "fx_rate": impact.get("fxRate"),
            "filled_at": fill.get("filledAt"),
        })

    priced = [r for r in rows if r["bps"] is not None]
    if len(priced) < COST_SAMPLE_MIN:
        return {"ok": False, "decisive": False, "fills": len(rows),
                "problem": f"only {len(priced)} usable fills"}

    def _bps_block(subset: list[dict]) -> dict[str, Any]:
        if not subset:
            return {"n": 0}
        vals = sorted(float(r["bps"]) for r in subset)
        return {"n": len(vals),
                "min": round(vals[0], 2),
                "median": round(statistics.median(vals), 2),
                "p90": round(vals[min(int(len(vals) * 0.9), len(vals) - 1)], 2),
                "max": round(vals[-1], 2)}

    us = [r for r in priced if r["market"] == "us"]
    charged_any = [r for r in priced if r["charge_gbp"] != 0]
    free = [r for r in priced if r["charge_gbp"] == 0]
    us_charge_names: dict[str, int] = {}
    for row in us:
        for name in row["charges"]:
            us_charge_names[name] = us_charge_names.get(name, 0) + 1
    return {
        "ok": True,
        "fills_total": len(rows),
        "fills_priced": len(priced),
        "fills_charged": len(charged_any),
        "fills_free_of_charge": len(free),
        "all_fills_bps": _bps_block(priced),
        "us_equity_fills_bps": _bps_block(us),
        "us_equity_buy_bps": _bps_block([r for r in us if r["side"] == "BUY"]),
        "us_equity_sell_bps": _bps_block([r for r in us if r["side"] == "SELL"]),
        "us_equity_charge_types": us_charge_names,
        "charge_totals_gbp": {k: str(-v) for k, v in sorted(tax_totals.items())},
        "note": "bps are charges actually deducted, positive = paid. The "
                "currency conversion fee IS itemized by the venue; a zero "
                "charge means the fill needed no conversion (already GBP).",
    }


# ============================================================================
# [5] Decision rehearsal
# ============================================================================

def rehearse(cfg: dict[str, Any], session_date: str | None = None
             ) -> dict[str, Any]:
    """Walk the whole decision chain on real data without submitting.

    Every stage the live cycle runs is run here in order -- calendar,
    market-data refresh and freshness gate, strategy, target diff, risk
    gate, router in dry run -- against the real book and the real account.
    The submit window gate is forced open so the rehearsal can be run
    outside trading hours; nothing else is relaxed, and the router is in
    dry run so no order endpoint is reachable from this path.
    """
    cycle = session_cycle._Cycle(cfg)
    started = time.perf_counter()
    stages: dict[str, Any] = {}

    sessions = cycle.session_list()
    if session_date:
        session = instruments.session_on(
            sessions, pd.Timestamp(session_date).date())
    else:
        session = instruments.last_full_session(
            sessions, pd.Timestamp.now(tz="UTC"))
    if session is None:
        return {"ok": False, "problem": f"no session for {session_date}"}
    key = instruments.decision_key(session)
    stages["session"] = {"date_ny": str(session.date_ny),
                         "decision_key_utc": str(key),
                         "close_utc": str(session.close_utc),
                         "is_full": session.is_full}

    t0 = time.perf_counter()
    try:
        view, history, thin = session_cycle._assemble_market(
            cycle, session, key, list(cycle.params["trade_symbols"]),
            cycle.params["state_symbol"], cycle.params["fx_symbol"])
    except Exception as exc:
        return {"ok": False, "stage": "market_data", "problem": repr(exc)[:400],
                "stages": stages}
    stages["market_data"] = {"seconds": round(time.perf_counter() - t0, 1),
                             "symbols_without_decision_bar": thin}

    ledger = cycle.ledger()
    fee_buffer = Decimal(str((cfg.get("risk") or {}).get("fee_buffer", "0")))
    portfolio = ledger.portfolio_view(fee_buffer)
    stages["book"] = {"cash_gbp": str(portfolio.cash_gbp),
                      "available_gbp": str(portfolio.available_cash_gbp),
                      "positions": {s: str(q)
                                    for s, q in portfolio.positions.items()}}

    t0 = time.perf_counter()
    strategy = load_intraday_strategy(cycle.shim_name, cycle.shim_version,
                                      history)
    targets = strategy(view, portfolio, cycle.params)
    stages["strategy"] = {"seconds": round(time.perf_counter() - t0, 2),
                          "targets": {s: str(q) for s, q in targets.items()}}
    if not targets:
        stages["strategy"]["note"] = ("no targets: the shim's own decision "
                                      "gate did not fire at this key")

    intents = session_cycle._diff_to_intents(cycle, targets, ledger, view,
                                             session)
    held = session_cycle._positions_ref_notional(ledger, view, cycle.params)
    stages["intents"] = [{"symbol": i.symbol, "ticker": i.ticker,
                          "quantity": str(i.quantity),
                          "ref_price_usd": str(i.ref_price_usd),
                          "fx_usd_per_gbp": str(i.fx_usd_per_gbp),
                          "ref_notional_gbp": str(round(i.ref_notional_gbp, 2))}
                         for i in intents]

    gate = risk_gate.check_intents(
        intents, portfolio, held, cfg.get("risk") or {}, orders_today=0,
        in_submit_window=True, halt_path=cycle.halt_path)
    stages["risk_gate"] = {
        "closed": gate.closed, "summary": gate.summary(),
        "approved": [i.symbol for i in gate.approved],
        "rejected": [{"symbol": i.symbol, "reason": r}
                     for i, r in gate.rejected]}

    report = order_router.submit_intents(
        gate.approved, ledger, client=None,
        decision_day=pd.Timestamp(session.date_ny), dry_run=True, armed=False,
        halt_path=cycle.halt_path)
    stages["router_dry_run"] = report.summary()

    planned = sum(abs(i.ref_notional_gbp) for i in gate.approved)
    stages["totals"] = {
        "approved_orders": len(gate.approved),
        "planned_notional_gbp": str(round(planned, 2)),
        "buys": len([i for i in gate.approved if i.quantity > 0]),
        "sells": len([i for i in gate.approved if i.quantity < 0]),
        "whole_rehearsal_seconds": round(time.perf_counter() - started, 1)}
    return {"ok": True, "stages": stages}


# ============================================================================
# [6] Drills
# ============================================================================

def drills(cfg: dict[str, Any]) -> dict[str, Any]:
    """Emergency-stop and crash-recovery drills on a throwaway book.

    The halt drill uses the real flag path because that is the thing being
    tested; it is raised and cleared inside this function and the final
    state is asserted. The ledger drills run on a temporary directory so
    the real book is never touched.
    """
    import shutil
    import tempfile

    out: dict[str, Any] = {}
    cycle = session_cycle._Cycle(cfg)

    # --- halt flag: raise, observe the gate close, clear -----------------
    already = cycle.halt_path.exists()
    if already:
        out["halt"] = {"skipped": "a halt flag is already present; a drill "
                                  "must not clear an operator's halt"}
    else:
        cycle.halt_path.parent.mkdir(parents=True, exist_ok=True)
        cycle.halt_path.touch()
        blocked = risk_gate.halt_active(cycle.halt_path)
        decided = session_cycle.decide(cfg, armed=False)
        cycle.halt_path.unlink()
        out["halt"] = {
            "gate_saw_flag": blocked,
            "decide_aborted": bool(decided.get("aborted")),
            "abort_reason": decided.get("reason"),
            "flag_cleared_after_drill": not cycle.halt_path.exists()}

    # --- dangling intent: freeze, and stay frozen without evidence -------
    tmp = Path(tempfile.mkdtemp(prefix="a0-drill-"))
    try:
        book = ShadowLedger.init_fresh(tmp, "a0_v0_0_1", Decimal("1000"))
        book.record_intent("drill|NVDA", "NVDA", "NVDA_US_EQ", Decimal("2"),
                           Decimal("175"), Decimal("1.35"), dry_run=False)
        frozen = book.freeze_dangling_live_intents()
        reloaded = ShadowLedger.load(tmp, "a0_v0_0_1")
        out["crash_recovery"] = {
            "intent_frozen": frozen,
            "book_frozen_after_reload": reloaded.is_frozen,
            "ambiguity_anchored_at_intent_time":
                reloaded.ambiguous_intents["drill|NVDA"]["at"]
                == _first_intent_ts(tmp, "a0_v0_0_1"),
            "new_intents_refused": _refuses_new_intent(reloaded)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- torn journal tail is detected, not silently absorbed -----------
    tmp = Path(tempfile.mkdtemp(prefix="a0-drill-"))
    try:
        ShadowLedger.init_fresh(tmp, "a0_v0_0_1", Decimal("1000"))
        journal = tmp / "a0_v0_0_1_journal.jsonl"
        with open(journal, "a", encoding="utf-8") as handle:
            handle.write('{"ts_utc": "2026-08-29T00:00:00+00:00", "event_id"')
        detected = False
        try:
            ShadowLedger.load(tmp, "a0_v0_0_1")
        except Exception as exc:
            detected = "json" in repr(exc).lower() or "journal" in repr(exc).lower()
        out["torn_journal_detected"] = detected
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return out


def _first_intent_ts(state_dir: Path, strategy_id: str) -> str | None:
    from trading212.execution.ledger_store import iter_journal
    for record in iter_journal(state_dir, strategy_id):
        if record.get("event_type") == "ORDER_INTENT":
            return record.get("ts_utc")
    return None


def _refuses_new_intent(book) -> bool:
    try:
        book.record_intent("drill|AAPL", "AAPL", "AAPL_US_EQ", Decimal("1"),
                           Decimal("200"), Decimal("1.35"), dry_run=False)
        return False
    except Exception:
        return True


# ============================================================================
# [7] CLI
# ============================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="live_probe", description="Read-only live probes; never orders")
    parser.add_argument("mode", choices=["latency", "clock", "instruments",
                                         "costs", "rehearse", "drills", "all"])
    parser.add_argument("--session", default=None,
                        help="rehearsal session date, YYYY-MM-DD (NY)")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    args = parser.parse_args(argv)

    cfg = load_config("t212")
    report: dict[str, Any] = {"env": cfg["_env"], "mode": args.mode,
                              "at_utc": str(pd.Timestamp.now(tz="UTC"))}
    needs_client = args.mode in ("latency", "clock", "instruments", "all")
    client = None
    try:
        if needs_client:
            client = T212Client(cfg["_env"], cfg=cfg,
                                secret_name=(cfg.get("endpoints") or {})
                                .get("secret_name", "trading212_api_key"))
        if args.mode in ("latency", "all"):
            report["latency"] = probe_latency(client, args.rounds)
        if args.mode in ("clock", "all"):
            report["clock"] = probe_clock(client)
        if args.mode in ("instruments", "all"):
            params = session_cycle._Cycle(cfg).params
            report["instruments"] = probe_instruments(client, params)
        if args.mode in ("costs", "all"):
            report["costs"] = probe_costs()
        if args.mode in ("rehearse", "all"):
            report["rehearsal"] = rehearse(cfg, args.session)
        if args.mode in ("drills", "all"):
            report["drills"] = drills(cfg)
    finally:
        if client is not None:
            client.close()

    print(json.dumps(report, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
