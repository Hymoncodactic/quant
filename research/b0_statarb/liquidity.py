"""Per-name liquidity: effective spread, participation and the capacity gates.

Responsibility: everything needed to answer "can GBP 10,000 actually trade this
name". Estimates the effective spread from daily OHLC with the Corwin-Schultz
(2012) high-low estimator, measures how large one order is against the name's
own turnover, and applies the capacity gates frozen in
research/prereg/20260823_b0_round2_prereg.md section 3.

Not responsible for pair selection (pairs.py), signals (engine.py) or the study
driver (run_study.py).

The estimator (Corwin & Schultz, Journal of Finance 67(2), 2012): over two
consecutive days, the high-low range of each single day reflects both the true
variance and the spread, while the two-day range reflects mostly variance.
Differencing the two isolates the spread:
    beta  = E[ (ln(H_t/L_t))^2 + (ln(H_t+1/L_t+1))^2 ]
    gamma = (ln(max(H_t,H_t+1)/min(L_t,L_t+1)))^2
    alpha = (sqrt(2*beta)-sqrt(beta))/(3-2*sqrt(2)) - sqrt(gamma/(3-2*sqrt(2)))
    S     = 2*(exp(alpha)-1)/(1+exp(alpha))
S is the PROPORTIONAL ROUND-TRIP spread; the half spread is S/2. Negative
estimates are floored at zero, as the authors prescribe.

Public functions:
    corwin_schultz(high, low)      Proportional round-trip spread series.
    name_liquidity(closes, ...)    Per-name spread, dollar volume, zero-volume share.
    capacity_gates(table, ...)     Apply the frozen gates; returns kept and dropped.

Constants:
    ORDER_GBP           float  500.0, one leg at GBP 10,000 over 20 pairs.
    GBPUSD_ASSUMED      float  1.28, only for expressing the order in USD.
    MAX_PARTICIPATION   float  0.001, order over median dollar volume.
    MIN_DOLLAR_VOLUME   float  1e6, the timeliness floor T2.
    MAX_ZERO_VOLUME     float  0.01, the timeliness floor T1.

Change log:
    2026-08-23  Created for B0 round 2 (small-cap extension and capacity).
"""

from __future__ import annotations

__all__ = ["corwin_schultz", "name_liquidity", "capacity_gates",
           "ORDER_GBP", "GBPUSD_ASSUMED", "MAX_PARTICIPATION",
           "MIN_DOLLAR_VOLUME", "MAX_ZERO_VOLUME"]

import numpy as np
import pandas as pd

ORDER_GBP = 500.0
GBPUSD_ASSUMED = 1.28
MAX_PARTICIPATION = 0.001
MIN_DOLLAR_VOLUME = 1e6
MAX_ZERO_VOLUME = 0.01

_K = 3.0 - 2.0 * np.sqrt(2.0)


def corwin_schultz(high: pd.Series, low: pd.Series) -> pd.Series:
    """Proportional round-trip spread per two-day window, floored at zero."""
    h, l = high.astype(float), low.astype(float)
    ok = (h > 0) & (l > 0)
    h, l = h.where(ok), l.where(ok)
    hl = np.log(h / l) ** 2
    beta = hl + hl.shift(-1)
    h2 = pd.concat([h, h.shift(-1)], axis=1).max(axis=1)
    l2 = pd.concat([l, l.shift(-1)], axis=1).min(axis=1)
    gamma = np.log(h2 / l2) ** 2
    alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / _K - np.sqrt(gamma / _K)
    spread = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))
    return spread.clip(lower=0.0)


def name_liquidity(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """One row per name: spread, dollar volume, zero-volume share, participation.

    frames maps ticker to a daily frame carrying high, low, close and volume.
    """
    rows = []
    order_usd = ORDER_GBP * GBPUSD_ASSUMED
    for ticker, frame in frames.items():
        if frame is None or frame.empty:
            continue
        dollar = (frame["close"] * frame["volume"]).dropna()
        if dollar.empty:
            continue
        median_dollar = float(dollar.median())
        spread = corwin_schultz(frame["high"], frame["low"]).dropna()
        rows.append({
            "ticker": ticker,
            "days": int(len(frame)),
            "median_dollar_volume": median_dollar,
            "half_spread_bps": float(spread.median() * 1e4 / 2.0)
                               if len(spread) else np.nan,
            "zero_volume_share": float((frame["volume"] <= 0).mean()),
            "participation": order_usd / median_dollar if median_dollar > 0
                             else np.inf,
        })
    return pd.DataFrame(rows).set_index("ticker")


def capacity_gates(table: pd.DataFrame,
                   max_participation: float = MAX_PARTICIPATION,
                   min_dollar: float = MIN_DOLLAR_VOLUME,
                   max_zero: float = MAX_ZERO_VOLUME
                   ) -> tuple[list[str], pd.DataFrame]:
    """Apply the frozen capacity gates; return (kept tickers, per-gate reasons).

    Gates are applied before any return is computed, so nothing here can be
    tuned against performance.
    """
    fail_part = table["participation"] >= max_participation
    fail_dollar = table["median_dollar_volume"] < min_dollar
    fail_zero = table["zero_volume_share"] > max_zero
    reasons = pd.DataFrame({
        "fail_participation": fail_part,
        "fail_dollar_volume": fail_dollar,
        "fail_zero_volume": fail_zero,
    })
    reasons["dropped"] = reasons.any(axis=1)
    return sorted(table.index[~reasons["dropped"]]), reasons
