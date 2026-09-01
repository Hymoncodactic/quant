"""Head-to-head: the wide-momentum winner vs A0, real engine, worst tier.

Responsibility: the cost-true comparison frozen in
research/prereg/20260902_xsmom_wide_prereg.md section 5. Both arms run through
backtest/ at interval 1d, fill_timing same_close, fee_tier worst, GBP 10,000,
validation window 2020-01-02..2026-08-28 (engines warm up earlier; capital is
confined by live_from / the rebalance calendar).

The wide arm's signals come from an injected daily close panel (the same
loaders as the research layer), causally sliced; the A0 gate series is
precomputed by research/xsmom_wide/run_study.gate_series, the SAME
construction A0's own module uses. Under same_close both arms may use day t's
close and fill at day t's close, which mirrors the live convention (submit
about a minute before the close); the research layer's extra one-day lag is
the more conservative variant and is disclosed in the ruling.

Public functions:
    main()   Run both arms, write curves and the comparison row.

Change log:
    2026-09-02  Created for the wide-momentum vs A0 comparison.
"""

from __future__ import annotations

__all__ = ["main"]

import argparse
import json
import sys
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.engine.strategy_loader import load_strategy          # noqa: E402
from backtest.engine.types import EngineConfig                     # noqa: E402
from backtest.t212.runner import run_t212_backtest                 # noqa: E402
from research.xsmom_wide.run_study import (gate_series,             # noqa: E402
                                           momentum_scores, load_panels,
                                           eligibility,
                                           REBALANCE_EVERY, IVOL_WINDOW)

RESULTS = ROOT / "backtest" / "results"
NY = "America/New_York"
D = Decimal
VAL_START, VAL_END = "2020-01-02", "2026-08-28"
FX = "GBPUSD=X"


def winner_plan(closes: pd.DataFrame, elig: pd.DataFrame, n_hold: int,
                weighting: str, band: bool, use_gates: bool
                ) -> tuple[dict, pd.Series]:
    """Per-rebalance-date target weights, plus the daily gate series.

    Deterministic replay of the research selection: rebalance every 21
    sessions from VAL_START; weights from data up to and including the
    decision day (same_close convention).
    """
    scores = momentum_scores(closes)
    vols = closes.pct_change().rolling(IVOL_WINDOW).std(ddof=1)
    days = [d for d in closes.index
            if pd.Timestamp(VAL_START).date() <= d
            <= pd.Timestamp(VAL_END).date()]
    plan: dict[str, dict[str, float]] = {}
    book: dict[str, float] = {}
    for i, day in enumerate(days):
        if i % REBALANCE_EVERY:
            continue
        row = scores.loc[day].dropna()
        ok = elig.loc[day]
        row = row[[t for t in row.index if bool(ok.get(t, False))]]
        ranked = row.sort_values(ascending=False)
        if band and book:
            keep = [t for t in book if t in ranked.index[:2 * n_hold]]
            fresh = [t for t in ranked.index if t not in keep]
            pick = keep + fresh[:max(0, n_hold - len(keep))]
        else:
            pick = list(ranked.index[:n_hold])
        if weighting == "IVOL":
            v = vols.loc[day, pick].replace(0, np.nan)
            inv = (1.0 / v).fillna(0.0)
            w = (inv / inv.sum()).to_dict() if inv.sum() > 0 else \
                {t: 1.0 / len(pick) for t in pick}
        else:
            w = {t: 1.0 / len(pick) for t in pick}
        book = w
        plan[day.isoformat()] = w
    gate = gate_series() if use_gates else None
    return plan, gate


def make_wide_strategy(plan: dict, gate: pd.Series | None,
                       us_days: set):
    """Rebalance to the planned book on plan days; gate flips daily.

    Review fixes: (a) a US-session guard, because GBPUSD=X supplies London
    timeline keys on US holidays where the gate lookup would otherwise
    default open and phantom-rebuy the book (review major 9); (b) the
    remembered book is updated from the plan even while the gate is closed,
    so a reopen rebuys the CURRENT plan rather than one up to 21 sessions
    stale (review major 10); the previous convoluted branch (with its dead
    'if held and not current' arm, review minor 15) is gone.
    """
    def strategy(view, portfolio, params) -> dict[str, Decimal]:
        ts = view.now
        day = (ts.date() if ts.tzinfo is None
               else ts.tz_convert(NY).date())
        if day not in us_days:
            return {}
        iso = day.isoformat()
        if iso in plan:
            strategy.last_book = plan[iso]
            strategy.dirty = True
        current = {s for s, q in portfolio.positions.items() if q > 0}
        g_open = True if gate is None else bool(gate.get(day, 0.0) > 0)
        if not g_open:
            strategy.dirty = True          # rebuy when the gate reopens
            return {s: D("0") for s in current}
        if not strategy.dirty or not strategy.last_book:
            return {}
        strategy.dirty = False
        fx_bar = view.bar(params["fx_symbol"])
        if fx_bar is None or fx_bar.close <= 0:
            strategy.dirty = True          # retry next session
            return {}
        fx = D(str(fx_bar.close))
        equity = portfolio.cash_gbp
        for symbol, qty in portfolio.positions.items():
            bar = view.bar(symbol)
            if bar is not None and qty:
                equity += qty * D(str(bar.close)) / fx
        targets = {s: D("0") for s in current}
        for symbol, weight in strategy.last_book.items():
            bar = view.bar(symbol)
            if bar is None or bar.close <= 0:
                continue
            shares = (equity * D(str(weight)) * D("0.99") * fx
                      / D(str(bar.close))).quantize(D("0.0001"), ROUND_DOWN)
            if shares > 0:
                targets[symbol] = shares
        return targets
    strategy.last_book = {}
    strategy.dirty = False
    return strategy


def session_curve(equity: pd.DataFrame, us_days: set) -> pd.Series:
    """One record per US trading session: GBPUSD=X's London calendar adds 60
    FX-only keys inside the window that are not sessions (review major 11)."""
    ts = equity["ts"]
    date = (ts.dt.tz_convert(NY).dt.date
            if isinstance(ts.dtype, pd.DatetimeTZDtype)
            else pd.to_datetime(ts).dt.date)
    frame = equity.assign(d=date)
    lo, hi = pd.Timestamp(VAL_START).date(), pd.Timestamp(VAL_END).date()
    frame = frame[(frame["d"] >= lo) & (frame["d"] <= hi)
                  & frame["d"].isin(us_days)]
    return frame.groupby("d")["equity_liq_gbp"].last()


def stats(curve: pd.Series, trades: pd.DataFrame, label: str) -> dict:
    rets = curve.pct_change().dropna()
    years = len(curve) / 252.0
    cagr = float((curve.iloc[-1] / 10000.0) ** (1 / years) - 1)
    monthly = curve.groupby(pd.PeriodIndex(pd.to_datetime(curve.index),
                                           freq="M")).last().pct_change().dropna()
    costs = float(sum(trades[c].sum() for c in trades.columns
                      if c.startswith("cost_"))) if not trades.empty else 0.0
    notional = float(trades["cash_delta_gbp"].abs().sum()) \
        if not trades.empty else 0.0
    return {"arm": label, "sessions": len(curve),
            "final_gbp": float(curve.iloc[-1]), "cagr": cagr,
            "max_dd": float(-(curve / curve.cummax() - 1.0).min()),
            "ann_vol": float(rets.std(ddof=1) * np.sqrt(252)),
            "sharpe": float(rets.mean() / rets.std(ddof=1) * np.sqrt(252)),
            "monthly_win": float((monthly > 0).mean()),
            "fills": int(len(trades)), "costs_gbp": costs,
            # Both-legs units of AVERAGE equity per year, matching the
            # research layer's Sigma|dw| definition (review major 12; the old
            # initial-capital denominator overstated 2x+ once equity grew).
            "turnover_legs_per_yr": notional / float(curve.mean()) / years}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True,
                        help='winner label, e.g. "N50|IVOL|band+|gate+"')
    args = parser.parse_args()
    n_hold = int(args.config.split("|")[0][1:])
    weighting = args.config.split("|")[1]
    band = "band+" in args.config
    use_gates = "gate+" in args.config

    payload = json.loads((ROOT / "data/reference/b0_universe_1500_20260823.json"
                          ).read_text())
    closes, volumes = load_panels(payload["members"])
    closes = closes[closes.index >= pd.Timestamp("2010-01-04").date()]
    volumes = volumes.reindex(closes.index)
    elig = eligibility(closes, volumes)
    plan, gate = winner_plan(closes, elig, n_hold, weighting, band, use_gates)
    from backtest.t212.data_source import load_bars
    us_days = set(load_bars(["SPY"], "1d", "2000-01-01", "2099-01-01")["SPY"]
                  ["ts"].dt.tz_convert(NY).dt.date)
    union = sorted({s for w in plan.values() for s in w})
    print(f"plan: {len(plan)} rebalances, union {len(union)} names", flush=True)

    rows, curves = [], {}
    # --- wide arm ---
    cfg = EngineConfig(symbols=union + [FX], interval="1d",
                       start="2019-06-03", end=VAL_END,
                       initial_cash_gbp=D("10000"), arm="xsmom_wide_worst",
                       fee_tier="worst", fill_timing="same_close",
                       strategy_name="xsmom_wide", strategy_version="0.0.1",
                       params={"fx_symbol": FX})
    res, met, _ = run_t212_backtest(cfg, make_wide_strategy(plan, gate, us_days),
                                    write=True)
    curve = session_curve(res.equity, us_days)
    rows.append(stats(curve, res.trades, f"xsmom {args.config}"))
    curves["xsmom"] = curve
    print(f"wide done: £{curve.iloc[-1]:,.2f}", flush=True)

    # --- A0 arm ---
    import copy, yaml
    base = yaml.safe_load((ROOT / "trading212/config/strategies/"
                           "a0_v0_0_1.yaml").read_text())
    params = copy.deepcopy(base)
    params["live_from"] = VAL_START
    feed = list(base["trade_symbols"]) + [base["state_symbol"], FX]
    cfg_a0 = EngineConfig(symbols=feed, interval="1d", start="2010-01-04",
                          end=VAL_END, initial_cash_gbp=D("10000"),
                          arm="a0_val_worst", fee_tier="worst",
                          fill_timing="same_close", strategy_name="a0",
                          strategy_version="0.0.1", params=params)
    res_a, met_a, _ = run_t212_backtest(cfg_a0,
                                        load_strategy("t212", "a0", "0.0.1"),
                                        write=True)
    curve_a = session_curve(res_a.equity, us_days)
    rows.append(stats(curve_a, res_a.trades, "A0"))
    curves["a0"] = curve_a

    joined = pd.concat([curves["xsmom"].pct_change().rename("x"),
                        curves["a0"].pct_change().rename("a")],
                       axis=1).dropna()
    rho = float(joined["x"].corr(joined["a"]))
    out = pd.DataFrame(rows)
    out["corr_with_a0"] = [rho, 1.0]
    out.to_csv(RESULTS / "xsmom_a0_headtohead_causal_20260902.csv", index=False)
    pd.DataFrame(curves).to_csv(RESULTS / "xsmom_a0_curves_causal_20260902.csv")
    print("\n" + out.to_string(index=False,
                               float_format=lambda v: f"{v:,.4f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
