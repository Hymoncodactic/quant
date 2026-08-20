"""Validate range-based daily volatility as a proxy for hourly realized vol.

The study classifies volatility regimes over decades of daily data using the
Yang-Zhang estimator, on the claim that it tracks what actual high-frequency
data would measure. This script tests that claim on the only window where both
exist (the last 730 days of hourly bars): levels correlation, rank correlation,
and — the part that matters for regimes — agreement of the top-quantile
stress/calm classification produced by each series.

Usage:
    python -m regime_lab.validate_rv_proxy

Public functions:
    validate(symbol, group, window)   One symbol's proxy-quality row
    main()                            QQQ + six equities, writes results/
"""

from __future__ import annotations

__all__ = ["validate", "main", "SYMBOLS_CHECKED"]

import numpy as np
import pandas as pd
from scipy import stats

from . import data as data_module
from . import vol as vol_module

RESULTS_DIR = __import__("pathlib").Path(__file__).resolve().parent / "results"

SYMBOLS_CHECKED = (("QQQ", "us_etf"), ("SPY", "us_etf"), ("NVDA", "us_equity"),
                   ("AAPL", "us_equity"), ("TSLA", "us_equity"), ("AMD", "us_equity"),
                   ("META", "us_equity"))


def validate(symbol: str, group: str, window: int = 20) -> dict:
    """Compare Yang-Zhang (daily OHLC) against hourly realized vol for one symbol.

    Regime agreement uses each series' own in-overlap 70th percentile as the
    stress threshold, mirroring how the strategy layer will threshold within a
    trailing window.

    Args:
        symbol: Ticker.
        group: Data group.
        window: Rolling window in trading days, same for both series.

    Returns:
        Dict with sample size, Pearson and Spearman correlations of the two
        vol series, median relative gap, and stress-classification agreement.
    """
    daily = data_module.load_daily(symbol, group)
    intraday = data_module.load_intraday(symbol, "1h", group)

    yz = vol_module.yang_zhang(daily, window)
    yz.index = daily["date"]
    rv = vol_module.realized_vol_hourly(intraday, window)

    joined = pd.concat([yz.rename("yz"), rv.rename("rv")], axis=1, join="inner").dropna()
    stress_yz = joined["yz"] >= joined["yz"].quantile(0.70)
    stress_rv = joined["rv"] >= joined["rv"].quantile(0.70)
    return {
        "symbol": symbol, "n_days": len(joined),
        "pearson": float(joined["yz"].corr(joined["rv"])),
        "spearman": float(stats.spearmanr(joined["yz"], joined["rv"]).statistic),
        "median_rel_gap": float(((joined["yz"] - joined["rv"]).abs() / joined["rv"]).median()),
        "stress_agreement": float((stress_yz == stress_rv).mean()),
        "mean_yz": float(joined["yz"].mean()), "mean_rv": float(joined["rv"].mean()),
    }


def main() -> int:
    """Validate the proxy on QQQ plus six tech names and persist the table."""
    rows = [validate(symbol, group) for symbol, group in SYMBOLS_CHECKED]
    table = pd.DataFrame(rows)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    table.to_csv(RESULTS_DIR / "rv_proxy_validation.csv", index=False)
    print(table.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
