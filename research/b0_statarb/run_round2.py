"""B0 round 2: S&P Composite 1500 universe, per-name spreads, capacity gates.

Responsibility: the round-2 driver. Extends round 1 (research/b0_statarb/
run_study.py, large caps only, flat 32bp cost) in exactly three ways, all
frozen in research/prereg/20260823_b0_round2_prereg.md:

    R2-1  Universe is the S&P Composite 1500 intersected with Trading 212,
          1,500 names across three market-cap tiers, instead of 502 large caps.
    R2-2  Costs are per name: each leg pays its own Corwin-Schultz half spread
          plus the 15bp Trading 212 FX fee, instead of a flat 32bp round trip.
    R2-3  Capacity gates are applied BEFORE any return is computed, so no gate
          can be tuned against performance.

Round 1's driver is left untouched so its result stays reproducible.

Public functions:
    build_liquidity()   Per-name liquidity table for the whole universe.
    main()              Gate, run the grid by cap tier, write results.

Inputs:  data/t212/curated/us_equity/**,
         data/reference/b0_universe_1500_20260823.json
Outputs: research/b0_statarb/results/round2_*.csv

Change log:
    2026-08-23  Created for the small-cap extension and capacity verification.
"""

from __future__ import annotations

__all__ = ["build_liquidity", "evaluate_tier", "main"]

import argparse
import glob
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from research.b0_statarb import engine as b0engine                # noqa: E402
from research.b0_statarb import liquidity as b0liq                # noqa: E402
from research.b0_statarb.run_study import run_config, summarize   # noqa: E402

RESULTS = HERE / "results"
UNIVERSE_JSON = ROOT / "data" / "reference" / "b0_universe_1500_20260823.json"
CURATED = ROOT / "data" / "t212" / "curated" / "us_equity"
NY = "America/New_York"
START = "2010-01-01"


def _load_frame(ticker: str) -> pd.DataFrame | None:
    parts = sorted(glob.glob(str(CURATED / ticker / "1d" / "*.parquet")))
    if not parts:
        return None
    frame = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    frame["date"] = pd.to_datetime(frame["ts"], utc=True).dt.tz_convert(NY).dt.date
    frame = frame.sort_values("date").drop_duplicates("date", keep="last")
    frame = frame[frame["date"] >= pd.Timestamp(START).date()]
    return frame if len(frame) else None


def build_liquidity(members: list[dict]) -> tuple[pd.DataFrame, dict, dict]:
    """Liquidity table, close panel and the ticker to cap-tier map."""
    frames, closes = {}, {}
    for member in members:
        ticker = member["ticker"]
        frame = _load_frame(ticker)
        if frame is None:
            continue
        frames[ticker] = frame
        closes[ticker] = frame.set_index("date")["close"]
    table = b0liq.name_liquidity(frames)
    tiers = {m["ticker"]: m["cap_tier"] for m in members}
    table["cap_tier"] = [tiers.get(t, "?") for t in table.index]
    return table, pd.DataFrame(closes).sort_index(), tiers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-history", type=int, default=1260)
    args = parser.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)

    payload = json.loads(UNIVERSE_JSON.read_text())
    members = payload["members"]
    table, closes, tiers = build_liquidity(members)
    subs = {m["ticker"]: m["gics_sub_industry"] for m in members}
    print(f"universe {len(members)}, data present {len(table)}", flush=True)

    # --- capacity gates, applied before any return is computed (R2-3) ---
    kept, reasons = b0liq.capacity_gates(table)
    long_enough = {t for t in closes.columns
                   if closes[t].dropna().shape[0] >= args.min_history}
    kept = sorted(set(kept) & long_enough)
    reasons["fail_history"] = ~reasons.index.isin(long_enough)
    reasons["dropped"] = reasons[[c for c in reasons.columns
                                  if c.startswith("fail_")]].any(axis=1)
    table.join(reasons).to_csv(RESULTS / "round2_liquidity.csv")
    print(f"capacity gates: kept {len(kept)} of {len(table)}", flush=True)
    for col in [c for c in reasons.columns if c.startswith("fail_")]:
        print(f"  {col:22s} drops {int(reasons[col].sum())}", flush=True)
    by_tier = pd.Series([tiers.get(t, "?") for t in kept]).value_counts()
    print(f"  kept by tier: {by_tier.to_dict()}", flush=True)

    # --- capacity report (user requirement: GBP 10,000 book) ---
    from research.b0_statarb import capacity as b0cap
    scaling = b0cap.scaling_table(table)
    tiers_cost = b0cap.cost_by_tier(table)
    scaling.to_csv(RESULTS / "round2_capacity_scaling.csv", index=False)
    tiers_cost.to_csv(RESULTS / "round2_cost_by_tier.csv")
    print("\n=== 容量：不同本金下仍过闸的标的数 ===", flush=True)
    print(scaling.to_string(index=False, float_format=lambda v: f"{v:,.6g}"),
          flush=True)
    print("\n=== 成本：按市值层 ===", flush=True)
    print(tiers_cost.to_string(float_format=lambda v: f"{v:,.2f}"), flush=True)

    half_spread = table["half_spread_bps"].to_dict()
    panel = closes[kept].dropna(axis=1, thresh=int(len(closes) * 0.9))
    groups = {t: subs[t] for t in panel.columns if t in subs}
    print(f"panel {panel.shape[0]} days x {panel.shape[1]} names", flush=True)

    rows = {}
    tier_sets = {"all": list(panel.columns)}
    for tier in ("large", "mid", "small"):
        names = [t for t in panel.columns if tiers.get(t) == tier]
        if len(names) >= 40:
            tier_sets[tier] = names

    series = {}
    for tier, names in tier_sets.items():
        # Pair selection is the expensive step (an ADF per candidate pair per
        # window) and depends only on (tier, window) -- not on the variant or
        # the cost model. Selecting once per window and reusing the result
        # across all four variant/cost combinations cuts the run by 4x; doing
        # it inside the combination loop would re-run 14,798 candidate pairs
        # per window four times over for identical output.
        evaluated = evaluate_tier(panel[names],
                                  {t: groups[t] for t in names if t in groups},
                                  half_spread)
        for label, ret in evaluated.items():
            full = f"{tier}|{label}"
            rows[full] = summarize(ret, full)
            if not ret.empty:
                series[full] = ret
            print(f"  {full:22s} days={rows[full].get('days',0):5d} "
                  f"ann={rows[full].get('ann_return',float('nan')):+.2%} "
                  f"t={rows[full].get('nw_t',float('nan')):+.2f}", flush=True)

    pd.DataFrame(rows).T.to_csv(RESULTS / "round2_summary.csv")
    if series:
        pd.DataFrame(series).to_csv(RESULTS / "round2_daily_returns.csv")
    print(f"\nwritten {RESULTS}/round2_summary.csv")
    return 0


def evaluate_tier(panel: pd.DataFrame, groups: dict,
                  half_spread: dict) -> dict[str, pd.Series]:
    """Every variant and cost mode for one tier, on ONE pair selection pass.

    Returns {"MN|free": series, "MN|spread": ..., "L2|free": ..., "L2|spread": ...}.
    """
    from research.b0_statarb import pairs as b0pairs
    from research.b0_statarb.run_study import windows, TOP_N
    combos = [(v, c) for v in ("MN", "L2") for c in ("free", "spread")]
    chunks: dict[str, list] = {f"{v}|{c}": [] for v, c in combos}
    for form_idx, trade_idx in windows(panel.index):
        form = panel.loc[form_idx].dropna(axis=1, how="any")
        if form.shape[1] < 10:
            continue
        chosen = b0pairs.select_cointegration(form, groups, TOP_N, True)
        if not chosen:
            continue
        legs: dict[str, list] = {f"{v}|{c}": [] for v, c in combos}
        for a, b, params in chosen:
            trade = panel.loc[trade_idx, [a, b]].dropna()
            if len(trade) < 20:
                continue
            for variant, cost in combos:
                hs = None if cost == "free" else half_spread
                legs[f"{variant}|{cost}"].append(
                    b0engine.run_pair(trade, a, b, params, variant, "coint",
                                      0.0, half_spread=hs))
        for key, group in legs.items():
            if group:
                chunks[key].append(pd.concat(group, axis=1).mean(axis=1))
    return {k: (pd.concat(v).sort_index() if v else pd.Series(dtype=float))
            for k, v in chunks.items()}


if __name__ == "__main__":
    raise SystemExit(main())
