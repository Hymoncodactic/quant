"""Wide-universe cross-sectional momentum family, frozen 16 configs, vs A0.

Responsibility: the research layer of research/prereg/20260902_xsmom_wide_prereg.md.
Loads the frozen 1,500-name universe through the B0 loaders, applies the B0
capacity gates, builds the daily A0 market-gate series from QQQ (identical
parameters and 2010-01-04 history start), runs the 16-config family with
share-based accounting (decision at t close, executed at t+1 close, holdings
drift between monthly rebalances, gate flips charged as turnover), picks the
winner by Selection-window Calmar, and runs the two probes: a deliberate
lookahead arm and a 300-draw random-selection luck baseline.

Costs at this layer: 16 bp per leg (15 bp FX + 1 bp half spread) on both-legs
turnover. The winner's cost-true verdict comes from the separate framework
run (scripts/20260902_xsmom_a0_headtohead.py), not from here.

Public functions:
    gate_series()          Daily A0 gate multiplier from QQQ, causal.
    momentum_scores()      12-1 momentum panel.
    run_config(...)        One config's daily value path and turnover.
    main()                 The whole study; writes results/*.csv.

Constants:
    SEL_START/SEL_END/VAL_START/VAL_END   The frozen two-stage split.
    REBALANCE_EVERY   int  21 sessions.
    COST_PER_LEG_BPS  float 16.0.

Change log:
    2026-09-02  Created for the wide-universe selection study.
    2026-09-02  Adversarial-review fixes (research/decisions ruling section on
                the review): (a) the liquidity screen is now CAUSAL -- a
                rolling 252-session eligibility mask replaces the full-sample
                capacity gates, whose future-conditioned admission flipped the
                frozen winner's identity (blocker, doubly reproduced);
                (b) valuation uses last-known prices so a data hole no longer
                zeroes a held name (majors 16/17); (c) trade legs are charged
                only for executable names (minor 19); (d) the prereg's
                declared cutoff assertion is now actually implemented
                (assert_scores_causal, minor 6); (e) the as-of-day 300-bar
                history rule rides inside the mask (minor 7).
"""

from __future__ import annotations

__all__ = ["gate_series", "momentum_scores", "run_config", "main"]

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from research.b0_statarb.run_round2 import _load_frame              # noqa: E402

RESULTS = HERE / "results"
NY = "America/New_York"
UNIVERSE_JSON = ROOT / "data" / "reference" / "b0_universe_1500_20260823.json"

SEL_START, SEL_END = "2012-01-02", "2019-12-31"
VAL_START, VAL_END = "2020-01-02", "2026-08-28"
GATE_HISTORY_START = "2010-01-04"          # A0's frozen percentile anchor
REBALANCE_EVERY = 21
COST_PER_LEG_BPS = 16.0
MOM_LONG, MOM_SKIP = 252, 21
IVOL_WINDOW = 60
MIN_OBS = 300

FAMILY = list(itertools.product((20, 50), ("EW", "IVOL"), (False, True),
                                (False, True)))   # N, weight, band, gates

ORDER_USD = 640.0            # one GBP-500 leg at GBPUSD ~1.28 (capacity anchor)


def load_panels(members: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Close and volume panels for the frozen universe, exchange-local dates."""
    closes, volumes = {}, {}
    for member in members:
        ticker = member["ticker"]
        frame = _load_frame(ticker)
        if frame is None:
            continue
        series = frame.set_index("date")
        closes[ticker] = series["close"]
        volumes[ticker] = series["volume"]
    c = pd.DataFrame(closes).sort_index()
    return c, pd.DataFrame(volumes).reindex(c.index)


def eligibility(closes: pd.DataFrame, volumes: pd.DataFrame) -> pd.DataFrame:
    """CAUSAL liquidity mask: eligible at t from data up to and including t.

    Replaces the full-sample capacity gates after the adversarial review
    showed full-history medians condition admission on future liquidity
    growth and flip the frozen winner. Same thresholds as the B0 gates,
    evaluated on a rolling 252-session window: trailing median dollar volume
    at least USD 1M (which also caps a GBP-500 order's participation at
    0.064 bps), fewer than 1% zero-volume sessions, and at least 300 bars of
    as-of-day history.
    """
    dollar = closes * volumes
    med = dollar.rolling(252, min_periods=252).median()
    traded = ((volumes > 0) & closes.notna()).astype(float)
    zero_share = 1.0 - traded.rolling(252, min_periods=252).mean()
    obs = closes.notna().cumsum()
    mask = (med >= 1e6) & (zero_share < 0.01) & (obs >= 300)         & ((ORDER_USD / med) < 0.001)
    return mask.fillna(False)


def assert_scores_causal(closes: pd.DataFrame, scores: pd.DataFrame) -> None:
    """The prereg's declared cutoff control, actually executed.

    Recomputes the score row for three sampled dates from a panel truncated
    AT that date and asserts bitwise agreement: any dependence on later data
    would break the equality.
    """
    dates = list(scores.index)
    for idx in (len(dates) // 3, len(dates) // 2, len(dates) - 30):
        day = dates[idx]
        truncated = momentum_scores(closes.loc[:day]).loc[day]
        full = scores.loc[day]
        both = pd.concat([truncated.rename("t"), full.rename("f")], axis=1)
        same = (both["t"] == both["f"]) | (both["t"].isna() & both["f"].isna())
        assert bool(same.all()), f"score at {day} depends on later data"


def _yang_zhang(o, h, l, c, w=20):
    o_ = np.log(o[1:] / c[:-1]); c_ = np.log(c[1:] / o[1:])
    u = np.log(h[1:] / o[1:]); d = np.log(l[1:] / o[1:])
    rs = pd.Series(u * (u - c_) + d * (d - c_))
    k = 0.34 / (1.34 + (w + 1) / (w - 1))
    var = (pd.Series(o_).rolling(w).var(ddof=1)
           + k * pd.Series(c_).rolling(w).var(ddof=1)
           + (1 - k) * rs.rolling(w).mean()).clip(lower=0.0)
    return np.sqrt(var.to_numpy() * 252.0)


def gate_series() -> pd.Series:
    """A0's two market gates as one daily 0/1 multiplier, causal.

    Identical construction to trading212/strategy/a0_v0_0_1.py: QQQ below its
    SMA200, or Yang-Zhang(20) annualized vol at or above the 0.80 expanding
    percentile (minimum 756 observations, history from 2010-01-04). The value
    AT date t uses bars up to and including t; the caller applies it with a
    one-day execution lag like every other decision.
    """
    import glob
    parts = sorted(glob.glob(str(ROOT / "data/t212/curated/us_etf/QQQ/1d/*.parquet")))
    frame = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    frame["d"] = frame["ts"].dt.tz_convert(NY).dt.date
    frame = frame[frame["d"] >= pd.Timestamp(GATE_HISTORY_START).date()]
    frame = frame.sort_values("d").drop_duplicates("d", keep="last")
    o, h, l, c = (frame[k].to_numpy() for k in ("open", "high", "low", "close"))
    close = pd.Series(c, index=frame["d"].to_numpy())
    sma = close.rolling(200).mean()
    trend_ok = ~(close < sma)                       # NaN sma -> gate open
    vol = pd.Series(np.concatenate([[np.nan], _yang_zhang(o, h, l, c)]),
                    index=close.index)
    valid = vol.dropna()
    pct = pd.Series(np.nan, index=close.index)
    ranks = valid.expanding().apply(
        lambda a: float((a <= a[-1]).mean()), raw=True)
    counts = valid.expanding().count()
    pct.loc[valid.index] = ranks.where(counts >= 756)
    vol_ok = ~(pct >= 0.80)                         # NaN pct -> gate open
    return (trend_ok & vol_ok).astype(float)


def momentum_scores(closes: pd.DataFrame) -> pd.DataFrame:
    """12-1 momentum: total return from t-252 to t-21, NaN when short."""
    return closes.shift(MOM_SKIP) / closes.shift(MOM_LONG) - 1.0


def run_config(closes: pd.DataFrame, scores: pd.DataFrame, gate: pd.Series,
               n_hold: int, weighting: str, band: bool, use_gates: bool,
               start: str, end: str, seed_names: np.random.Generator | None = None,
               elig: pd.DataFrame | None = None,
               px_ffill: pd.DataFrame | None = None
               ) -> tuple[pd.Series, float]:
    """Daily portfolio value (start=1.0) and total both-legs turnover.

    Decision at day t close; executed at day t+1 close. Holdings are SHARES,
    so weights drift between rebalances. A gate close liquidates at the next
    close and a reopen rebuys the remembered book, both charged as turnover.
    seed_names, when given, replaces the momentum ranking with a uniformly
    random draw of n_hold eligible names (the luck baseline).
    """
    dates = [d for d in closes.index
             if pd.Timestamp(start).date() <= d <= pd.Timestamp(end).date()]
    vols = closes.pct_change().rolling(IVOL_WINDOW).std(ddof=1)
    if px_ffill is None:
        px_ffill = closes.ffill()
    value, cash = 1.0, 1.0
    shares: dict[str, float] = {}
    book: dict[str, float] = {}            # remembered target weights
    pending: dict | None = None            # decided at t, trade at t+1 close
    gate_state = 1.0
    turnover = 0.0
    path = []
    since_reb = REBALANCE_EVERY            # force rebalance on first day
    for day in dates:
        px = closes.loc[day]
        pxf = px_ffill.loc[day]
        # Valuation uses last-known prices: a data hole must not zero a held
        # name (review majors 16/17). Trading additionally requires a finite
        # CURRENT price; an untradable held name is frozen, not annihilated,
        # and charges no leg (review minor 19).
        if pending is not None:
            target_w, g = pending
            value = cash + sum(s * pxf[t] for t, s in shares.items()
                               if np.isfinite(pxf.get(t, np.nan)))
            eff = {t: w * g for t, w in target_w.items()}
            old_w = {t: (s * pxf[t] / value if value > 0 else 0.0)
                     for t, s in shares.items()
                     if np.isfinite(pxf.get(t, np.nan))}

            def tradable(t):
                v = px.get(t, np.nan)
                return np.isfinite(v) and v > 0
            legs = sum(abs(eff.get(t, 0.0) - old_w.get(t, 0.0))
                       for t in set(eff) | set(old_w) if tradable(t))
            turnover += legs
            cost = value * legs * COST_PER_LEG_BPS / 1e4
            value -= cost
            frozen = {t: q for t, q in shares.items() if not tradable(t)}
            frozen_val = sum(q * pxf[t] for t, q in frozen.items()
                             if np.isfinite(pxf.get(t, np.nan)))
            new_shares = dict(frozen)
            invested = 0.0
            for t, w in eff.items():
                if t in frozen or w <= 0 or not tradable(t):
                    continue
                new_shares[t] = value * w / px[t]
                invested += value * w
            shares = new_shares
            cash = value - invested - frozen_val
            pending = None
        else:
            value = cash + sum(s * pxf[t] for t, s in shares.items()
                               if np.isfinite(pxf.get(t, np.nan)))
        path.append((day, value))
        # 2) decide at today's close
        since_reb += 1
        g_now = gate.get(day, 1.0) if use_gates else 1.0
        need_reb = since_reb > REBALANCE_EVERY
        if need_reb:
            since_reb = 1
            row = scores.loc[day].dropna()
            if elig is not None:
                ok = elig.loc[day]
                row = row[[t for t in row.index if bool(ok.get(t, False))]]
            eligible = row.index
            if seed_names is not None:
                pick = list(seed_names.choice(eligible,
                                              size=min(n_hold, len(eligible)),
                                              replace=False))
            else:
                ranked = row.sort_values(ascending=False)
                if band and book:
                    keep = [t for t in book
                            if t in ranked.index[:2 * n_hold]]
                    fresh = [t for t in ranked.index if t not in keep]
                    pick = keep + fresh[:max(0, n_hold - len(keep))]
                else:
                    pick = list(ranked.index[:n_hold])
            if weighting == "IVOL":
                v = vols.loc[day, pick].replace(0, np.nan)
                inv = (1.0 / v).fillna(0.0)
                w = inv / inv.sum() if inv.sum() > 0 else \
                    pd.Series(1.0 / len(pick), index=pick)
            else:
                w = pd.Series(1.0 / len(pick), index=pick)
            book = w.to_dict()
        if need_reb or (use_gates and g_now != gate_state):
            pending = (dict(book), g_now)
            gate_state = g_now
    curve = pd.Series(dict(path)).sort_index()
    return curve, turnover


def metrics(curve: pd.Series, label: str) -> dict:
    rets = curve.pct_change().dropna()
    years = len(curve) / 252.0
    cagr = float(curve.iloc[-1] ** (1 / years) - 1) if years > 0 else np.nan
    dd = float(-(curve / curve.cummax() - 1.0).min())
    monthly = curve.resample("ME").last() if isinstance(curve.index, pd.DatetimeIndex) \
        else curve.groupby(pd.PeriodIndex(pd.to_datetime(curve.index), freq="M")).last()
    mrets = monthly.pct_change().dropna()
    return {"config": label, "days": len(curve), "cagr": cagr,
            "ann_vol": float(rets.std(ddof=1) * np.sqrt(252)),
            "max_dd": dd, "calmar": cagr / dd if dd > 0 else np.nan,
            "monthly_win": float((mrets > 0).mean()),
            "sharpe": float(rets.mean() / rets.std(ddof=1) * np.sqrt(252))
            if rets.std(ddof=1) > 0 else np.nan}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-random", action="store_true")
    args = parser.parse_args()
    RESULTS.mkdir(exist_ok=True)

    payload = json.loads(UNIVERSE_JSON.read_text())
    closes, volumes = load_panels(payload["members"])
    closes = closes[closes.index >= pd.Timestamp("2010-01-04").date()]
    volumes = volumes.reindex(closes.index)
    elig = eligibility(closes, volumes)
    px_ffill = closes.ffill()
    sel0 = pd.Timestamp(SEL_START).date()
    first_elig = elig.loc[[d for d in elig.index if d >= sel0][0]]
    print(f"panel {closes.shape[0]} days x {closes.shape[1]} names; "
          f"causally eligible on the first selection day: "
          f"{int(first_elig.sum())}", flush=True)

    gate = gate_series()
    scores = momentum_scores(closes)
    assert_scores_causal(closes, scores)
    print("cutoff assertion passed (3 sampled dates)", flush=True)
    gate_off_share = float((gate.reindex(closes.index).fillna(1.0) == 0).mean())
    print(f"gate closed on {gate_off_share:.1%} of days", flush=True)

    rows = []
    curves_v = {}
    for n_hold, weighting, band, gates_on in FAMILY:
        label = f"N{n_hold}|{weighting}|band{'+' if band else '-'}|gate{'+' if gates_on else '-'}"
        cs, to_s = run_config(closes, scores, gate, n_hold, weighting, band,
                              gates_on, SEL_START, SEL_END,
                              elig=elig, px_ffill=px_ffill)
        cv, to_v = run_config(closes, scores, gate, n_hold, weighting, band,
                              gates_on, VAL_START, VAL_END,
                              elig=elig, px_ffill=px_ffill)
        ms, mv = metrics(cs, label), metrics(cv, label)
        years_s, years_v = len(cs) / 252, len(cv) / 252
        rows.append({"config": label,
                     "S_cagr": ms["cagr"], "S_dd": ms["max_dd"],
                     "S_calmar": ms["calmar"],
                     "V_cagr": mv["cagr"], "V_dd": mv["max_dd"],
                     "V_calmar": mv["calmar"], "V_vol": mv["ann_vol"],
                     "V_monthly_win": mv["monthly_win"],
                     "V_sharpe": mv["sharpe"],
                     "S_turnover_yr": to_s / years_s,
                     "V_turnover_yr": to_v / years_v})
        curves_v[label] = cv
        print(f"  {label:26s} S: {ms['cagr']:+.1%}/{ms['max_dd']:.1%} "
              f"calmar {ms['calmar']:.2f} | V: {mv['cagr']:+.1%}/{mv['max_dd']:.1%} "
              f"win {mv['monthly_win']:.0%} to {to_v/years_v:.1f}x", flush=True)

    fam = pd.DataFrame(rows).sort_values("S_calmar", ascending=False)
    fam.to_csv(RESULTS / "family_causal_20260902.csv", index=False)
    winner = fam.iloc[0]["config"]
    print(f"\nS 段 Calmar 第一名（冻结规则）: {winner}", flush=True)
    pd.DataFrame({k: v for k, v in curves_v.items()}).to_csv(
        RESULTS / "curves_v_causal_20260902.csv")

    # --- lookahead probe: rank by FUTURE 21d return (selection window) ---
    future = closes.shift(-MOM_SKIP) / closes - 1.0
    n_hold, weighting, band, gates_on = FAMILY[0]
    cp, _ = run_config(closes, future, gate, 20, "EW", False, False,
                       SEL_START, SEL_END, elig=elig, px_ffill=px_ffill)
    mp = metrics(cp, "probe")
    base = fam[fam.config == "N20|EW|band-|gate-"].iloc[0]
    print(f"前视探针 S 段: CAGR {mp['cagr']:+.1%} "
          f"（正常 N20|EW {base.S_cagr:+.1%}）-> "
          f"{'判别力成立' if mp['cagr'] > base.S_cagr + 0.5 else '★探针未爆炸，检查★'}",
          flush=True)

    # --- random-selection luck baseline on the winner's config ---
    if not args.skip_random:
        wn, ww, wb, wg = next(f for f in FAMILY
                              if f"N{f[0]}|{f[1]}|band{'+' if f[2] else '-'}|gate{'+' if f[3] else '-'}" == winner)
        rng = np.random.default_rng(20260902)
        calmars = []
        for i in range(300):
            cr, _ = run_config(closes, scores, gate, wn, ww, wb, wg,
                               VAL_START, VAL_END, seed_names=rng,
                               elig=elig, px_ffill=px_ffill)
            m = metrics(cr, f"rand{i}")
            calmars.append(m["calmar"])
            if (i + 1) % 50 == 0:
                print(f"  random {i+1}/300", flush=True)
        arr = np.array([c for c in calmars if np.isfinite(c)])
        np.save(RESULTS / "random_calmars_causal_20260902.npy", arr)
        wv = fam[fam.config == winner].iloc[0]["V_calmar"]
        p95 = float(np.quantile(arr, 0.95))
        print(f"随机基线 V 段 Calmar: 中位 {np.median(arr):.2f}  p95 {p95:.2f}  "
              f"赢家 {wv:.2f} -> {'R5 通过' if wv > p95 else 'R5 不通过'}", flush=True)

    print("\n" + fam.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
