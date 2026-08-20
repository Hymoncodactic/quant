"""KDJ and MACD, computed as strictly causal recursions.

Responsibility: turn an OHLC frame into indicator series. Every value at index i
depends only on rows 0..i, which is what makes the look-ahead assertions in
engine.py meaningful. Not responsible for: signal rules, trade construction.

Definitions are frozen in research/prereg/20260820_kdj_macd_daily_prereg.md
section 2.1 and follow the Chinese charting-package conventions:

    RSV_t = (C_t - LLV(L,n)_t) / (HHV(H,n)_t - LLV(L,n)_t) * 100
    K_t   = (1 - 1/m1) * K_{t-1} + (1/m1) * RSV_t        K_0 seeded at 50
    D_t   = (1 - 1/m2) * D_{t-1} + (1/m2) * K_t          D_0 seeded at 50
    J_t   = 3*K_t - 2*D_t

    DIF_t  = EMA(C, fast)_t - EMA(C, slow)_t             EMA seeded at C_0
    DEA_t  = EMA(DIF, signal)_t                          seeded at DIF_0
    HIST_t = 2 * (DIF_t - DEA_t)

Public functions:
    kdj(high, low, close, n, m1, m2)      K, D, J and the flat-range counter
    macd(close, fast, slow, signal)       DIF, DEA, HIST
    add_indicators(bars, ...)             Both, appended to a copy of the frame
"""

from __future__ import annotations

__all__ = ["kdj", "macd", "add_indicators", "KDJ_SEED", "FLAT_RANGE_RSV"]

import numpy as np
import pandas as pd

# Seed for the K and D recursions. Mainstream charting packages start the
# stochastic at the mid point rather than at the first RSV.
KDJ_SEED = 50.0

# RSV substitute when the n-day high equals the n-day low. The ratio is 0/0 and
# has no natural value; 50 is the neutral choice and the occurrence count is
# reported so a symbol where this fires often cannot pass unnoticed.
FLAT_RANGE_RSV = 50.0


def kdj(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    n: int = 9,
    m1: int = 3,
    m2: int = 3,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Compute the KDJ stochastic.

    Args:
        high: High prices, price units, ascending by time.
        low: Low prices, same length and order as high.
        close: Close prices, same length and order as high.
        n: Look-back for the highest high and lowest low, in bars.
        m1: Smoothing divisor for K.
        m2: Smoothing divisor for D.

    Returns:
        Tuple (k, d, j, flat_range_count). K and D are float64 arrays on a 0..100
        scale; J is unbounded. flat_range_count is the number of bars where the
        n-day range was zero and FLAT_RANGE_RSV was substituted.

    Raises:
        ValueError: Inputs have different lengths, or n / m1 / m2 are not positive.
    """
    if not (len(high) == len(low) == len(close)):
        raise ValueError("high, low and close must have equal length")
    if min(n, m1, m2) < 1:
        raise ValueError("n, m1 and m2 must be positive")

    # min_periods=1 so the first n-1 bars use the bars available so far, which is
    # what LLV/HHV do in the charting packages. Those bars fall inside the warm-up
    # window anyway and never produce a signal.
    llv = pd.Series(low).rolling(n, min_periods=1).min().to_numpy()
    hhv = pd.Series(high).rolling(n, min_periods=1).max().to_numpy()

    span = hhv - llv
    flat = span <= 0.0
    rsv = np.zeros(len(close), dtype=np.float64)
    np.divide(close - llv, span, out=rsv, where=~flat)
    rsv *= 100.0
    rsv[flat] = FLAT_RANGE_RSV

    k = np.empty(len(close), dtype=np.float64)
    d = np.empty(len(close), dtype=np.float64)
    k_prev = KDJ_SEED
    d_prev = KDJ_SEED
    a1 = 1.0 / m1
    a2 = 1.0 / m2
    for i in range(len(close)):
        k_prev = (1.0 - a1) * k_prev + a1 * rsv[i]
        d_prev = (1.0 - a2) * d_prev + a2 * k_prev
        k[i] = k_prev
        d[i] = d_prev

    return k, d, 3.0 * k - 2.0 * d, int(flat.sum())


def macd(
    close: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute DIF, DEA and the MACD histogram.

    Args:
        close: Close prices, price units, ascending by time.
        fast: Fast EMA length in bars.
        slow: Slow EMA length in bars.
        signal: EMA length applied to DIF, in bars.

    Returns:
        Tuple (dif, dea, hist) as float64 arrays in price units. hist follows the
        doubled convention, hist = 2 * (dif - dea).

    Raises:
        ValueError: Any length is not positive, or fast is not shorter than slow.
    """
    if min(fast, slow, signal) < 1:
        raise ValueError("fast, slow and signal must be positive")
    if fast >= slow:
        raise ValueError("fast must be shorter than slow")

    series = pd.Series(close, dtype="float64")
    # adjust=False seeds the recursion at the first observation, matching the
    # charting packages. adjust=True would apply an expanding-window weighting
    # and give different early values.
    ema_fast = series.ewm(span=fast, adjust=False).mean().to_numpy()
    ema_slow = series.ewm(span=slow, adjust=False).mean().to_numpy()
    dif = ema_fast - ema_slow
    dea = pd.Series(dif).ewm(span=signal, adjust=False).mean().to_numpy()
    return dif, dea, 2.0 * (dif - dea)


def add_indicators(
    bars: pd.DataFrame,
    kdj_params: tuple[int, int, int] = (9, 3, 3),
    macd_params: tuple[int, int, int] = (12, 26, 9),
) -> tuple[pd.DataFrame, int]:
    """Append K, D, J, DIF, DEA and HIST to a copy of an OHLC frame.

    Args:
        bars: Frame with high, low and close columns, ascending by date.
        kdj_params: (n, m1, m2) for kdj().
        macd_params: (fast, slow, signal) for macd().

    Returns:
        Tuple (frame, flat_range_count) where frame is a copy of bars with the
        six indicator columns added, and flat_range_count is the KDJ zero-range
        occurrence count.
    """
    out = bars.copy()
    k, d, j, flat_count = kdj(
        out["high"].to_numpy(), out["low"].to_numpy(), out["close"].to_numpy(), *kdj_params
    )
    dif, dea, hist = macd(out["close"].to_numpy(), *macd_params)
    out["k"], out["d"], out["j"] = k, d, j
    out["dif"], out["dea"], out["hist"] = dif, dea, hist
    return out, flat_count
