"""Matched-horizon unconditional benchmark and its significance test.

Responsibility: answer "how often would an arbitrary entry in the same symbol,
same window and same holding length have been profitable". Without it a raw win
rate on large-cap US tech over a rising sample says nothing, because the
unconditional rate is already far above 50 percent. Prereg section 3.1.

Not responsible for: trade construction, reporting.

Public functions:
    horizon_win_rate(prices, first_bar, last_bar, hold_bars)
    benchmark_trades(prices, trades, first_bar, last_bar)
    poisson_binomial_test(wins, probabilities)
"""

from __future__ import annotations

__all__ = ["horizon_win_rate", "benchmark_trades", "poisson_binomial_test"]

import math

import numpy as np
import pandas as pd
from scipy import stats


def horizon_win_rate(
    prices: np.ndarray,
    first_bar: int,
    last_bar: int,
    hold_bars: int,
) -> tuple[float, int]:
    """Fraction of overlapping hold_bars windows that ended higher.

    Args:
        prices: Fill-price series, the same series the trades were filled on
            (opens for next_open timing, closes for same_close timing).
        first_bar: First bar eligible as a window start, inclusive.
        last_bar: Last bar available in the window, inclusive.
        hold_bars: Window length in trading days, must be positive.

    Returns:
        Tuple (win_rate, sample_count). win_rate is NaN when no complete window
        fits between first_bar and last_bar.

    Raises:
        ValueError: hold_bars is not positive.
    """
    if hold_bars <= 0:
        raise ValueError("hold_bars must be positive")
    starts = np.arange(first_bar, last_bar - hold_bars + 1)
    if starts.size == 0:
        return float("nan"), 0
    # Strictly greater, so a flat window counts as a loss. This matches the win
    # definition applied to the strategy trades, prereg section 2.8.
    wins = prices[starts + hold_bars] > prices[starts]
    return float(wins.mean()), int(starts.size)


def benchmark_trades(
    prices: np.ndarray,
    trades: pd.DataFrame,
    first_bar: int,
    last_bar: int,
) -> pd.DataFrame:
    """Attach the matched-horizon benchmark to every closed trade.

    Args:
        prices: Fill-price series used by the trades.
        trades: Trade frame from engine.extract_trades.
        first_bar: First bar eligible as a benchmark window start.
        last_bar: Last bar of the study window, inclusive.

    Returns:
        A copy of trades with two extra columns: base_rate, the unconditional win
        rate for that trade's holding length, and base_n, how many overlapping
        windows that rate was measured over. Open trades get NaN.
    """
    out = trades.copy()
    if out.empty:
        out["base_rate"] = pd.Series(dtype="float64")
        out["base_n"] = pd.Series(dtype="int64")
        return out

    cache: dict[int, tuple[float, int]] = {}
    rates, counts = [], []
    for hold, closed in zip(out["hold_bars"], out["closed"]):
        if not closed:
            rates.append(float("nan"))
            counts.append(0)
            continue
        hold = int(hold)
        if hold not in cache:
            cache[hold] = horizon_win_rate(prices, first_bar, last_bar, hold)
        rate, count = cache[hold]
        rates.append(rate)
        counts.append(count)
    out["base_rate"] = rates
    out["base_n"] = counts
    return out


def poisson_binomial_test(wins: int, probabilities: np.ndarray) -> dict[str, float]:
    """One-sided test that the observed wins exceed the matched-horizon baseline.

    Each trade i has its own baseline success probability p_i, so the null is a
    Poisson-binomial rather than a binomial. The normal approximation with a
    continuity correction is used; with several dozen trades and p_i away from
    the boundaries it is adequate, and the exact distribution is not needed to
    separate "clearly above" from "indistinguishable".

    The test assumes trades are independent. Trades on one symbol never overlap,
    but trades on different symbols do overlap in time and share market-wide
    moves, so the p-value understates the true uncertainty. The per-symbol
    breakdown in the ruling is the guard against reading it too literally.

    Args:
        wins: Observed number of winning trades.
        probabilities: Baseline win probability for each trade, same ordering.

    Returns:
        Dict with n, wins, expected_wins, sd, z and p_value. All NaN when the
        variance is zero or no trades were supplied.
    """
    probabilities = np.asarray(probabilities, dtype="float64")
    probabilities = probabilities[~np.isnan(probabilities)]
    n = probabilities.size
    if n == 0:
        return {"n": 0, "wins": wins, "expected_wins": float("nan"),
                "sd": float("nan"), "z": float("nan"), "p_value": float("nan")}

    expected = float(probabilities.sum())
    variance = float((probabilities * (1.0 - probabilities)).sum())
    if variance <= 0.0:
        return {"n": n, "wins": wins, "expected_wins": expected,
                "sd": 0.0, "z": float("nan"), "p_value": float("nan")}

    sd = math.sqrt(variance)
    z = (wins - expected - 0.5) / sd
    return {"n": n, "wins": wins, "expected_wins": expected, "sd": sd,
            "z": z, "p_value": float(stats.norm.sf(z))}
