"""Multiple-testing statistics: Hansen's SPA test and the Deflated Sharpe Ratio.

These answer the two honesty questions of a strategy search:
    SPA  - after searching K configurations, does the best one beat the
           benchmark by more than selection luck (Hansen 2005, JBES; the
           studentized, consistent-recentering refinement of White 2000)?
    DSR  - given K trials, is the selected configuration's Sharpe still
           positive after deflating by the expected maximum Sharpe of K
           skill-free trials (Bailey & Lopez de Prado 2014)?

Public functions:
    spa_test(family_returns, benchmark_returns, ...)   Consistent SPA p-value
    deflated_sharpe(selected, n_trials, trial_sharpe_variance)   DSR and inputs
"""

from __future__ import annotations

__all__ = ["spa_test", "deflated_sharpe"]

import numpy as np
import pandas as pd
from scipy import stats

from . import metrics as metrics_module


def spa_test(
    family_returns: pd.DataFrame,
    benchmark_returns: pd.Series,
    n_boot: int = 2000,
    mean_block: float = 20.0,
    seed: int = 20260820,
) -> dict:
    """Hansen (2005) Superior Predictive Ability test, consistent p-value.

    H0: no configuration in the family has positive expected excess return over
    the benchmark. f_k,t = r_k,t - r_bench,t; the statistic is the maximum
    studentized mean excess; the null distribution comes from the stationary
    bootstrap with Hansen's consistent recentering (poor performers are centered
    at their sample mean so they cannot inflate the null).

    Args:
        family_returns: Daily returns, one column per configuration, aligned.
        benchmark_returns: Daily benchmark returns on the same index.
        n_boot: Bootstrap replications.
        mean_block: Stationary-bootstrap expected block length, days.
        seed: RNG seed.

    Returns:
        Dict with t_spa, p_value (consistent), p_lower, p_upper, best_config,
        n_configs, n_days. p_lower centers every model at its mean (liberal);
        p_upper centers none (conservative, White's Reality Check).
    """
    excess = family_returns.sub(benchmark_returns, axis=0).dropna()
    values = excess.to_numpy()
    n, k = values.shape
    means = values.mean(axis=0)

    rng = np.random.default_rng(seed)
    boot_means = np.empty((n_boot, k))
    for b in range(n_boot):
        boot_means[b] = values[metrics_module.stationary_bootstrap(n, mean_block, rng)].mean(axis=0)
    omega = boot_means.std(axis=0, ddof=1) * np.sqrt(n)
    omega = np.where(omega <= 0, np.finfo(float).eps, omega)

    t_stats = np.sqrt(n) * means / omega
    t_spa = float(np.max(t_stats))

    threshold = -omega / np.sqrt(n) * np.sqrt(2.0 * np.log(np.log(max(n, 3))))
    center_consistent = np.where(means >= threshold, 0.0, means)
    # Hansen's lower null centers positive-mean models at zero, min(mean, 0);
    # centering at the raw mean would keep a winner's edge inside the null and
    # push p_lower toward 0.5 (defect found by independent formula audit,
    # 2026-08-20; direction of the old behavior was conservative).
    center_lower = np.minimum(means, 0.0)
    center_upper = np.zeros(k)

    def p_value(center: np.ndarray) -> float:
        stat = np.sqrt(n) * (boot_means - means + center) / omega
        return float((stat.max(axis=1) >= t_spa).mean())

    best = excess.columns[int(np.argmax(t_stats))]
    return {"t_spa": t_spa, "p_value": p_value(center_consistent),
            "p_lower": p_value(center_lower), "p_upper": p_value(center_upper),
            "best_config": str(best), "n_configs": k, "n_days": n}


def deflated_sharpe(
    selected: pd.Series,
    n_trials: int,
    trial_sharpe_variance: float,
) -> dict:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014).

    Steps, all in per-day units: SR_hat = mean/std of the selected series;
    the expected maximum skill-free Sharpe over n_trials is
        SR0 = sqrt(V) * ((1 - gamma) * z(1 - 1/N) + gamma * z(1 - 1/(N e)))
    with gamma the Euler-Mascheroni constant and V the cross-trial variance of
    the family's Sharpe estimates; DSR is the PSR of SR_hat against SR0 with
    the skew/kurtosis-adjusted standard error.

    Args:
        selected: Daily returns of the selected configuration.
        n_trials: Number of configurations searched (the whole frozen family).
        trial_sharpe_variance: Variance across the family of per-day Sharpes.

    Returns:
        Dict with sr_daily, sr_annual, sr0_daily, dsr (probability the true
        Sharpe exceeds the luck benchmark), skew, kurtosis, n_days.
    """
    values = selected.dropna().to_numpy()
    n = len(values)
    sr = float(values.mean() / values.std(ddof=1))
    skew = float(stats.skew(values))
    kurt = float(stats.kurtosis(values, fisher=False))

    gamma = 0.5772156649015329
    z = stats.norm.ppf
    sr0 = float(np.sqrt(max(trial_sharpe_variance, 0.0))
                * ((1.0 - gamma) * z(1.0 - 1.0 / n_trials)
                   + gamma * z(1.0 - 1.0 / (n_trials * np.e))))

    denom = np.sqrt(max(1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2, 1e-12))
    dsr = float(stats.norm.cdf((sr - sr0) * np.sqrt(n - 1) / denom))
    return {"sr_daily": sr, "sr_annual": sr * np.sqrt(252.0), "sr0_daily": sr0,
            "dsr": dsr, "skew": skew, "kurtosis": kurt, "n_days": n}
