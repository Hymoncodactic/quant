"""Answer the B0 pre-registered doubts from the study's daily return series.

Responsibility: the checks that decide whether B0 is arbitrage or repackaged
market exposure. Reads only research/b0_statarb/results/daily_returns.csv and
stored price data; runs no new backtest.

Checks, keyed to research/prereg/20260823_b0_statarb_prereg.md:
    D1  Regress each variant on SPY. A long-only variant whose return is
        explained by beta is not statistical arbitrage, whatever its Sharpe.
    C5  Correlation with A0's daily return (orthogonality gate, < 0.30).
    D5  Year-by-year return, to see whether the edge decays monotonically.
    C3  Split-half stability: both halves positive and same sign.

Public functions:
    market_regression(returns, market)  Alpha, beta and their Newey-West t values.
    main()                              Run every check and print the table.

Change log:
    2026-08-23  Created for the B0 statistical-arbitrage study.
"""

from __future__ import annotations

__all__ = ["market_regression", "main"]

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

RESULTS = HERE / "results"
NY = "America/New_York"


def _nw_se(resid: np.ndarray, x: np.ndarray, lags: int = 20) -> np.ndarray:
    """Newey-West standard errors of an OLS slope vector."""
    n, k = x.shape
    xtx_inv = np.linalg.pinv(x.T @ x)
    s = (resid[:, None] * x)
    meat = s.T @ s
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1.0)
        gamma = s[lag:].T @ s[:-lag]
        meat += w * (gamma + gamma.T)
    cov = xtx_inv @ meat @ xtx_inv
    return np.sqrt(np.diag(cov))


def market_regression(returns: pd.Series, market: pd.Series) -> dict:
    """Regress a strategy's daily return on the market's; report alpha and beta."""
    joined = pd.concat([returns.rename("r"), market.rename("m")], axis=1).dropna()
    if len(joined) < 100:
        return {}
    x = np.column_stack([np.ones(len(joined)), joined["m"].to_numpy()])
    y = joined["r"].to_numpy()
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ coef
    se = _nw_se(resid, x)
    return {"alpha_ann": float(coef[0] * 252), "alpha_t": float(coef[0] / se[0]),
            "beta": float(coef[1]), "beta_t": float(coef[1] / se[1]),
            "r2": float(1 - resid.var() / y.var()), "n": len(joined)}


def _spy() -> pd.Series:
    parts = sorted(glob.glob(str(ROOT / "data/t212/curated/us_etf/SPY/1d/*.parquet")))
    frame = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    frame["date"] = pd.to_datetime(frame["ts"], utc=True).dt.tz_convert(NY).dt.date
    series = frame.sort_values("date").set_index("date")["close"]
    return series.pct_change().dropna()


def _a0() -> pd.Series | None:
    hits = sorted(glob.glob(str(ROOT / "backtest/results/"
                                 "a0_intraday_v0_0_1_1h_same_close_actual_*"
                                 "_fee-actual_fill-same_close_*.equity.parquet")))
    if not hits:
        return None
    eq = pd.read_parquet(hits[-1])
    eq["date"] = pd.to_datetime(eq["ts"], utc=True).dt.tz_convert(NY).dt.date
    daily = eq.groupby("date")["equity_liq_gbp"].last()
    return daily.pct_change().dropna()


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--returns", default="daily_returns.csv",
                        help="file under results/ holding the daily series")
    parser.add_argument("--max-drawdown", type=float, default=0.15,
                        help="C7 drawdown ceiling")
    parser.add_argument("--max-vol", type=float, default=0.12,
                        help="C7 annualized volatility ceiling")
    args = parser.parse_args()
    path = RESULTS / args.returns
    if not path.is_file():
        print("daily_returns.csv absent; run run_study.py first")
        return 1
    series = pd.read_csv(path, index_col=0)
    series.index = pd.to_datetime(series.index).date
    spy, a0 = _spy(), _a0()

    print("=== D1 市场回归：alpha 与 beta（Newey-West(20) t）===")
    print(f"{'config':26s} {'alpha年化':>10s} {'t(a)':>7s} {'beta':>7s} "
          f"{'t(b)':>7s} {'R2':>6s}")
    rows = []
    for col in series.columns:
        reg = market_regression(series[col].dropna(), spy)
        if not reg:
            continue
        rows.append({"config": col, **reg})
        print(f"{col:26s} {reg['alpha_ann']:+10.2%} {reg['alpha_t']:+7.2f} "
              f"{reg['beta']:+7.3f} {reg['beta_t']:+7.2f} {reg['r2']:6.3f}")
    pd.DataFrame(rows).to_csv(RESULTS / "market_regression.csv", index=False)

    print("\n=== C5 与 A0 的相关性（门槛 < 0.30）===")
    if a0 is None:
        print("  A0 权益件缺失，跳过")
    else:
        for col in series.columns:
            joined = pd.concat([series[col].rename("b"), a0.rename("a")],
                               axis=1).dropna()
            if len(joined) < 60:
                continue
            rho = float(joined["b"].corr(joined["a"]))
            print(f"  {col:26s} rho={rho:+.3f}  n={len(joined)}  "
                  f"{'通过' if abs(rho) < 0.30 else '不通过'}")

    print("\n=== D5 分年收益（actual 档）===")
    cols = [c for c in series.columns
            if c.endswith("|actual") or c.endswith("|spread")]
    frame = series[cols].copy()
    frame["year"] = [d.year for d in frame.index]
    annual = frame.groupby("year").apply(
        lambda g: (1 + g[cols]).prod() - 1, include_groups=False)
    print(annual.to_string(float_format=lambda v: f"{v:+.2%}"))

    print("\n=== C7 低风险配置门槛（回撤 < "
          f"{args.max_drawdown:.0%}，年化波动 < {args.max_vol:.0%}）===")
    for col in series.columns:
        s_ = series[col].dropna()
        if len(s_) < 60:
            continue
        curve = (1 + s_).cumprod()
        mdd = float(-(curve / curve.cummax() - 1).min())
        vol = float(s_.std(ddof=1) * np.sqrt(252))
        ok = (mdd < args.max_drawdown) and (vol < args.max_vol)
        print(f"  {col:26s} 回撤 {mdd:6.2%}  波动 {vol:6.2%}  "
              f"{'通过' if ok else '不通过'}")

    print("\n=== C3 分半样本稳定性（actual 档）===")
    for col in cols:
        s = series[col].dropna()
        half = len(s) // 2
        h1 = float((1 + s.iloc[:half]).prod() - 1)
        h2 = float((1 + s.iloc[half:]).prod() - 1)
        ok = (h1 > 0) and (h2 > 0)
        print(f"  {col:26s} 前半 {h1:+.2%}  后半 {h2:+.2%}  "
              f"{'通过' if ok else '不通过'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
