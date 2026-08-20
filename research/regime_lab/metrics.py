"""Performance metrics and the stationary bootstrap.

Conventions frozen for the study (backtest-discipline section 5):
    - Capital basis: 1.0 = the account, fully allocated across equal slots.
    - Annualization factor 252 (US equities), risk-free rate 0.
    - CAGR is compounded; ann_simple is mean daily return x 252. Both reported.
    - Max drawdown is measured on the compounded daily equity curve, as a
      fraction of the running peak.

Public functions:
    summarize(returns)                All headline metrics for one return series
    max_drawdown(returns)             Depth (positive fraction) and dates
    stationary_bootstrap(n, block, rng)   Index resampler (Politis-Romano 1994)
    bootstrap_ci(returns, metric, ...)    Percentile CI under the bootstrap
"""

from __future__ import annotations

__all__ = ["summarize", "max_drawdown", "stationary_bootstrap", "bootstrap_ci",
           "ANN_FACTOR"]

import numpy as np
import pandas as pd

ANN_FACTOR = 252


def max_drawdown(returns: pd.Series) -> tuple[float, pd.Timestamp | None, pd.Timestamp | None]:
    """Depth and location of the deepest peak-to-trough drawdown.

    Args:
        returns: Daily simple returns.

    Returns:
        (depth, peak_date, trough_date); depth is a positive fraction of the
        running peak (0.20 = -20%). Zeros for an empty or non-negative curve.
    """
    if len(returns) == 0:
        return 0.0, None, None
    equity = (1.0 + returns).cumprod()
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    trough = drawdown.idxmin()
    depth = -float(drawdown.min())
    peak_date = equity.loc[:trough].idxmax() if depth > 0 else None
    return depth, peak_date, trough if depth > 0 else None


def summarize(returns: pd.Series) -> dict:
    """Headline metrics for one daily return series.

    Args:
        returns: Daily simple returns, tz-naive DatetimeIndex, NaN-free.

    Returns:
        Dict with n_days, cagr, ann_simple, ann_vol, sharpe, max_dd, calmar,
        worst_day, best_day, positive_day_share.
    """
    n = len(returns)
    if n == 0:
        return {key: float("nan") for key in
                ("n_days", "cagr", "ann_simple", "ann_vol", "sharpe", "max_dd",
                 "calmar", "worst_day", "best_day", "positive_day_share")}
    equity_end = float((1.0 + returns).prod())
    cagr = equity_end ** (ANN_FACTOR / n) - 1.0
    ann_simple = float(returns.mean()) * ANN_FACTOR
    ann_vol = float(returns.std(ddof=1)) * np.sqrt(ANN_FACTOR) if n > 1 else float("nan")
    depth, _, _ = max_drawdown(returns)
    return {
        "n_days": n, "cagr": cagr, "ann_simple": ann_simple, "ann_vol": ann_vol,
        "sharpe": ann_simple / ann_vol if ann_vol and ann_vol > 0 else float("nan"),
        "max_dd": depth, "calmar": cagr / depth if depth > 0 else float("inf"),
        "worst_day": float(returns.min()), "best_day": float(returns.max()),
        "positive_day_share": float((returns > 0).mean()),
    }


def stationary_bootstrap(n: int, mean_block: float, rng: np.random.Generator) -> np.ndarray:
    """One stationary-bootstrap index sample (Politis & Romano 1994).

    Blocks start uniformly and end each step with probability 1/mean_block,
    wrapping circularly, which preserves weak dependence such as volatility
    clustering that an iid bootstrap would destroy.

    Args:
        n: Series length; the sample has the same length.
        mean_block: Expected block length in days.
        rng: Source of randomness (caller controls the seed).

    Returns:
        Integer index array of length n.
    """
    p = 1.0 / mean_block
    restart = rng.random(n) < p
    restart[0] = True
    starts = rng.integers(0, n, n)
    positions = np.arange(n)
    segment_start = np.maximum.accumulate(np.where(restart, positions, -1))
    return (starts[segment_start] + (positions - segment_start)) % n


def bootstrap_ci(
    returns: pd.Series,
    metric: str,
    n_boot: int = 2000,
    mean_block: float = 20.0,
    seed: int = 20260820,
    level: float = 0.90,
) -> tuple[float, float]:
    """Percentile confidence interval for cagr / max_dd / sharpe.

    Args:
        returns: Daily simple returns.
        metric: "cagr", "max_dd" or "sharpe".
        n_boot: Bootstrap replications.
        mean_block: Expected block length, days.
        seed: RNG seed, fixed so results are reproducible.
        level: Two-sided coverage.

    Returns:
        (low, high) percentile bounds.
    """
    rng = np.random.default_rng(seed)
    values = returns.to_numpy()
    n = len(values)
    outcomes = np.empty(n_boot)
    for b in range(n_boot):
        sample = pd.Series(values[stationary_bootstrap(n, mean_block, rng)])
        outcomes[b] = summarize(sample)[metric]
    alpha = (1.0 - level) / 2.0
    return float(np.quantile(outcomes, alpha)), float(np.quantile(outcomes, 1.0 - alpha))
