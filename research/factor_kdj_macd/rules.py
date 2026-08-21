"""Entry and exit signal rules, one arm per function.

Responsibility: turn an indicator frame into two boolean arrays, entry_signal
and exit_signal, both indexed by bar. Not responsible for: execution timing,
position state, prices. Those live in engine.py.

The rules are frozen in research/prereg/20260820_kdj_macd_daily_prereg.md
sections 2.3 and 2.4:

    entry at bar t   K_t > D_t and K_{t-1} <= D_{t-1}   (K crosses above D)
                     K_t < K_MAX
                     DIF_t > DEA_t
                     DIF_t > 0
    exit at bar s    K_s < D_s and K_{s-1} >= D_{s-1}   (K crosses below D)

Public functions:
    cross_up(fast, slow)              Rising crossover mask
    cross_down(fast, slow)            Falling crossover mask
    entry_exit(frame, ...)            Signal pair for one arm
    shift_forward(frame)              Look-ahead control, indicators moved one bar earlier
    ARMS                              Arm name -> rule keyword arguments
"""

from __future__ import annotations

__all__ = ["cross_up", "cross_down", "entry_exit", "shift_forward", "macd_filter",
           "ARMS", "MACD_MODES", "K_MAX", "RECENT_CROSS_BARS", "INDICATOR_COLUMNS"]

import numpy as np
import pandas as pd

# Upper bound on K at the entry crossover. Frozen at 30 by the user's own hand
# calculation, prereg section 2.3 condition 2.
K_MAX = 30.0

INDICATOR_COLUMNS = ("k", "d", "j", "dif", "dea", "hist")

# Look-back for the "a MACD golden cross happened recently" filter mode.
RECENT_CROSS_BARS = 5

# Arm name -> entry_exit keyword arguments. shift and fill are handled by the
# runner, not here, because they change execution rather than the rule.
ARMS: dict[str, dict[str, object]] = {
    "A-MAIN": {"macd_mode": "both", "k_max": K_MAX},
    "A-LOOKAHEAD": {"macd_mode": "both", "k_max": K_MAX},
    "A-CLOSE-FILL": {"macd_mode": "both", "k_max": K_MAX},
    "A-NOMACD": {"macd_mode": "none", "k_max": K_MAX},
    "A-NOK30": {"macd_mode": "both", "k_max": None},
}


def cross_up(fast: np.ndarray, slow: np.ndarray) -> np.ndarray:
    """Return the mask of bars where fast crosses from at-or-below to above slow.

    Args:
        fast: Faster series, for example K.
        slow: Slower series, for example D.

    Returns:
        Boolean array of the same length. Index 0 is always False because a
        crossover needs a previous bar. NaN on either bar yields False.
    """
    out = np.zeros(len(fast), dtype=bool)
    out[1:] = (fast[1:] > slow[1:]) & (fast[:-1] <= slow[:-1])
    return out


def cross_down(fast: np.ndarray, slow: np.ndarray) -> np.ndarray:
    """Return the mask of bars where fast crosses from at-or-above to below slow.

    Args:
        fast: Faster series, for example K.
        slow: Slower series, for example D.

    Returns:
        Boolean array of the same length, index 0 always False.
    """
    out = np.zeros(len(fast), dtype=bool)
    out[1:] = (fast[1:] < slow[1:]) & (fast[:-1] >= slow[:-1])
    return out


def _macd_none(dif: np.ndarray, dea: np.ndarray, hist: np.ndarray) -> np.ndarray:
    """No MACD condition: every bar passes."""
    return np.ones(len(dif), dtype=bool)


def _macd_dif_gt_dea(dif: np.ndarray, dea: np.ndarray, hist: np.ndarray) -> np.ndarray:
    """DIF above DEA, that is the MACD histogram is positive."""
    return dif > dea


def _macd_dif_gt_zero(dif: np.ndarray, dea: np.ndarray, hist: np.ndarray) -> np.ndarray:
    """DIF above the zero axis, the medium-term trend filter."""
    return dif > 0.0


def _macd_both(dif: np.ndarray, dea: np.ndarray, hist: np.ndarray) -> np.ndarray:
    """DIF above DEA and above zero. The pre-registered filter."""
    return (dif > dea) & (dif > 0.0)


def _macd_hist_rising(dif: np.ndarray, dea: np.ndarray, hist: np.ndarray) -> np.ndarray:
    """Histogram larger than on the previous bar, regardless of its sign."""
    out = np.zeros(len(hist), dtype=bool)
    out[1:] = hist[1:] > hist[:-1]
    return out


def _macd_dif_rising(dif: np.ndarray, dea: np.ndarray, hist: np.ndarray) -> np.ndarray:
    """DIF higher than on the previous bar, a short-term momentum-direction filter."""
    out = np.zeros(len(dif), dtype=bool)
    out[1:] = dif[1:] > dif[:-1]
    return out


def _macd_recent_cross(dif: np.ndarray, dea: np.ndarray, hist: np.ndarray) -> np.ndarray:
    """A DIF-over-DEA golden cross occurred within the last RECENT_CROSS_BARS bars."""
    crossed = cross_up(dif, dea)
    out = np.zeros(len(dif), dtype=bool)
    for lag in range(RECENT_CROSS_BARS + 1):
        if lag == 0:
            out |= crossed
        else:
            out[lag:] |= crossed[:-lag]
    return out


# Filter mode -> predicate over (dif, dea, hist). A dispatch table rather than an
# if-chain so a new mode is one entry, not an edit inside entry_exit.
MACD_MODES = {
    "none": _macd_none,
    "dif_gt_dea": _macd_dif_gt_dea,
    "dif_gt_zero": _macd_dif_gt_zero,
    "both": _macd_both,
    "hist_rising": _macd_hist_rising,
    "recent_cross": _macd_recent_cross,
    "dif_rising": _macd_dif_rising,
}


def macd_filter(frame: pd.DataFrame, macd_mode: str) -> np.ndarray:
    """Evaluate one MACD filter mode over an indicator frame.

    Args:
        frame: Indicator frame carrying dif, dea and hist columns.
        macd_mode: Key of MACD_MODES.

    Returns:
        Boolean array, True where the filter allows an entry.

    Raises:
        KeyError: Unknown mode.
    """
    if macd_mode not in MACD_MODES:
        raise KeyError(f"unknown macd_mode {macd_mode!r}, expected one of {sorted(MACD_MODES)}")
    return MACD_MODES[macd_mode](
        frame["dif"].to_numpy(), frame["dea"].to_numpy(), frame["hist"].to_numpy()
    )


def entry_exit(
    frame: pd.DataFrame,
    macd_mode: str = "both",
    k_max: float | None = K_MAX,
    k_min: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Build the entry and exit signal masks for one arm.

    Args:
        frame: Indicator frame carrying k, d, dif, dea and hist columns.
        macd_mode: Key of MACD_MODES. "both" is the pre-registered filter,
            DIF > DEA and DIF > 0; "none" drops the MACD condition entirely.
        k_max: Upper bound on K at the crossover bar (exclusive), or None to
            drop that bound.
        k_min: Lower bound on K at the crossover bar (inclusive), or None to
            drop that bound. The default None preserves the behavior of every
            arm run before the band search was added.

    Returns:
        Tuple (entry_signal, exit_signal) as boolean arrays indexed by bar. Both
        are signal masks evaluated on the close of that bar; they say nothing
        about when the resulting order is filled.
    """
    k = frame["k"].to_numpy()
    d = frame["d"].to_numpy()

    entry = cross_up(k, d)
    if k_min is not None:
        entry &= k >= k_min
    if k_max is not None:
        entry &= k < k_max
    entry &= macd_filter(frame, macd_mode)

    return entry, cross_down(k, d)


def shift_forward(frame: pd.DataFrame) -> pd.DataFrame:
    """Move every indicator column one bar earlier, building the look-ahead arm.

    After the shift, bar t carries the indicator values of bar t+1, so any rule
    evaluated on the result peeks exactly one bar into the future. This is the
    deliberate look-ahead control of backtest-discipline section 1.2: it must
    beat the clean arm, otherwise the check has no discriminating power.

    Args:
        frame: Indicator frame from indicators.add_indicators.

    Returns:
        A copy with the indicator columns shifted by -1. The last bar's indicator
        values become NaN, which suppresses signals there.
    """
    out = frame.copy()
    for column in INDICATOR_COLUMNS:
        out[column] = out[column].shift(-1)
    return out
