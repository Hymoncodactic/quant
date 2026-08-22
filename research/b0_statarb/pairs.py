"""Pair selection: distance (Gatev et al. 2006) and Engle-Granger cointegration.

Responsibility: given a formation-window price panel, rank candidate pairs and
return the chosen ones together with the parameters the trading window will
use. Every number returned here is estimated on the formation window ONLY; the
trading window never re-estimates, which is what keeps the split honest.

Not responsible for: signals or position sizing (engine.py), window slicing
(run_study.py), data loading (data.py).

Public functions:
    normalized_paths(prices)        Cumulative-return paths used by the distance rule.
    select_distance(prices, ...)    Top pairs by squared Euclidean distance.
    select_cointegration(prices,..) Pairs whose Engle-Granger residual is stationary.
    spread_parameters(a, b, method) Hedge ratio and spread moments from formation.

Constants:
    ADF_MAX_P        float  Engle-Granger residual ADF p-value ceiling, 0.05.
    MIN_HALF_LIFE    float  Ornstein-Uhlenbeck half life floor in days, 1.0.
    MAX_HALF_LIFE    float  Ceiling in days, 126 (half a trading year). A spread
                            that reverts more slowly than the trading window is
                            not tradeable inside it.

Change log:
    2026-08-23  Created for the B0 statistical-arbitrage study.
"""

from __future__ import annotations

__all__ = ["normalized_paths", "select_distance", "select_cointegration",
           "spread_parameters", "half_life", "ADF_MAX_P",
           "MIN_HALF_LIFE", "MAX_HALF_LIFE"]

from itertools import combinations

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller

ADF_MAX_P = 0.05
MIN_HALF_LIFE = 1.0
MAX_HALF_LIFE = 126.0


def normalized_paths(prices: pd.DataFrame) -> pd.DataFrame:
    """Cumulative total-return paths starting at 1, the distance rule's input.

    Gatev et al. (2006) normalize each name to a cumulative return index over
    the formation window so that the distance compares SHAPES, not price levels.
    """
    returns = prices.pct_change().fillna(0.0)
    return (1.0 + returns).cumprod()


def select_distance(prices: pd.DataFrame, groups: dict[str, str],
                    top_n: int, within_group: bool = True) -> list[tuple]:
    """Pairs with the smallest squared Euclidean distance between paths."""
    paths = normalized_paths(prices)
    scored = []
    for a, b in combinations(sorted(prices.columns), 2):
        if within_group and groups.get(a) != groups.get(b):
            continue
        diff = (paths[a] - paths[b]).to_numpy()
        scored.append((float(np.nansum(diff ** 2)), a, b))
    scored.sort()
    return [(a, b, {"distance": d}) for d, a, b in scored[:top_n]]


def half_life(spread: pd.Series) -> float:
    """Ornstein-Uhlenbeck half life of mean reversion, in bars.

    Regress the spread's change on its lagged level; a negative slope means
    reversion and the half life is -ln(2)/ln(1+slope). Returns inf when the
    slope is non-negative, i.e. no reversion.
    """
    lagged = spread.shift(1).dropna()
    delta = spread.diff().dropna()
    lagged, delta = lagged.align(delta, join="inner")
    if len(lagged) < 20 or lagged.std(ddof=1) == 0:
        return float("inf")
    slope = np.polyfit(lagged.to_numpy(), delta.to_numpy(), 1)[0]
    if slope >= 0:
        return float("inf")
    return float(-np.log(2.0) / np.log1p(slope))


def spread_parameters(a: pd.Series, b: pd.Series, method: str) -> dict:
    """Hedge ratio and spread moments, estimated on the formation window only.

    method "coint" regresses a on b (with intercept) and takes the residual as
    the spread. method "dist" uses the difference of the normalized paths, which
    is the Gatev formulation and carries no hedge ratio.
    """
    if method == "dist":
        paths = normalized_paths(pd.DataFrame({"a": a, "b": b}))
        spread = paths["a"] - paths["b"]
        beta, alpha = 1.0, 0.0
    else:
        x = b.to_numpy()
        y = a.to_numpy()
        beta, alpha = np.polyfit(x, y, 1)
        spread = pd.Series(y - (beta * x + alpha), index=a.index)
    return {"beta": float(beta), "alpha": float(alpha),
            "mu": float(spread.mean()), "sigma": float(spread.std(ddof=1)),
            "half_life": half_life(spread)}


def select_cointegration(prices: pd.DataFrame, groups: dict[str, str],
                         top_n: int, within_group: bool = True) -> list[tuple]:
    """Pairs whose Engle-Granger residual rejects a unit root.

    Two-step Engle-Granger: regress a on b, run ADF on the residual, keep the
    pair when p < ADF_MAX_P. Ranked by p ascending. A half life outside
    [MIN_HALF_LIFE, MAX_HALF_LIFE] is dropped: a spread that reverts slower than
    the trading window cannot be harvested inside it, and one that reverts
    within a bar is noise, not structure.
    """
    scored = []
    for a, b in combinations(sorted(prices.columns), 2):
        if within_group and groups.get(a) != groups.get(b):
            continue
        sa, sb = prices[a].dropna(), prices[b].dropna()
        sa, sb = sa.align(sb, join="inner")
        if len(sa) < 60 or sa.std(ddof=1) == 0 or sb.std(ddof=1) == 0:
            continue
        params = spread_parameters(sa, sb, "coint")
        if not (MIN_HALF_LIFE <= params["half_life"] <= MAX_HALF_LIFE):
            continue
        resid = sa.to_numpy() - (params["beta"] * sb.to_numpy() + params["alpha"])
        try:
            pvalue = float(adfuller(resid, maxlag=1, regression="c",
                                    autolag=None)[1])
        except Exception:                                  # noqa: BLE001
            continue
        if pvalue < ADF_MAX_P:
            scored.append((pvalue, a, b, params))
    scored.sort(key=lambda row: row[0])
    return [(a, b, {**params, "adf_p": p}) for p, a, b, params in scored[:top_n]]
