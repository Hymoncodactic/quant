"""Walk-forward B0 study: select pairs, trade them, aggregate, score.

Responsibility: the driver. Slices the panel into non-overlapping
formation/trading windows, calls pairs.py to choose pairs on the formation
window and engine.py to trade them on the trading window, aggregates the pairs
equally into one portfolio per configuration, and writes the daily series and a
summary table. Nothing here estimates a parameter; every parameter comes from
the formation window through pairs.spread_parameters.

Protocol frozen in research/prereg/20260823_b0_statarb_prereg.md:
formation 12 months, trading 6 months, non-overlapping, rolling forward.

Public functions:
    windows(index, ...)   Non-overlapping (formation, trading) index slices.
    run_config(...)       One (method, variant, cost) configuration end to end.
    main()                Run the frozen grid and write results.

Constants:
    FORMATION_DAYS  int  252 trading days, the 12-month formation window.
    TRADING_DAYS    int  126 trading days, the 6-month trading window.
    TOP_N           int  20 pairs per window, the Gatev et al. (2006) top-20 rule.

Inputs:  data/t212/curated/us_equity/**, data/reference/b0_universe_20260823.json
Outputs: research/b0_statarb/results/*.csv

Change log:
    2026-08-23  Created for the B0 statistical-arbitrage study.
"""

from __future__ import annotations

__all__ = ["windows", "run_config", "main"]

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from research.b0_statarb import data as b0data                    # noqa: E402
from research.b0_statarb import engine as b0engine                # noqa: E402
from research.b0_statarb import pairs as b0pairs                  # noqa: E402

RESULTS = HERE / "results"
FORMATION_DAYS = 252
TRADING_DAYS = 126
TOP_N = 20


def windows(index: pd.Index, formation: int = FORMATION_DAYS,
            trading: int = TRADING_DAYS):
    """Yield (formation_slice, trading_slice) with no overlap between them."""
    start = 0
    while start + formation + trading <= len(index):
        yield (index[start:start + formation],
               index[start + formation:start + formation + trading])
        start += trading


def run_config(closes: pd.DataFrame, groups: dict[str, str], method: str,
               variant: str, cost_bps: float, top_n: int = TOP_N,
               within_group: bool = True, entry: float = b0engine.ENTRY_Z,
               formation: int = FORMATION_DAYS,
               trading: int = TRADING_DAYS) -> tuple[pd.Series, list[dict]]:
    """Daily portfolio return and the per-window pair record."""
    chunks, record = [], []
    select = (b0pairs.select_cointegration if method == "coint"
              else b0pairs.select_distance)
    for form_idx, trade_idx in windows(closes.index, formation, trading):
        form = closes.loc[form_idx].dropna(axis=1, how="any")
        if form.shape[1] < 10:
            continue
        chosen = select(form, groups, top_n, within_group)
        if not chosen:
            continue
        legs = []
        for a, b, params in chosen:
            if method == "dist":
                params = {**params,
                          **b0pairs.spread_parameters(form[a], form[b], "dist")}
            trade = closes.loc[trade_idx, [a, b]].dropna()
            if len(trade) < 20:
                continue
            legs.append(b0engine.run_pair(trade, a, b, params, variant,
                                          method, cost_bps, entry))
        if not legs:
            continue
        book = pd.concat(legs, axis=1).mean(axis=1)
        chunks.append(book)
        record.append({"trade_start": str(trade_idx[0]),
                       "trade_end": str(trade_idx[-1]),
                       "pairs": len(legs),
                       "example": ", ".join(f"{a}/{b}" for a, b, _ in chosen[:3])})
    if not chunks:
        return pd.Series(dtype=float), record
    return pd.concat(chunks).sort_index(), record


def summarize(returns: pd.Series, label: str) -> dict:
    """Annualized statistics of a daily return series."""
    if returns.empty:
        return {"config": label, "days": 0}
    curve = (1.0 + returns).cumprod()
    peak = curve.cummax()
    n = len(returns)
    ann = float(returns.mean() * 252)
    vol = float(returns.std(ddof=1) * np.sqrt(252))
    # Newey-West(20) t statistic of the mean daily return.
    x = returns.to_numpy() - returns.mean()
    gamma0 = float((x * x).mean())
    lrv = gamma0
    for lag in range(1, 21):
        cov = float((x[lag:] * x[:-lag]).mean())
        lrv += 2.0 * (1.0 - lag / 21.0) * cov
    se = float(np.sqrt(max(lrv, 1e-18) / n))
    return {"config": label, "days": n,
            "ann_return": ann, "ann_vol": vol,
            "sharpe": ann / vol if vol > 0 else float("nan"),
            "max_drawdown": float(-(curve / peak - 1.0).min()),
            "total_return": float(curve.iloc[-1] - 1.0),
            "nw_t": float(returns.mean() / se) if se > 0 else float("nan")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default="2010-01-01")
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    universe = b0data.load_universe()
    closes, dollars = b0data.load_closes()
    keep = b0data.liquid_subset(closes, dollars)
    closes = closes.loc[closes.index >= pd.Timestamp(args.start).date(), keep]
    closes = closes.dropna(axis=1, thresh=int(len(closes) * 0.9))
    groups = {t: universe[t][1] for t in closes.columns if t in universe}
    print(f"panel {closes.shape[0]} days x {closes.shape[1]} names, "
          f"{len(set(groups.values()))} sub-industries", flush=True)

    methods = ["coint"] if args.quick else ["coint", "dist"]
    variants = ["MN", "L1", "L2"]
    costs = {"free": 0.0, "actual": b0engine.COST_BPS_ACTUAL,
             "worst": b0engine.COST_BPS_WORST}
    rows, series = [], {}
    for method, variant, (cname, cbps) in itertools.product(
            methods, variants, costs.items()):
        label = f"{method}|{variant}|{cname}"
        ret, record = run_config(closes, groups, method, variant, cbps)
        rows.append(summarize(ret, label))
        if not ret.empty:
            series[label] = ret
        print(f"  {label:24s} days={rows[-1].get('days',0):5d} "
              f"ann={rows[-1].get('ann_return',float('nan')):+.2%} "
              f"t={rows[-1].get('nw_t',float('nan')):+.2f}", flush=True)
        if cname == "actual" and record:
            json.dump(record, open(RESULTS / f"windows_{method}_{variant}.json",
                                   "w"), indent=1)

    table = pd.DataFrame(rows)
    table.to_csv(RESULTS / "summary.csv", index=False)
    if series:
        pd.DataFrame(series).to_csv(RESULTS / "daily_returns.csv")
    print("\n" + table.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print(f"\nwritten {RESULTS}/summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
