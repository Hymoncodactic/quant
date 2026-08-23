"""Capacity of B0 at a given book size: how much can trade, and what it costs.

Responsibility: answer "does GBP 10,000 fit, and where does it stop fitting".
Two distinct questions are kept separate because they have different answers:

    Size  Can the order be absorbed without moving the price? Measured as the
          order over the name's median daily dollar volume (participation).
    Cost  What does crossing the spread cost at that size? Measured from the
          per-name Corwin-Schultz half spread. Participation does not enter
          this: a GBP 500 order pays the same spread as a GBP 5 order.

Conflating the two is the usual capacity mistake. At GBP 10,000 the order is
about GBP 500 a leg, which is a rounding error against any name that clears the
liquidity floor, so size is not the binding constraint; spread is.

Public functions:
    order_size(capital_gbp, pairs, legs)   One leg's order in GBP and USD.
    scaling_table(liquidity, ...)          Names surviving the gate by book size.
    cost_by_tier(liquidity)                Round-trip cost by market-cap tier.

Change log:
    2026-08-23  Created for B0 round 2 capacity verification.
"""

from __future__ import annotations

__all__ = ["order_size", "scaling_table", "cost_by_tier"]

import numpy as np
import pandas as pd

from research.b0_statarb.liquidity import (GBPUSD_ASSUMED, MAX_PARTICIPATION,
                                           MIN_DOLLAR_VOLUME)

FX_FEE_BPS_ONE_WAY = 15.0


def order_size(capital_gbp: float, pairs: int = 20, legs: int = 1
               ) -> tuple[float, float]:
    """One leg's order value. legs=2 splits a pair across two names (MN)."""
    per_leg_gbp = capital_gbp / pairs / legs
    return per_leg_gbp, per_leg_gbp * GBPUSD_ASSUMED


def scaling_table(liquidity: pd.DataFrame,
                  capitals: tuple[float, ...] = (1e4, 5e4, 1e5, 5e5, 1e6, 5e6),
                  pairs: int = 20,
                  max_participation: float = MAX_PARTICIPATION,
                  min_dollar: float = MIN_DOLLAR_VOLUME) -> pd.DataFrame:
    """How many names still clear the gates as the book grows.

    The dollar-volume floor is a property of the name, not of the book, so it
    is held fixed; only participation scales with capital. The point of the
    table is the headroom: the book size at which the surviving universe stops
    being large enough to form pairs.
    """
    rows = []
    base = liquidity[liquidity["median_dollar_volume"] >= min_dollar]
    for capital in capitals:
        _, order_usd = order_size(capital, pairs)
        part = order_usd / base["median_dollar_volume"]
        keep = base[part < max_participation]
        rows.append({
            "capital_gbp": capital,
            "order_per_leg_gbp": capital / pairs,
            "order_per_leg_usd": order_usd,
            "names_passing": int(len(keep)),
            "median_participation": float(part.median()),
            "p95_participation": float(part.quantile(0.95)),
        })
    return pd.DataFrame(rows)


def cost_by_tier(liquidity: pd.DataFrame) -> pd.DataFrame:
    """Round-trip cost by market-cap tier: spread plus the Trading 212 FX fee."""
    frame = liquidity.dropna(subset=["half_spread_bps"]).copy()
    frame["round_trip_bps"] = 2.0 * (frame["half_spread_bps"]
                                     + FX_FEE_BPS_ONE_WAY)
    grouped = frame.groupby("cap_tier")["half_spread_bps"]
    out = pd.DataFrame({
        "names": grouped.size(),
        "half_spread_median_bps": grouped.median(),
        "half_spread_p90_bps": grouped.quantile(0.90),
        "round_trip_median_bps": frame.groupby("cap_tier")["round_trip_bps"].median(),
        "median_dollar_volume": frame.groupby("cap_tier")["median_dollar_volume"].median(),
    })
    return out
