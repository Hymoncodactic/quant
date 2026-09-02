"""Derive every number and series of the A0+A1 merge report from engine files.

Responsibility: read the six engine runs written by
scripts/20260902_a0_a1_merge_backtest.py (merged / a0_solo / a1_solo, worst
and actual tiers), compute the session curves, drawdowns, monthly returns,
capital occupancy attribution (A0 share / A1 share / cash, daily), holding
episodes (duration, per-episode win rate), turnover, costs by item, and the
two-pot combination (a0_solo + a1_solo on GBP 2,000), and write one JSON
payload beside this file for the template. Nothing here renders.

Out of scope: running the backtests (the script above) and rendering
(merge_report_template.html + build_merge_report.py).

Public functions:
    build(priority)   Return the payload dict.
    main()            Write merge_report_data.json.

Constants:
    WINDOW_START / WINDOW_END   str  2020-01-02 / 2026-08-28.
    ARMS                        list Arm stems and their display labels.

Change log:
    2026-09-02  Created.
"""

from __future__ import annotations

__all__ = ["build", "main"]

import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from backtest.t212.data_source import load_bars                    # noqa: E402

RESULTS = ROOT / "backtest" / "results"
NY = "America/New_York"
WINDOW_START, WINDOW_END = "2020-01-02", "2026-08-28"
CAPITAL = 1000.0


def us_sessions() -> list:
    frame = load_bars(["SPY"], "1d", "2000-01-01", "2099-01-01")["SPY"]
    days = sorted(set(frame["ts"].dt.tz_convert(NY).dt.date))
    lo, hi = pd.Timestamp(WINDOW_START).date(), pd.Timestamp(WINDOW_END).date()
    return [d for d in days if lo <= d <= hi]


def _find(pattern: str) -> Path:
    hits = sorted(glob.glob(str(RESULTS / pattern)))
    if not hits:
        raise FileNotFoundError(pattern)
    return Path(hits[-1])


def load_arm(stem: str, tier: str, sessions: list) -> dict:
    eq = pd.read_parquet(_find(f"*_{stem}_{tier}_*.equity.parquet"))
    tr = pd.read_parquet(_find(f"*_{stem}_{tier}_*.trades.parquet"))
    meta = json.loads(_find(f"*_{stem}_{tier}_*.meta.json").read_text())
    ts = eq["ts"]
    date = (ts.dt.tz_convert(NY).dt.date if isinstance(ts.dtype, pd.DatetimeTZDtype)
            else pd.to_datetime(ts).dt.date)
    eq = eq.assign(d=date)
    eq = eq[eq["d"].isin(set(sessions))].groupby("d").last()
    eq = eq.reindex(sessions).ffill()
    if not tr.empty:
        tr = tr.assign(d=pd.to_datetime(tr["ts"], utc=True).dt.tz_convert(NY).dt.date)
        tr = tr[tr["d"].isin(set(sessions))]
    return {"equity": eq, "trades": tr, "meta": meta}


def curve_metrics(curve: pd.Series, initial: float) -> dict:
    rets = curve.pct_change().dropna()
    years = len(curve) / 252.0
    peak = curve.cummax()
    dd = curve / peak - 1.0
    trough = dd.idxmin()
    peak_day = curve.loc[:trough].idxmax()
    monthly = curve.groupby(pd.PeriodIndex(pd.to_datetime(curve.index), freq="M")) \
        .last().pct_change().dropna()
    x = rets.to_numpy() - rets.mean()
    lrv = float((x * x).mean())
    for lag in range(1, 21):
        lrv += 2 * (1 - lag / 21) * float((x[lag:] * x[:-lag]).mean())
    nw_t = float(rets.mean() / np.sqrt(max(lrv, 1e-18) / len(rets)))
    return {"final": float(curve.iloc[-1]), "initial": initial,
            "total_return": float(curve.iloc[-1] / initial - 1),
            "cagr": float((curve.iloc[-1] / initial) ** (1 / years) - 1),
            "ann_vol": float(rets.std(ddof=1) * np.sqrt(252)),
            "sharpe": float(rets.mean() / rets.std(ddof=1) * np.sqrt(252)),
            "nw_t": nw_t,
            "max_dd": float(-dd.min()), "dd_peak": str(peak_day),
            "dd_trough": str(trough),
            "calmar": float(((curve.iloc[-1] / initial) ** (1 / years) - 1)
                            / max(-dd.min(), 1e-9)),
            "monthly_win": float((monthly > 0).mean()),
            "best_month": float(monthly.max()), "worst_month": float(monthly.min()),
            "worst_month_label": str(monthly.idxmin()),
            "sessions": int(len(curve)), "years": years,
            "monthly": {str(k): float(v) for k, v in monthly.items()},
            "drawdown": [float(v) for v in dd.to_numpy()]}


def episodes(trades: pd.DataFrame, sessions: list) -> dict:
    """Holding episodes per symbol from signed fills; durations in sessions."""
    if trades.empty:
        return {"count": 0, "open": 0, "mean_days": None, "median_days": None,
                "win_rate": None, "durations": [], "bands": {}}
    idx = {d: i for i, d in enumerate(sessions)}
    out, bands = [], {}
    for symbol, g in trades.sort_values("ts").groupby("symbol"):
        qty = 0.0
        start = None
        pnl = 0.0
        for row in g.itertuples(index=False):
            signed = float(row.quantity)      # engine fills carry the sign
            cash = float(row.cash_delta_gbp)
            if qty <= 1e-9 and signed > 0:
                start, pnl = row.d, 0.0
            qty += signed
            pnl += cash
            if start is not None and qty <= 1e-9:
                dur = idx.get(row.d, 0) - idx.get(start, 0)
                out.append({"symbol": symbol, "start": str(start),
                            "end": str(row.d), "days": int(dur),
                            "pnl": pnl, "win": pnl > 0})
                bands.setdefault(symbol, []).append([str(start), str(row.d)])
                start = None
        if start is not None:
            bands.setdefault(symbol, []).append([str(start), None])
    closed = [e for e in out]
    durs = [e["days"] for e in closed]
    open_n = sum(1 for b in bands.values() for s in b if s[1] is None)
    return {"count": len(closed), "open": int(open_n),
            "mean_days": float(np.mean(durs)) if durs else None,
            "median_days": float(np.median(durs)) if durs else None,
            "win_rate": float(np.mean([e["win"] for e in closed])) if closed else None,
            "durations": durs, "bands": bands}


def costs(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"total": 0.0, "items": {}, "fx_share": None}
    items = {c[5:]: float(trades[c].sum()) for c in trades.columns
             if c.startswith("cost_")}
    total = sum(items.values())
    fx = items.get("currency_conversion_fee", 0.0)
    return {"total": total, "items": items,
            "fx_share": fx / total if total > 0 else None}


def occupancy(arm: dict, plan: dict, a0_names: list, sessions: list) -> dict:
    """Daily GBP value attributed to A0 names, A1 names and cash."""
    tr = arm["trades"]
    eq = arm["equity"]
    symbols = sorted(set(tr["symbol"])) if not tr.empty else []
    if not symbols:
        return {"a0": [0.0] * len(sessions), "a1": [0.0] * len(sessions),
                "cash": [float(v) for v in eq["equity_liq_gbp"]],
                "n_a0": [0] * len(sessions), "n_a1": [0] * len(sessions)}
    frames = load_bars(symbols + ["GBPUSD=X"], "1d", "2019-01-01", WINDOW_END)
    px = pd.DataFrame({s: f.assign(d=f["ts"].dt.tz_convert(NY).dt.date)
                       .groupby("d")["close"].last()
                       for s, f in frames.items() if s != "GBPUSD=X"})
    px = px.reindex(sessions).ffill()
    fxf = frames["GBPUSD=X"]
    fx = fxf.assign(d=fxf["ts"].dt.tz_convert("Europe/London").dt.date) \
        .groupby("d")["close"].last().reindex(sessions).ffill().bfill()
    signed = tr.assign(sq=tr["quantity"].astype(float))   # already signed
    pos = signed.pivot_table(index="d", columns="symbol", values="sq",
                             aggfunc="sum").reindex(sessions).fillna(0.0).cumsum()
    plan_days = sorted(plan)
    book_for = {}
    current = set()
    j = 0
    for d in sessions:
        while j < len(plan_days) and plan_days[j] <= d.isoformat():
            current = set(plan[plan_days[j]])
            j += 1
        book_for[d] = current
    a0v, a1v, n0, n1 = [], [], [], []
    a0set = set(a0_names)
    for d in sessions:
        v0 = v1 = 0.0
        k0 = k1 = 0
        for s in pos.columns:
            q = float(pos.at[d, s])
            if q <= 1e-9 or not np.isfinite(px.at[d, s]):
                continue
            val = q * float(px.at[d, s]) / float(fx.at[d])
            if s in book_for[d]:
                v1 += val; k1 += 1
            elif s in a0set:
                v0 += val; k0 += 1
            else:
                v1 += val; k1 += 1      # stale A1 name still held between plans
        a0v.append(v0); a1v.append(v1); n0.append(k0); n1.append(k1)
    eqv = [float(v) for v in eq["equity_liq_gbp"]]
    cash = [max(e - a - b, 0.0) for e, a, b in zip(eqv, a0v, a1v)]
    return {"a0": a0v, "a1": a1v, "cash": cash, "n_a0": n0, "n_a1": n1}


def build(priority: str) -> dict:
    sessions = us_sessions()
    plan_file = _find(f"a0_a1_plan_{priority}_20260902.json")
    plan_doc = json.loads(plan_file.read_text())
    plan, a0_names = plan_doc["plan"], plan_doc["a0_names"]
    arms = {}
    for tier in ("worst", "actual"):
        try:
            loaded = {stem: load_arm(stem, tier, sessions)
                      for stem in (f"merged_{priority}", "a0_solo", "a1_solo")}
        except FileNotFoundError as exc:
            print(f"tier {tier} skipped: {exc}")
            continue
        for stem, arm in loaded.items():
            key = f"{stem.split('_')[0]}/{tier}"
            curve = arm["equity"]["equity_liq_gbp"].astype(float)
            m = curve_metrics(curve, CAPITAL)
            ep = episodes(arm["trades"], sessions)
            trn = float(arm["trades"]["cash_delta_gbp"].abs().sum()) \
                if not arm["trades"].empty else 0.0
            entry = {"label": key, "curve": [float(v) for v in curve],
                     "metrics": m, "episodes": ep, "costs": costs(arm["trades"]),
                     "fills": int(len(arm["trades"])),
                     "rejected": int(arm["meta"].get("run", {}).get("orders_rejected",
                                     arm["meta"].get("orders_rejected", 0))),
                     "turnover_legs_per_yr": trn / float(curve.mean()) / m["years"],
                     "exposure_mean": float(1 - (arm["equity"]["cash_gbp"].astype(float)
                                                 / curve).mean())}
            if stem.startswith("merged"):
                entry["occupancy"] = occupancy(arm, plan, a0_names, sessions)
            arms[key] = entry
        # two-pot combination
        both = (pd.Series(arms[f"a0/{tier}"]["curve"], index=sessions)
                + pd.Series(arms[f"a1/{tier}"]["curve"], index=sessions))
        m2 = curve_metrics(both, 2 * CAPITAL)
        arms[f"twopot/{tier}"] = {"label": f"twopot/{tier}",
                                  "curve": [float(v) for v in both],
                                  "metrics": m2,
                                  "costs": {"total": arms[f"a0/{tier}"]["costs"]["total"]
                                            + arms[f"a1/{tier}"]["costs"]["total"]}}
        r = pd.DataFrame({k: pd.Series(arms[k]["curve"], index=sessions).pct_change()
                          for k in (f"merged/{tier}", f"a0/{tier}", f"a1/{tier}")}).dropna()
        arms[f"corr/{tier}"] = r.corr().round(3).to_dict()
    return {"priority": priority, "sessions": [str(d) for d in sessions],
            "capital": CAPITAL, "window": [WINDOW_START, WINDOW_END],
            "a0_names": a0_names, "n_plan": len(plan), "arms": arms}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--priority", default="a1")
    args = parser.parse_args()
    payload = build(args.priority)
    out = HERE / "merge_report_data.json"
    out.write_text(json.dumps(payload, ensure_ascii=False))
    print(f"written {out} ({out.stat().st_size/1e6:.1f} MB)")
    for k, v in payload["arms"].items():
        if "metrics" in v:
            m = v["metrics"]
            print(f"  {k:14s} CAGR {m['cagr']:+.2%} dd {m['max_dd']:.2%} "
                  f"sharpe {m['sharpe']:.2f} mwin {m['monthly_win']:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
