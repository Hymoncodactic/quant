"""A0 versus comparison arms through the T212 backtest framework, daily bars.

Entry layer for the comparison the user ordered on 2026-08-20: load the A0
strategy module (single copy, trading212/strategy/a0_v0_0_1.py), derive the
ablation arms from its baseline yaml with explicit overrides, run every arm
under both fee tiers, and post-process holding-time and turnover statistics
from the fill records.

Arms (fixed up front; no tuning against the output):
    a0        the true A0: tsmom252 + vol gate + trend gate
    tsmom     tsmom252 only, both gates off
    ma200     per-symbol 200-day MA, both gates off
    bh        always long, both gates off (equal-slot buy and hold)

Window: engine 2010-01-04 .. 2026-08-19 with live_from 2018-01-01, so the
state-symbol history inside the view is ~2000 bars by go-live (the vol gate
needs 756 vol observations); metrics cover the whole run but capital is only
occupied from 2018.

Usage:
    python scripts/20260821_a0_framework_backtest.py [--quick]
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.engine.strategy_loader import load_strategy          # noqa: E402
from backtest.engine.types import EngineConfig                     # noqa: E402
from backtest.t212.runner import run_t212_backtest                 # noqa: E402

RESULTS_DIR = ROOT / "backtest" / "results"

ARM_OVERRIDES: dict[str, dict] = {
    "a0": {},
    "tsmom": {"use_vol_gate": False, "use_trend_gate": False},
    "ma200": {"signal_mode": "ma200", "use_vol_gate": False,
              "use_trend_gate": False},
    "bh": {"signal_mode": "always", "use_vol_gate": False,
           "use_trend_gate": False},
}

FEE_TIERS = ("actual", "worst")


def holding_stats(trades: pd.DataFrame) -> dict:
    """Per-position holding durations from the fill record.

    A position opens when a symbol's cumulative quantity leaves zero and
    closes when it returns to (numerically) zero. Durations are calendar days
    between the opening and closing fills. Open positions at the end of the
    run are excluded from the closed-duration statistics and counted.
    """
    if trades.empty:
        return {"positions_closed": 0, "positions_open_at_end": 0,
                "hold_days_mean": None, "hold_days_median": None,
                "hold_days_p90": None, "hold_days_max": None}
    durations: list[float] = []
    open_at_end = 0
    eps = Decimal("0.000001")
    for symbol, group in trades.groupby("symbol"):
        group = group.sort_values(["step", "order_id"])
        qty = Decimal("0")
        opened_ts = None
        for row in group.itertuples(index=False):
            before = qty
            qty += Decimal(str(row.quantity))
            ts = pd.Timestamp(row.ts)
            if before.copy_abs() <= eps and qty.copy_abs() > eps:
                opened_ts = ts
            elif before.copy_abs() > eps and qty.copy_abs() <= eps:
                if opened_ts is not None:
                    durations.append((ts - opened_ts).total_seconds() / 86400.0)
                opened_ts = None
        if qty.copy_abs() > eps:
            open_at_end += 1
    arr = pd.Series(durations, dtype=float)
    return {
        "positions_closed": int(len(arr)),
        "positions_open_at_end": open_at_end,
        "hold_days_mean": float(arr.mean()) if len(arr) else None,
        "hold_days_median": float(arr.median()) if len(arr) else None,
        "hold_days_p90": float(arr.quantile(0.9)) if len(arr) else None,
        "hold_days_max": float(arr.max()) if len(arr) else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true",
                        help="short window smoke run, no files written")
    args = parser.parse_args()

    base_params = yaml.safe_load(
        (ROOT / "trading212" / "config" / "strategies" / "a0_v0_0_1.yaml")
        .read_text())
    strategy = load_strategy("t212", "a0", "0.0.1")

    start, end = "2010-01-04", "2026-08-19"
    live_from = base_params["live_from"]
    if args.quick:
        start, end, live_from = "2016-01-04", "2019-12-31", "2019-01-02"
        # PLTR lists 2020; an empty frame is a hard validation stop by design.
        base_params["trade_symbols"] = [
            s for s in base_params["trade_symbols"] if s != "PLTR"]

    feed_symbols = list(base_params["trade_symbols"]) + [
        base_params["state_symbol"], base_params["fx_symbol"]]

    rows = []
    for arm, overrides in ARM_OVERRIDES.items():
        params = copy.deepcopy(base_params)
        params.update(overrides)
        params["live_from"] = live_from
        for tier in FEE_TIERS:
            config = EngineConfig(
                symbols=feed_symbols, interval="1d", start=start, end=end,
                initial_cash_gbp=Decimal("10000"),
                arm=f"{arm}_{tier}", fee_tier=tier,
                strategy_name="a0", strategy_version="0.0.1", params=params)
            result, metrics, paths = run_t212_backtest(
                config, strategy, write=not args.quick)
            live_days = None
            if not result.equity.empty:
                eq = result.equity
                occupied = eq[eq["occupied_gbp"] > 0]
                if len(occupied):
                    span = (pd.Timestamp(occupied["ts"].iloc[-1])
                            - pd.Timestamp(occupied["ts"].iloc[0]))
                    live_days = span.days
            row = {"arm": arm, "fee_tier": tier, **metrics,
                   **holding_stats(result.trades),
                   "live_span_days": live_days}
            if live_days and metrics.get("turnover_both_legs_on_capital"):
                row["turnover_both_legs_annualized"] = (
                    metrics["turnover_both_legs_on_capital"]
                    / (live_days / 365.25))
            rows.append(row)
            print(f"done {arm}/{tier}: ann="
                  f"{metrics.get('annualized_return_rate')}, "
                  f"maxdd={metrics.get('max_drawdown_on_capital')}, "
                  f"fills={metrics.get('fills')}", flush=True)

    table = pd.DataFrame(rows)
    out = RESULTS_DIR / "a0_comparison_20260821.csv"
    if not args.quick:
        table.to_csv(out, index=False)
        print(f"\nwritten {out}")
    columns = ["arm", "fee_tier", "annualized_return_rate",
               "total_return_rate_on_capital", "max_drawdown_on_capital",
               "sharpe_rf0", "calmar", "win_rate", "fills", "closed_trades",
               "turnover_both_legs_annualized", "hold_days_mean",
               "hold_days_median", "hold_days_p90",
               "positions_open_at_end", "online_trading_days",
               "capital_peak_occupied_gbp"]
    present = [c for c in columns if c in table.columns]
    print(table[present].to_string(index=False,
                                   float_format=lambda v: f"{v:.4f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
