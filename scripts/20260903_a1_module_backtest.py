"""Reproduction arm for trading212/strategy/a1_v0_0_1.py against the reference.

Responsibility: prove that the A1 module computes the same book and the same
equity curve as the research reference it was derived from
(scripts/20260902_xsmom_a0_headtohead.py winner_plan / make_wide_strategy),
on one panel, in one process, at the caliber frozen in
fixplans/t212/b0/06_tests_and_rollout.md section 2: engine start 2019-06-03,
window 2020-01-02..2026-08-28, interval 1d, fill_timing same_close, worst fee
tier, GBP 10,000, rebalance anchor 2020-01-02, research admission (four
conditions, rank_as_of == as_of).

Why equivalence and not a recorded number. The criterion in the plan
(GBP 111,347.561961, recorded 2026-09-02) is not reachable and could not have
been reached by any implementation:

  1. The reference was hash-seed dependent. Its target mapping seeded the
     zero targets from a SET, and the engine submits in mapping order, so the
     sell/buy interleaving moved with PYTHONHASHSEED. Measured 2026-09-03 on
     one panel: seed 1 -> GBP 118,221.803491, seed 7 -> GBP 116,595.492465.
     Both scripts were made deterministic before this arm was written.
  2. Adjusted daily prices are retroactive. Every dividend restates a name's
     whole history, so an equity figure recorded on one day is not
     reproducible from a lake refreshed on the next. The SELECTION is stable
     -- all 80 rebalance books computed on 2026-09-03 are identical to the
     ones recorded on 2026-09-02 (backtest/results/a0_a1_plan_a1_20260902.json)
     -- but the fills are not.

What is testable is that the module and the reference agree on the same data,
which is what this script asserts, plus that the 80 books match the recorded
plan, which pins the selection logic to the recorded one.

Out of scope: the selection rules themselves (trading212/strategy/
a1_v0_0_1.py), the merged strategy (scripts/20260902_a0_a1_merge_backtest.py),
and the engine.

Public functions:
    build_injection(closes, volumes, sessions)  The research-shape injection.
    main()                                      Run both arms and compare.

Constants:
    CAPITAL_GBP  Decimal  10000, the caliber's principal.
    ENGINE_START str      "2019-06-03", the warm-up start of the reference arm.
    VAL_START / VAL_END   The evaluation window.

Inputs:
    data/reference/b0_universe_1500_20260823.json
    data/t212/curated/us_equity/**, us_etf/SPY/1d/**
Outputs:
    backtest/results/a1_module_vs_reference_20260903.csv
    the engine's own equity/trades/meta files for both arms

Change log:
    2026-09-03  Created for fixplans/t212/b0/01_strategy_a1.md section 5 step 4.
"""

from __future__ import annotations

__all__ = ["build_injection", "main"]

import argparse
import copy
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

RESULTS = ROOT / "backtest" / "results"
D = Decimal
FX = "GBPUSD=X"
CAPITAL_GBP = D("10000")
ENGINE_START = "2019-06-03"
PANEL_START = "2010-01-04"
VAL_START, VAL_END = "2020-01-02", "2026-08-28"


def build_injection(closes: pd.DataFrame, volumes: pd.DataFrame,
                    sessions: list) -> dict:
    """The research-shape injection of 00_coordination.md section 2.3.

    Research shape means the panel travels instead of a pre-computed ranking
    table, so rank_as_of equals the decision day and the module ranks on every
    rebalance itself. a1_book starts empty: the first rebalance has no previous
    book and takes the plain top twenty.
    """
    return {"panel": {"closes": closes, "volumes": volumes},
            "sessions": sessions, "a1_book": {}, "thin": [],
            "rank_as_of": None, "rank_stale_sessions": 0}


def _stats(res, us_days: set, label: str) -> dict:
    curve = h2h.session_curve(res.equity, us_days)
    row = h2h.stats(curve, res.trades, label)
    row["rejected"] = int(res.meta.get("orders_rejected", 0))
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--end", default=VAL_END,
                        help="window end; shorten it for a smoke run")
    parser.add_argument("--arms", default="module,reference")
    args = parser.parse_args()
    end = args.end
    arms = args.arms.split(",")

    params = yaml.safe_load(
        (ROOT / "trading212/config/strategies/a1_v0_0_1.yaml").read_text())
    params = copy.deepcopy(params)
    # Research arm: four admission conditions, not five. E5 exists because a
    # live order needs a venue ticker; the backtest places no venue order.
    params["require_verified_ticker"] = False
    params["rebalance_anchor"] = VAL_START
    params["live_from"] = VAL_START
    params["fx_symbol"] = FX

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
    plan, _gate = h2h.winner_plan(closes, elig, int(params["n_hold"]), "EW",
                                  True, False)
    plan = {d: w for d, w in plan.items() if d <= end}
    union = sorted({s for w in plan.values() for s in w})
    sessions = us_sessions(VAL_START, end)
    us_days = set(us_sessions("2000-01-01", "2099-01-01"))
    print(f"plan: {len(plan)} rebalances, union {len(union)} names, "
          f"{len(sessions)} sessions", flush=True)

    a1 = load_module("a1", "0.0.1")
    injection = build_injection(closes, volumes, sessions)
    module_strategy = a1.make_strategy(injection)

    rows = []
    if "module" in arms:
        cfg = EngineConfig(symbols=union + [FX], interval="1d",
                           start=ENGINE_START, end=end,
                           initial_cash_gbp=CAPITAL_GBP, arm="a1_module_worst",
                           fee_tier="worst", fill_timing="same_close",
                           strategy_name="a1", strategy_version="0.0.1",
                           params={"fx_symbol": FX})
        res, _, _ = run_t212_backtest(cfg, _bound(module_strategy, params),
                                      write=True)
        rows.append(_stats(res, us_days, "a1_module/worst"))
        print(f"  module/worst: GBP {rows[-1]['final_gbp']:,.6f}", flush=True)

    if "reference" in arms:
        cfg = EngineConfig(symbols=union + [FX], interval="1d",
                           start=ENGINE_START, end=end,
                           initial_cash_gbp=CAPITAL_GBP,
                           arm="a1_reference_worst", fee_tier="worst",
                           fill_timing="same_close", strategy_name="xsmom_wide",
                           strategy_version="0.0.1", params={"fx_symbol": FX})
        res, _, _ = run_t212_backtest(
            cfg, h2h.make_wide_strategy(plan, None, us_days), write=True)
        rows.append(_stats(res, us_days, "a1_reference/worst"))
        print(f"  reference/worst: GBP {rows[-1]['final_gbp']:,.6f}", flush=True)

    table = pd.DataFrame(rows)
    out = RESULTS / "a1_module_vs_reference_20260903.csv"
    table.to_csv(out, index=False)
    print("\n" + table.to_string(index=False,
                                 float_format=lambda v: f"{v:,.6f}"))
    if len(rows) == 2:
        gap = abs(rows[0]["final_gbp"] - rows[1]["final_gbp"])
        print(f"\nmodule minus reference: GBP {gap:.6f}  "
              f"({'MATCH' if gap < 0.005 else 'DIVERGENT'})")
        return 0 if gap < 0.005 else 1
    return 0


def _bound(strategy, params: dict):
    """Feed the module's own params in, whatever the engine passes."""
    def call(view, portfolio, engine_params):
        merged = dict(params)
        merged.update(engine_params or {})
        merged["fx_symbol"] = params["fx_symbol"]
        merged["live_from"] = params["live_from"]
        return strategy(view, portfolio, merged)
    return call


if __name__ == "__main__":
    raise SystemExit(main())
