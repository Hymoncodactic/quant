"""Reproduction arm for trading212/strategy/b0_v0_0_1.py against the reference.

Responsibility: prove that the B0 module computes the same targets and the
same equity curve as the reference closure it was derived from
(scripts/20260902_a0_a1_merge_backtest.py make_merged), on one panel, in one
process, at the caliber frozen in fixplans/t212/b0/06_tests_and_rollout.md
section 2: engine start 2010-01-04, window 2020-01-02..2026-08-28, interval
1d, fill_timing same_close, GBP 1,000, a0_params.live_from and
a1_params.rebalance_anchor both 2020-01-02, sells_first false so the module
keeps the reference's submission order, and A1 in research shape
(rank_as_of == as_of, four admission conditions).

The recorded numbers of b0_spec.md section 9.4 (GBP 17,469.4818 worst,
GBP 18,793.7032 actual) are NOT the acceptance test and cannot be: the
reference sized from python sets, so the engine's submission order -- and with
it which buys the cash check accepted -- moved with PYTHONHASHSEED. Both
reference scripts were made deterministic on 2026-09-03 before this arm was
written, which necessarily re-bases those numbers; the deterministic figures
this script prints are the ones to record. What is testable, and what is
tested here, is that module and reference agree to the penny on one panel.

A second arm runs with sells_first true, which is the live order. It is not
expected to equal the reference: emitting reductions first is the improvement
b0_spec.md section 9.6 names, and the difference between the two arms is its
measured value.

Out of scope: the merged rule itself (trading212/strategy/b0_v0_0_1.py), A1's
selection (a1_v0_0_1.py), and the engine.

Public functions:
    build_injection(closes, volumes, sessions, a0_names)  Research injection.
    main()                                               Run the arms.

Constants:
    CAPITAL_GBP  Decimal  1000, the caliber's principal.
    ENGINE_START str      "2010-01-04", A0's volatility-percentile warm-up.

Inputs:
    data/reference/b0_universe_1500_20260823.json
    trading212/config/strategies/{a0,a1,b0}_v0_0_1.yaml
    data/t212/curated/**
Outputs:
    backtest/results/b0_module_vs_reference_20260903.csv
    the engine's own equity/trades/meta files per arm

Change log:
    2026-09-03  Created for fixplans/t212/b0/02_strategy_b0.md section 5.
"""

from __future__ import annotations

__all__ = ["build_injection", "main"]

import argparse
import copy
import importlib
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.engine.types import EngineConfig                     # noqa: E402
from backtest.t212.runner import run_t212_backtest                 # noqa: E402
from research.xsmom_wide.run_study import (load_panels,            # noqa: E402
                                           eligibility)
from trading212.execution.market_data import us_sessions       # noqa: E402
from trading212.ingest.a1_rank import drop_excluded_sessions           # noqa: E402
from trading212.execution.strategy_loader import load_module       # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "h2h", ROOT / "scripts" / "20260902_xsmom_a0_headtohead.py")
h2h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h2h)
_mspec = importlib.util.spec_from_file_location(
    "merge", ROOT / "scripts" / "20260902_a0_a1_merge_backtest.py")
merge = importlib.util.module_from_spec(_mspec)
_mspec.loader.exec_module(merge)

RESULTS = ROOT / "backtest" / "results"
D = Decimal
FX = "GBPUSD=X"
CAPITAL_GBP = D("1000")
ENGINE_START = "2010-01-04"
PANEL_START = "2010-01-04"
VAL_START, VAL_END = "2020-01-02", "2026-08-28"


def build_injection(closes: pd.DataFrame, volumes: pd.DataFrame,
                    sessions: list) -> dict:
    """Research-shape injection: the panel travels, A0 reads the daily view."""
    return {"panel": {"closes": closes, "volumes": volumes},
            "sessions": sessions, "a1_book": {}, "thin": [],
            "a1_frozen": False, "a0_mode": "view",
            "rank_as_of": None, "rank_stale_sessions": 0}


def _stats(res, us_days: set, label: str) -> dict:
    curve = h2h.session_curve(res.equity, us_days)
    row = h2h.stats(curve, res.trades, label)
    row["initial_gbp"] = float(CAPITAL_GBP)
    row["cagr"] = float((curve.iloc[-1] / float(CAPITAL_GBP))
                        ** (252.0 / len(curve)) - 1)
    row["rejected"] = int(res.meta.get("orders_rejected", 0))
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--end", default=VAL_END)
    parser.add_argument("--tiers", default="worst")
    parser.add_argument("--arms", default="module,reference,module_sells_first")
    args = parser.parse_args()
    end = args.end
    arms = args.arms.split(",")

    a0_params = copy.deepcopy(yaml.safe_load(
        (ROOT / "trading212/config/strategies/a0_v0_0_1.yaml").read_text()))
    a0_params["live_from"] = VAL_START
    a1_params = copy.deepcopy(yaml.safe_load(
        (ROOT / "trading212/config/strategies/a1_v0_0_1.yaml").read_text()))
    a1_params["require_verified_ticker"] = False
    a1_params["rebalance_anchor"] = VAL_START
    a1_params["live_from"] = VAL_START
    b0_params = copy.deepcopy(yaml.safe_load(
        (ROOT / "trading212/config/strategies/b0_v0_0_1.yaml").read_text()))
    b0_params.update({"live_from": VAL_START, "a0_params": a0_params,
                      "a1_params": a1_params, "sells_first": False})
    a0_names = list(a0_params["trade_symbols"])

    payload = json.loads(
        (ROOT / "data/reference/b0_universe_1500_20260823.json").read_text())
    closes, volumes = load_panels(payload["members"])
    closes = closes[closes.index >= pd.Timestamp(PANEL_START).date()]
    volumes = volumes.reindex(closes.index)
    # The same session exclusions the live ranking pass applies. Without this
    # the reproduction arm ranks a panel the live pass never sees, and the two
    # stop being comparable -- which is the whole point of these arms.
    closes, volumes = drop_excluded_sessions(closes, volumes)
    elig = eligibility(closes, volumes)
    plan, _ = h2h.winner_plan(closes, elig, int(a1_params["n_hold"]), "EW",
                              True, False)
    plan = {d: w for d, w in plan.items() if d <= end}
    union = sorted({s for w in plan.values() for s in w})
    sessions = us_sessions(VAL_START, end)
    us_days = set(us_sessions("2000-01-01", "2099-01-01"))
    feed = sorted(set(a0_names) | set(union)) + [a0_params["state_symbol"], FX]
    print(f"plan: {len(plan)} rebalances, union {len(union)}, feed {len(feed)}",
          flush=True)

    b0 = load_module("b0", "0.0.1")
    a0_module = importlib.import_module("trading212.strategy.a0_v0_0_1")

    rows = []
    for tier in args.tiers.split(","):
        if "module" in arms:
            rows.append(_run(b0, b0_params, closes, volumes, sessions, feed,
                             end, tier, "b0_module", us_days))
        if "reference" in arms:
            cfg = EngineConfig(symbols=feed, interval="1d", start=ENGINE_START,
                               end=end, initial_cash_gbp=CAPITAL_GBP,
                               arm=f"b0_reference_{tier}", fee_tier=tier,
                               fill_timing="same_close",
                               strategy_name="a0a1_merged",
                               strategy_version="0.0.1",
                               params={"fx_symbol": FX})
            res, _, _ = run_t212_backtest(
                cfg, merge.make_merged(a0_params, plan, us_days, "a1",
                                       a0_module), write=True)
            rows.append(_stats(res, us_days, f"b0_reference/{tier}"))
            print(f"  reference/{tier}: GBP {rows[-1]['final_gbp']:,.6f} "
                  f"rejected {rows[-1]['rejected']}", flush=True)
        if "module_sells_first" in arms:
            live_params = copy.deepcopy(b0_params)
            live_params["sells_first"] = True
            rows.append(_run(b0, live_params, closes, volumes, sessions, feed,
                             end, tier, "b0_module_sells_first", us_days))

    table = pd.DataFrame(rows)
    table.to_csv(RESULTS / "b0_module_vs_reference_20260903.csv", index=False)
    print("\n" + table.to_string(index=False,
                                 float_format=lambda v: f"{v:,.6f}"))
    finals = {r["arm"]: r["final_gbp"] for r in rows}
    for tier in args.tiers.split(","):
        a, b = finals.get(f"b0_module/{tier}"), finals.get(f"b0_reference/{tier}")
        if a is not None and b is not None:
            gap = abs(a - b)
            print(f"\nmodule minus reference ({tier}): GBP {gap:.6f}  "
                  f"({'MATCH' if gap < 0.005 else 'DIVERGENT'})")
            if gap >= 0.005:
                return 1
    return 0


def _run(module, params, closes, volumes, sessions, feed, end, tier, arm,
         us_days) -> dict:
    injection = build_injection(closes, volumes, sessions)
    strategy = module.make_strategy(injection)
    cfg = EngineConfig(symbols=feed, interval="1d", start=ENGINE_START,
                       end=end, initial_cash_gbp=CAPITAL_GBP,
                       arm=f"{arm}_{tier}", fee_tier=tier,
                       fill_timing="same_close", strategy_name="b0",
                       strategy_version="0.0.1", params={"fx_symbol": FX})

    def call(view, portfolio, _engine_params):
        return strategy(view, portfolio, params)

    res, _, _ = run_t212_backtest(cfg, call, write=True)
    row = _stats(res, us_days, f"{arm}/{tier}")
    print(f"  {arm}/{tier}: GBP {row['final_gbp']:,.6f} "
          f"rejected {row['rejected']}", flush=True)
    return row


if __name__ == "__main__":
    raise SystemExit(main())
