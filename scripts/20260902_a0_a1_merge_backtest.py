"""A0 + A1 on one GBP 1,000 account (merged) versus two separate GBP 1,000 pots.

Responsibility: the two experiments requested on 2026-09-02, both through the
real engine (interval 1d, fill_timing same_close, worst and actual fee tiers),
window 2020-01-02..2026-08-28 (A1's validation window; A0 warms from
2010-01-04 for its expanding volatility percentile and goes live 2020-01-02).

Experiment M (merged, one pot of GBP 1,000, fully deployed every day):
    A0 runs unchanged for its SIGNAL: the set S0 of its 18 names whose slot is
    on (signal on and both market gates open). Every A0 slot that is off, and
    the cash A0 would otherwise hold, is deployed into A1's current 20-name
    book. Names wanted by both are sized by the A1 rule when
    priority == "a1" (the default reading of the request: capital occupancy
    follows A1) or by the A0 slot rule when priority == "a0". A0-only names
    keep A0's no-churn rule (a held position is not resized while on); A1
    names are re-sized daily to the capital left after A0, inside a 10% band
    so drift alone does not churn them and rejected orders retry the next day.

Experiment S (separate): A0 alone on GBP 1,000 and A1 alone on GBP 1,000,
    each with its own strategy module untouched.

Arms written: merged_{worst,actual}, a0_solo_{worst,actual},
a1_solo_{worst,actual}. Each leaves the engine's equity/trades/meta files in
backtest/results and one row in a0_a1_merge_summary_20260902.csv. The report
(research/xsmom_wide/report/) reads those files; nothing here renders.

Out of scope: A1's selection logic (research/xsmom_wide/run_study.py and the
head-to-head script, imported here), A0's signal (trading212/strategy/
a0_v0_0_1.py), and the engine.

Public functions:
    make_merged(a0_params, plan, us_days, priority)   The merged closure.
    main()                                             Run all six arms.

Constants:
    CAPITAL_GBP   Decimal  1000, the requested pot size.
    A1_CONFIG     str      "N20|EW|band+|gate-", the A1 winner (ruling
                           research/decisions/20260902_xsmom_wide_ruling.md).
    A1_BAND       float    0.10, the no-churn band on A1 legs in the merged
                           account (design choice, section above).

Change log:
    2026-09-02  Created.
"""

from __future__ import annotations

__all__ = ["make_merged", "main"]

import argparse
import copy
import importlib.util
import json
import sys
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.engine.strategy_loader import load_strategy          # noqa: E402
from backtest.engine.types import EngineConfig                     # noqa: E402
from backtest.t212.data_source import load_bars                    # noqa: E402
from backtest.t212.runner import run_t212_backtest                 # noqa: E402
from research.xsmom_wide.run_study import (load_panels,            # noqa: E402
                                           eligibility)

_spec = importlib.util.spec_from_file_location(
    "h2h", ROOT / "scripts" / "20260902_xsmom_a0_headtohead.py")
h2h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h2h)

RESULTS = ROOT / "backtest" / "results"
NY = "America/New_York"
D = Decimal
FX = "GBPUSD=X"
CAPITAL_GBP = D("1000")
VAL_START, VAL_END = "2020-01-02", "2026-08-28"
A1_CONFIG = "N20|EW|band+|gate-"
A1_BAND = 0.10
SHARE_STEP = D("0.0001")


def _shares(gbp: Decimal, fx: Decimal, px: Decimal) -> Decimal:
    if px <= 0 or gbp <= 0:
        return D("0")
    return (gbp * fx / px).quantize(SHARE_STEP, ROUND_DOWN)


class _SignalView:
    """Portfolio lookalike with large cash, for reading A0's signal set only."""
    def __init__(self, portfolio):
        self.cash_gbp = D("1000000")
        self.positions = portfolio.positions
        self.pending_signed_qty = portfolio.pending_signed_qty


def make_merged(a0_params: dict, plan: dict, us_days: set,
                priority: str = "a1", a0_module=None):
    """One account, A0 signal first, A1 absorbs whatever A0 leaves."""
    a0_names = list(a0_params["trade_symbols"])
    a0_set = set(a0_names)
    lookback = int(a0_params.get("tsmom_lookback", 252))
    trend_ma = int(a0_params.get("trend_ma", 200))
    headroom = D(str(a0_params.get("slot_headroom", 0.99)))

    def strategy(view, portfolio, params) -> dict[str, Decimal]:
        ts = view.now
        day = ts.date() if ts.tzinfo is None else ts.tz_convert(NY).date()
        if day not in us_days:
            return {}
        iso = day.isoformat()
        if iso in plan:
            strategy.book = plan[iso]
        book = strategy.book
        fx_bar = view.bar(FX)
        if fx_bar is None or fx_bar.close <= 0:
            return {}
        fx = D(str(fx_bar.close))

        # A0's signal set, from its own untouched module. A0 sizes slots from
        # the equity IT can see (cash plus its 18 names), which in the merged
        # account can be near zero once A1 holds the capital; a fresh slot
        # would then floor to 0 shares and read as "not wanted". The signal
        # is therefore read through a view with synthetic cash: membership in
        # the set only needs q > 0, and the no-churn branch still returns the
        # real held quantity for names already on.
        t0 = a0_module.compute_targets(view, _SignalView(portfolio),
                                       a0_params)
        s0 = {s for s, q in t0.items() if q > 0}
        active = [s for s in a0_names
                  if len(view.bars(s, max(lookback, trend_ma) + 1))
                  >= lookback + 1]
        n_slots = max(len(active), 1)

        # Whole-account equity at the strategy's own mid view.
        prices: dict[str, Decimal] = {}
        equity = portfolio.cash_gbp
        for symbol, qty in portfolio.positions.items():
            bar = view.bar(symbol)
            if bar is not None and bar.close > 0:
                prices[symbol] = D(str(bar.close))
                if qty:
                    equity += qty * prices[symbol] / fx
        if not book and not s0:
            return {}

        def held_of(s):
            return portfolio.positions.get(s, D("0")) \
                + portfolio.pending_signed_qty.get(s, D("0"))

        def price_of(s):
            if s in prices:
                return prices[s]
            bar = view.bar(s)
            return D(str(bar.close)) if bar is not None and bar.close > 0 \
                else D("0")

        slot = equity / D(n_slots) * headroom
        book_set = set(book)
        a0_sized = s0 - book_set if priority == "a1" else set(s0)
        a1_sized = book_set if priority == "a1" else book_set - s0

        # sorted(): a0_sized and a1_sized are sets, and the engine submits
        # in the target mapping's insertion order, so set iteration made the
        # run depend on the process hash seed (fixplans/t212/b0/
        # 02_strategy_b0.md fact 6). Sorting fixes the order without
        # changing which names are sized.
        targets: dict[str, Decimal] = {}
        a0_value = D("0")
        for s in sorted(a0_sized):
            px = price_of(s)
            held = held_of(s)
            q = held if held > 0 else _shares(slot, fx, px)
            targets[s] = q
            a0_value += q * px / fx if px > 0 else D("0")

        c1 = equity * headroom - a0_value
        per = c1 / D(max(len(a1_sized), 1)) if a1_sized else D("0")
        for s in sorted(a1_sized):
            px = price_of(s)
            tgt = _shares(per, fx, px) if per > 0 else D("0")
            held = held_of(s)
            if held > 0 and tgt > 0 and \
                    abs(float(held - tgt)) / float(tgt) < A1_BAND:
                targets[s] = held
            else:
                targets[s] = tgt

        for s in list(portfolio.positions) + list(portfolio.pending_signed_qty):
            if s not in targets and (portfolio.positions.get(s, D("0")) > 0
                                     or portfolio.pending_signed_qty.get(s, D("0")) != 0):
                targets[s] = D("0")
        for s in a0_names:
            targets.setdefault(s, D("0"))
        return targets

    strategy.book = {}
    return strategy


def _stats_row(label: str, res, us_days: set) -> dict:
    curve = h2h.session_curve(res.equity, us_days)
    row = h2h.stats(curve, res.trades, label)
    row["initial_gbp"] = float(CAPITAL_GBP)
    row["cagr"] = float((curve.iloc[-1] / float(CAPITAL_GBP))
                        ** (252.0 / len(curve)) - 1)
    row["turnover_legs_per_yr"] = (float(res.trades["cash_delta_gbp"].abs().sum())
                                   / float(curve.mean()) / (len(curve) / 252.0)
                                   if not res.trades.empty else 0.0)
    row["rejected"] = int(res.meta.get("orders_rejected", 0))
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--priority", default="a1", choices=("a1", "a0"))
    parser.add_argument("--tiers", default="worst,actual")
    parser.add_argument("--only", default="merged,a0_solo,a1_solo")
    parser.add_argument("--quick", action="store_true",
                        help="end the window at 2021-03-31 (smoke test)")
    args = parser.parse_args()
    global VAL_END
    if args.quick:
        VAL_END = "2021-03-31"
    tiers = args.tiers.split(",")
    arms = args.only.split(",")

    base = yaml.safe_load((ROOT / "trading212/config/strategies/a0_v0_0_1.yaml")
                          .read_text())
    a0_params = copy.deepcopy(base)
    a0_params["live_from"] = VAL_START
    a0_names = list(base["trade_symbols"])

    payload = json.loads((ROOT / "data/reference/b0_universe_1500_20260823.json")
                         .read_text())
    closes, volumes = load_panels(payload["members"])
    closes = closes[closes.index >= pd.Timestamp("2010-01-04").date()]
    volumes = volumes.reindex(closes.index)
    elig = eligibility(closes, volumes)
    n_hold = int(A1_CONFIG.split("|")[0][1:])
    plan, _ = h2h.winner_plan(closes, elig, n_hold, "EW", "band+" in A1_CONFIG,
                              "gate+" in A1_CONFIG)
    plan = {d: w for d, w in plan.items() if d <= VAL_END}
    union = sorted({s for w in plan.values() for s in w})
    us_days = set(load_bars(["SPY"], "1d", "2000-01-01", "2099-01-01")["SPY"]
                  ["ts"].dt.tz_convert(NY).dt.date)
    print(f"A1 plan: {len(plan)} rebalances, union {len(union)} names; "
          f"overlap with A0 names: {sorted(set(union) & set(a0_names))}",
          flush=True)

    import importlib
    a0_module = importlib.import_module("trading212.strategy.a0_v0_0_1")

    rows = []
    for tier in tiers:
        if "merged" in arms:
            feed = sorted(set(a0_names) | set(union)) + [base["state_symbol"], FX]
            cfg = EngineConfig(symbols=feed, interval="1d", start="2010-01-04",
                               end=VAL_END, initial_cash_gbp=CAPITAL_GBP,
                               arm=f"merged_{args.priority}_{tier}",
                               fee_tier=tier, fill_timing="same_close",
                               strategy_name="a0a1_merged",
                               strategy_version="0.0.1",
                               params={"fx_symbol": FX})
            res, _, _ = run_t212_backtest(
                cfg, make_merged(a0_params, plan, us_days, args.priority,
                                 a0_module), write=True)
            rows.append(_stats_row(f"merged/{tier}", res, us_days))
            print(f"  merged/{tier}: final £{rows[-1]['final_gbp']:,.2f} "
                  f"rejected {rows[-1]['rejected']}", flush=True)
        if "a0_solo" in arms:
            feed = a0_names + [base["state_symbol"], FX]
            cfg = EngineConfig(symbols=feed, interval="1d", start="2010-01-04",
                               end=VAL_END, initial_cash_gbp=CAPITAL_GBP,
                               arm=f"a0_solo_{tier}", fee_tier=tier,
                               fill_timing="same_close", strategy_name="a0",
                               strategy_version="0.0.1", params=a0_params)
            res, _, _ = run_t212_backtest(
                cfg, load_strategy("t212", "a0", "0.0.1"), write=True)
            rows.append(_stats_row(f"a0_solo/{tier}", res, us_days))
            print(f"  a0_solo/{tier}: final £{rows[-1]['final_gbp']:,.2f}",
                  flush=True)
        if "a1_solo" in arms:
            cfg = EngineConfig(symbols=union + [FX], interval="1d",
                               start="2019-06-03", end=VAL_END,
                               initial_cash_gbp=CAPITAL_GBP,
                               arm=f"a1_solo_{tier}", fee_tier=tier,
                               fill_timing="same_close",
                               strategy_name="xsmom_wide",
                               strategy_version="0.0.1",
                               params={"fx_symbol": FX})
            res, _, _ = run_t212_backtest(
                cfg, h2h.make_wide_strategy(plan, None, us_days), write=True)
            rows.append(_stats_row(f"a1_solo/{tier}", res, us_days))
            print(f"  a1_solo/{tier}: final £{rows[-1]['final_gbp']:,.2f}",
                  flush=True)

    table = pd.DataFrame(rows)
    suffix = "_quick" if args.quick else ""
    out = RESULTS / f"a0_a1_merge_summary_{args.priority}{suffix}_20260902.csv"
    table.to_csv(out, index=False)
    (RESULTS / f"a0_a1_plan_{args.priority}_20260902.json").write_text(
        json.dumps({"plan": plan, "union": union, "a0_names": a0_names,
                    "priority": args.priority}))
    print("\n" + table.to_string(index=False,
                                 float_format=lambda v: f"{v:,.4f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
