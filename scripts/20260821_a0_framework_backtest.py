"""A0 and its comparison arms through the T212 backtest framework on daily bars.

Responsibility: entry layer for the ablation comparison. Load the single copy of
the A0 strategy from trading212/strategy/a0_v0_0_1.py together with its baseline
parameters, derive each comparison arm from those parameters by explicit
override, run every arm under both fee tiers, post-process holding-time and
turnover statistics from the fill records, and write one comparison table.

The arms are fixed up front and nothing is tuned against the output. Arm "a0" is
the true A0, that is tsmom252 with the volatility gate and the trend gate; arm
"tsmom" is tsmom252 alone with both gates off; arm "ma200" replaces the signal
with a per-symbol 200-day moving average and turns both gates off; arm "bh" is
always long with both gates off, which is an equal-slot buy and hold.

The engine window runs 2010-01-04 to 2026-08-19 with live_from at 2018-01-01, so
the state symbol has roughly 2000 bars of history inside the view by go-live,
which the volatility gate needs because it requires 756 volatility observations.
Metrics cover the whole run, but capital is occupied only from 2018.

Out of scope: the signal itself, whose only copy is
trading212/strategy/a0_v0_0_1.py; parameter values, which are external in
trading212/config/strategies/a0_v0_0_1.yaml; the engine, the cost model and the
metric definitions, which live under backtest/; the ruling drawn from these
numbers, which is research/decisions/20260821_a0_framework_comparison.md.

Public functions:
    holding_stats(trades)  Per-position holding durations derived from the fill
                           record. A position opens when a symbol's cumulative
                           quantity leaves zero and closes when it returns to
                           numerically zero; durations are calendar days between
                           the opening and closing fills, and positions still
                           open at the end of the run are counted separately
                           rather than folded into the closed-duration figures.
    main()                 Run every arm under both fee tiers, print the table,
                           and return the exit code.

Constants:
    ROOT           Path  Repository root, the parent of this file's directory.
    RESULTS_DIR    Path  backtest/results/, where the comparison table is
                         written.
    ARM_OVERRIDES  dict  Arm name mapped to the explicit parameter overrides
                         applied on top of the baseline yaml.
    FEE_TIERS      tuple Fee tiers run for every arm, "actual" and "worst".

Inputs:
    Command line: python scripts/20260821_a0_framework_backtest.py [--quick]
        --quick shortens the window to 2016-01-04 through 2019-12-31 with
        live_from 2019-01-02, drops PLTR because it lists in 2020 and an empty
        frame is a hard validation stop by design, and writes no files.
    trading212/config/strategies/a0_v0_0_1.yaml  Baseline strategy parameters.
    data/t212/curated/, through backtest.t212.data_source.
Outputs:
    backtest/results/a0_comparison_20260821.csv  One row per arm and fee tier.
    backtest/results/<run>.trades.parquet, <run>.equity.parquet and
        <run>.meta.json, written by the runner for every arm and tier.
    stdout carries the per-run progress lines and the final table.

Change log:
    2026-08-22  Header expanded to the six-section spec.
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
