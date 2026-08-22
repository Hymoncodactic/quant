"""Trade one pair over one trading window under the three B0 variants.

Responsibility: turn a formation-estimated spread into positions and a daily
return series, for each of the three variants frozen in
research/prereg/20260823_b0_statarb_prereg.md section 3.4. Not responsible for
pair selection (pairs.py), window slicing (run_study.py) or reporting.

Timing convention (the no-lookahead core): the position for day t is decided
from information up to and including day t's close, and earns the return from
t's close to t+1's close. That is the same_close convention already validated
for A0, so the two strategies are executed on the same assumption. Costs are
charged on the change in absolute exposure, on the bar the change happens.

Public functions:
    zscore(spread, mu, sigma)             Standardize on formation moments.
    positions(z, variant, ...)            Per-leg weights over the window.
    run_pair(prices, params, variant,..)  Daily return series of one pair.

Constants:
    ENTRY_Z / EXIT_Z / STOP_Z  float  2.0 / 0.0 / 4.0, frozen in the prereg.
    COST_BPS_ACTUAL            float  32.0 one round trip: T212 0.15% FX both
                                      ways (30bp) plus 1bp half spread each leg.
    COST_BPS_WORST             float  42.0, adding 5bp slippage per leg.

Change log:
    2026-08-23  Created for the B0 statistical-arbitrage study.
"""

from __future__ import annotations

__all__ = ["zscore", "positions", "run_pair", "ENTRY_Z", "EXIT_Z", "STOP_Z",
           "COST_BPS_ACTUAL", "COST_BPS_WORST"]

import numpy as np
import pandas as pd

ENTRY_Z = 2.0
EXIT_Z = 0.0
STOP_Z = 4.0
COST_BPS_ACTUAL = 32.0
COST_BPS_WORST = 42.0


def zscore(spread: pd.Series, mu: float, sigma: float) -> pd.Series:
    """Standardize a trading-window spread on FORMATION moments."""
    if sigma <= 0:
        return pd.Series(0.0, index=spread.index)
    return (spread - mu) / sigma


def positions(z: pd.Series, variant: str, entry: float = ENTRY_Z,
              stop: float = STOP_Z) -> pd.DataFrame:
    """Per-leg weights for every day of the trading window.

    State machine, identical across variants so the only difference between
    them is how a state maps to weights:
        flat  -> short_spread when z >= entry, long_spread when z <= -entry
        open  -> flat when z crosses EXIT_Z, or when |z| >= stop (stop loss)
    "long_spread" means the spread is cheap (a underpriced relative to b).

    Weight conventions:
        MN  long leg +0.5, short leg -0.5 (gross 1.0, net 0.0)
        L1  the cheap leg 1.0, the other 0.0; the last state persists while
            flat, so the book is always in one of the two names
        L2  the cheap leg 1.0 only while a position is open, else all cash
    """
    state = 0                    # +1 long spread (a cheap), -1 short spread
    last_leg = "a"
    wa, wb = [], []
    for value in z.to_numpy():
        if np.isnan(value):
            value = 0.0
        if state == 0:
            if value >= entry:
                state, last_leg = -1, "b"
            elif value <= -entry:
                state, last_leg = 1, "a"
        else:
            crossed = (state == 1 and value >= EXIT_Z) or \
                      (state == -1 and value <= EXIT_Z)
            if crossed or abs(value) >= stop:
                state = 0
        if variant == "MN":
            wa.append(0.5 * state)
            wb.append(-0.5 * state)
        elif variant == "L1":
            cheap = "a" if state == 1 else ("b" if state == -1 else last_leg)
            wa.append(1.0 if cheap == "a" else 0.0)
            wb.append(1.0 if cheap == "b" else 0.0)
        elif variant == "L2":
            wa.append(1.0 if state == 1 else 0.0)
            wb.append(1.0 if state == -1 else 0.0)
        else:
            raise ValueError(f"unknown variant {variant!r}")
    return pd.DataFrame({"wa": wa, "wb": wb}, index=z.index)


def run_pair(prices: pd.DataFrame, a: str, b: str, params: dict,
             variant: str, method: str, cost_bps: float,
             entry: float = ENTRY_Z) -> pd.Series:
    """Daily return of one pair over the trading window, costs included.

    prices must already be sliced to the trading window. The spread is rebuilt
    with the FORMATION hedge ratio and standardized with FORMATION moments;
    nothing in this function re-estimates anything.
    """
    sa, sb = prices[a], prices[b]
    if method == "dist":
        base = prices[[a, b]].iloc[0]
        pa, pb = sa / base[a], sb / base[b]
        spread = pa - pb
    else:
        spread = sa - (params["beta"] * sb + params["alpha"])
    z = zscore(spread, params["mu"], params["sigma"])

    weights = positions(z, variant, entry=entry)
    returns = prices[[a, b]].pct_change().fillna(0.0)
    # Position decided on day t earns day t+1's return.
    held = weights.shift(1).fillna(0.0)
    gross = held["wa"] * returns[a] + held["wb"] * returns[b]
    turnover = (weights - weights.shift(1).fillna(0.0)).abs().sum(axis=1)
    cost = turnover.shift(1).fillna(0.0) * (cost_bps / 2.0) / 1e4
    return (gross - cost).rename(f"{a}/{b}")
