"""Volatility estimators: range-based from daily OHLC, realized from hourly bars.

The study's bridge: decades of daily OHLC stand in for high-frequency data when
classifying volatility regimes, justified empirically by validate_rv_proxy.py
on the 730-day window where hourly bars exist. Formulas follow the published
estimators; constants are stated inline.

Public functions:
    yang_zhang(bars, window)     Yang-Zhang (2000), annualized, uses overnight term
    parkinson(bars, window)      Parkinson (1980) high-low estimator, annualized
    garman_klass(bars, window)   Garman-Klass (1980), annualized
    realized_vol_hourly(intraday, window)  Realized vol from hourly returns incl.
                                 overnight gap, annualized, indexed by date
    ANNUALIZATION_DAYS
"""

from __future__ import annotations

__all__ = ["yang_zhang", "parkinson", "garman_klass", "realized_vol_hourly",
           "ANNUALIZATION_DAYS"]

import numpy as np
import pandas as pd

ANNUALIZATION_DAYS = 252


def _log_terms(bars: pd.DataFrame) -> pd.DataFrame:
    """Per-bar log terms shared by the range-based estimators."""
    out = pd.DataFrame(index=bars.index)
    out["o"] = np.log(bars["open"] / bars["close"].shift(1))   # overnight
    out["u"] = np.log(bars["high"] / bars["open"])
    out["d"] = np.log(bars["low"] / bars["open"])
    out["c"] = np.log(bars["close"] / bars["open"])             # open-to-close
    return out


def yang_zhang(bars: pd.DataFrame, window: int = 20) -> pd.Series:
    """Yang-Zhang (2000) volatility, annualized.

    sigma^2 = sigma_o^2 + k * sigma_c^2 + (1 - k) * sigma_rs^2 with
    k = 0.34 / (1.34 + (window + 1) / (window - 1)); sigma_o^2 and sigma_c^2 are
    sample variances of the overnight and open-to-close log returns and
    sigma_rs^2 is the rolling mean of the Rogers-Satchell term. Handles both
    overnight gaps and drift, which matters for gappy tech names.

    Args:
        bars: Daily frame with open/high/low/close, ascending by date.
        window: Rolling window in trading days.

    Returns:
        Annualized volatility series aligned to bars.index; the value at row t
        uses rows t-window+1 .. t only.
    """
    t = _log_terms(bars)
    rs = t["u"] * (t["u"] - t["c"]) + t["d"] * (t["d"] - t["c"])
    k = 0.34 / (1.34 + (window + 1) / (window - 1))
    var_o = t["o"].rolling(window).var()
    var_c = t["c"].rolling(window).var()
    mean_rs = rs.rolling(window).mean()
    return np.sqrt((var_o + k * var_c + (1 - k) * mean_rs).clip(lower=0.0)
                   * ANNUALIZATION_DAYS)


def parkinson(bars: pd.DataFrame, window: int = 20) -> pd.Series:
    """Parkinson (1980) high-low estimator, annualized: mean of (ln(H/L))^2 / (4 ln 2)."""
    hl = np.log(bars["high"] / bars["low"]) ** 2
    return np.sqrt(hl.rolling(window).mean() / (4.0 * np.log(2.0)) * ANNUALIZATION_DAYS)


def garman_klass(bars: pd.DataFrame, window: int = 20) -> pd.Series:
    """Garman-Klass (1980), annualized: 0.5 (ln H/L)^2 - (2 ln 2 - 1)(ln C/O)^2."""
    t = _log_terms(bars)
    gk = 0.5 * (t["u"] - t["d"]) ** 2 - (2.0 * np.log(2.0) - 1.0) * t["c"] ** 2
    return np.sqrt(gk.rolling(window).mean().clip(lower=0.0) * ANNUALIZATION_DAYS)


def realized_vol_hourly(intraday: pd.DataFrame, window: int = 20) -> pd.Series:
    """Realized volatility from hourly bars, overnight gap included, annualized.

    Daily realized variance = sum of squared hourly log close-to-close returns
    within the day plus the squared overnight gap (first open over previous last
    close). The window smooths daily RV by its rolling mean before the square
    root, matching how the range-based estimators average variance terms.

    Args:
        intraday: Frame from data.load_intraday("...", "1h").
        window: Rolling window in trading days.

    Returns:
        Annualized volatility indexed by trading date (tz-naive). The value at
        date t uses bars up to and including t.
    """
    frame = intraday.copy()
    frame["log_close"] = np.log(frame["close"])
    frame["ret"] = frame["log_close"].diff()
    daily_rv = frame.groupby("date")["ret"].apply(lambda r: float((r.dropna() ** 2).sum()))
    return np.sqrt(daily_rv.rolling(window).mean() * ANNUALIZATION_DAYS)
