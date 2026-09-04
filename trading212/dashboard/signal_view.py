"""Assemble the live signal-versus-threshold view for the dashboard.

Responsibility: load the local bars and read-only injection the configured
strategy actually uses, run that strategy module's signal_diagnostics, and
blend delayed quotes into A0's signal subtree. Also serves the decided-signals
history from the records archive.

Caliber note, stated where it is computed: the diagnostics run on the
COMPLETED daily bars in the curated store (refreshed by strategy runs and
the daily update job), while the "live" distance per symbol re-evaluates
the same trigger with the delayed quote in place of the last close. That
preview is NOT the number the 15:30 decision will use -- the decision
rebuilds today's bar from fresh hourly data -- and the payload labels both
figures separately so the interface never conflates them.

Out of scope: the formulas (trading212/strategy/, single copy, reached via
its own signal_diagnostics); quote fetching (quotes.py via collector);
rendering (assets/app.js).

Public functions:
    live_signals(ctx, quotes)   The blended diagnostics payload.
    decided_history(ctx, limit) Recent rows of the signals record stream.

Constants:
    CACHE_SEC       float  30. Frame loading reads ~20 parquet series; a
                           browser polling faster than this re-reads for
                           nothing.

Inputs:
    data/t212/curated/ daily bars; trading212/records[/<env>]/signals.jsonl.
Outputs:
    None (pure reads).

Change log:
    2026-08-31  Created with the watch-and-signals dashboard panels.
    2026-09-04  Added the B0 S2/S3/S6 read-only path and preserved the A0
                subtree while applying delayed-quote margins.
"""

from __future__ import annotations

__all__ = ["live_signals", "decided_history", "CACHE_SEC"]

import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pandas as pd

from common.logging_setup import get_logger
from common.paths import records_dir
from trading212 import archive
from trading212.execution import market_data
from trading212.execution.strategy_loader import load_module

log = get_logger("t212.dashboard")

CACHE_SEC = 30.0

_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def _diagnostics(ctx) -> dict[str, Any]:
    """Run the configured strategy's local, read-only diagnostics, cached."""
    key = (ctx.env, ctx.strategy_id)
    held = _cache.get(key)
    if held is not None and time.monotonic() - held[0] < CACHE_SEC:
        return held[1]

    if ctx.signal_name == "b0":
        diag = _b0_diagnostics(ctx)
    else:
        diag = _a0_diagnostics(ctx)
    _cache[key] = (time.monotonic(), diag)
    return diag


def _a0_diagnostics(ctx) -> dict[str, Any]:
    """Run the original A0 diagnostics over completed daily bars."""

    params = dict(ctx.params)
    symbols = list(params["trade_symbols"]) + [params["state_symbol"]]
    now = pd.Timestamp.now(tz="UTC")
    # Same span the decision loads: the vol gate is an EXPANDING percentile
    # over everything since history_start, so a shorter load here would show
    # a different percentile than the one that actually gates trading.
    start = str((ctx.cfg.get("execution") or {})
                .get("history_start", "2010-01-04"))
    frames = market_data.load_frames(symbols, "1d", start, str(now.date()))
    # COMPLETED bars only: after a decision-time refresh the store can hold
    # today's in-progress daily bar (the vendor publishes it mid-session).
    # The panel's caliber is "as of the last completed close", so today's
    # exchange-local date is cut; the live quote supplies the intraday
    # number separately and is labeled as such.
    today_ny = now.tz_convert("America/New_York").date()
    completed = {}
    last_bar = None
    for symbol, frame in frames.items():
        if len(frame):
            local_days = frame["ts"].dt.tz_convert("America/New_York").dt.date
            frame = frame.loc[local_days < today_ny]
        completed[symbol] = frame
        if len(frame):
            tail = frame["ts"].iloc[-1]
            if last_bar is None or tail > last_bar:
                last_bar = tail
    view = market_data.build_view(completed, now)
    module = load_module(ctx.signal_name, ctx.signal_version)
    diag = module.signal_diagnostics(view, params)
    diag["as_of"] = str(last_bar) if last_bar is not None else None
    return diag


def _b0_diagnostics(ctx) -> dict[str, Any]:
    """Run B0 through seams S2, S3, and S6 without refresh or file writes."""
    params = ctx.params
    now = pd.Timestamp.now(tz="UTC")
    today_ny = now.tz_convert("America/New_York").date()
    sessions = market_data.us_sessions(params["history_start"], str(now.date()))
    completed = [session for session in sessions if session < today_ny]
    if not completed:
        raise FileNotFoundError("no completed US session is stored for B0")
    as_of = completed[-1]

    ledger = ctx.ledger()
    if ledger is None:
        portfolio = SimpleNamespace(
            cash_gbp=Decimal("0"), available_cash_gbp=Decimal("0"),
            positions={}, pending_signed_qty={})
        held_symbols: list[str] = []
    else:
        fee_buffer = Decimal(str((ctx.cfg.get("risk") or {})
                                 .get("fee_buffer", 0)))
        portfolio = ledger.portfolio_view(fee_buffer)
        held_symbols = list(ledger.positions)

    injection = market_data.load_b0_injection(
        params, as_of, held=held_symbols,
        records_root=records_dir("t212", ctx.env))
    decision_key = pd.Timestamp(
        f"{as_of.isoformat()} {params['decision_time_local']}",
        tz=params["exchange_tz"]).tz_convert("UTC")
    intraday_start = str((pd.Timestamp(as_of) - pd.Timedelta(
        days=market_data.INTRADAY_SESSIONS_LOADED * 2)).date())
    view_symbols = list(injection["view_symbols"])
    frames = market_data.load_frames(
        view_symbols, "1h", intraday_start, str(as_of), missing="skip")

    a0_params = params["a0_params"]
    required = set(a0_params["trade_symbols"]) | {
        a0_params["state_symbol"], params["fx_symbol"]}
    missing_required = sorted(required - set(frames))
    if missing_required:
        raise FileNotFoundError(
            f"no 1h partitions for B0 caliber symbols {missing_required}")
    absent = set(view_symbols) - set(frames)
    injection["thin"] = sorted(set(injection.get("thin") or []) | absent)

    view = market_data.build_view(frames, decision_key)
    module = load_module(ctx.signal_name, ctx.signal_version)
    return module.signal_diagnostics(view, portfolio, params, injection)


def live_signals(ctx, quotes: dict[str, dict] | None) -> dict[str, Any]:
    """Diagnostics plus a delayed-quote re-evaluation per symbol.

    For each symbol with a trigger price, the live margin is simply the
    quote against the same trigger -- the trigger itself moves only when a
    daily bar completes, so re-evaluating it with a fresher numerator is
    exact, not an approximation. The vol percentile has no meaningful
    intraday counterpart and is reported from completed bars only.
    """
    diag = _diagnostics(ctx)
    quotes = quotes or {}
    a0_params = ctx.params.get("a0_params") or ctx.params
    if ctx.signal_name == "b0":
        out = dict(diag)
        out["a0"] = _blend_a0_quotes(
            diag.get("a0") or {}, quotes,
            a0_params.get("state_symbol", "QQQ"))
        return out
    return _blend_a0_quotes(
        diag, quotes, a0_params.get("state_symbol", "QQQ"))


def _blend_a0_quotes(diag: dict[str, Any], quotes: dict[str, dict],
                     state_symbol: str) -> dict[str, Any]:
    """Copy one A0 diagnostics tree and add delayed-quote margins."""
    out = dict(diag)
    out["gates"] = dict(diag.get("gates") or {})
    out["symbols"] = {}

    state_quote = quotes.get(state_symbol) or {}
    trend = dict((out["gates"].get("trend") or {}))
    if trend.get("ma") and state_quote.get("ok") \
            and state_quote.get("price"):
        trend["live_price"] = float(state_quote["price"])
        trend["live_margin_pct"] = (trend["live_price"] / trend["ma"]
                                    - 1.0) * 100.0
    out["gates"]["trend"] = trend

    for symbol, row in (diag.get("symbols") or {}).items():
        entry = dict(row)
        quote = quotes.get(symbol) or {}
        trigger = row.get("trigger")
        if trigger and quote.get("ok") and quote.get("price"):
            entry["live_price"] = float(quote["price"])
            entry["live_margin_pct"] = (entry["live_price"] / trigger
                                        - 1.0) * 100.0
            entry["quote_age_sec"] = quote.get("age_sec")
        out["symbols"][symbol] = entry
    return out


def decided_history(ctx, limit: int = 20) -> list[dict[str, Any]]:
    """The most recent decided sessions from the signals record stream."""
    root = records_dir("t212", ctx.env)
    try:
        rows = archive.read_stream(root, "signals", limit=limit)
    except Exception as exc:
        log.warning("[signals] history read failed: %r", exc)
        return []
    return rows
