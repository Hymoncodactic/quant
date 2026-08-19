"""Probe: test whether a liquid, negatively correlated equity hedge exists.

Same question as the crypto probe, asked of US equities. The premise under test
is that liquid instruments exist which are negatively correlated with the
mega-caps and can therefore hedge them.

Two things this probe checks that a plain correlation matrix does not:
    1. Behaviour in drawdowns. A hedge only pays if it rises when the market
       falls, so the mean return in the market's worst decile matters more than
       the whole-window coefficient.
    2. Regime stability. The equity-bond correlation was reliably negative for
       two decades and turned positive during the 2022 inflation shock, so a
       single whole-window number would hide the very thing that matters.

Source: Yahoo Finance via yfinance. Measured limits on this machine, 2026-08-19:
    1m granularity, 8 days per request and roughly 30 days of history;
    5m, 60 days; 1h, 730 days; 1d, back to 1980. Daily is therefore the only
    granularity deep enough for a regime study.

Caveat: instrument availability to a UK retail investor is a separate question
this probe does not answer. Several candidates here are US-domiciled ETFs.

Public functions:
    main()   Fetch, compute and print the hedge analysis
"""

from __future__ import annotations

__all__ = ["main"]

import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

START = "2015-01-01"
CRASH_QUANTILE = 0.10
MIN_OVERLAP_DAYS = 120

BENCHMARK = "SPY"

UNIVERSE = {
    # Mega-caps the owner named, plus their closest peers
    "AAPL": "Apple",
    "TSLA": "Tesla",
    "MSFT": "Microsoft",
    "NVDA": "Nvidia",
    "AMZN": "Amazon",
    "GOOGL": "Alphabet",
    "META": "Meta",
    # Broad market, the benchmark for the hedge question
    "SPY": "S&P 500",
    "QQQ": "Nasdaq 100",
    # Defensive sectors, the usual low-beta suggestion
    "XLU": "Utilities",
    "XLP": "Consumer staples",
    "XLV": "Healthcare",
    # Gold, the classic cross-asset diversifier
    "GLD": "Gold bullion",
    "GDX": "Gold miners",
    # Long duration Treasuries, the classic hedge whose regime changed
    "TLT": "20y+ Treasuries",
    "IEF": "7-10y Treasuries",
    # Volatility, structurally long vol but decays
    "VXX": "VIX short-term futures",
    # Inverse index products, mechanically negative but path dependent
    "SH": "Inverse S&P 500",
    "PSQ": "Inverse Nasdaq 100",
    # Dollar
    "UUP": "US dollar index",
}


def _download() -> pd.DataFrame:
    """Download adjusted daily closes for the whole universe."""
    import yfinance as yf
    raw = yf.download(list(UNIVERSE), start=START, interval="1d",
                      progress=False, auto_adjust=True, threads=False)
    closes = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    return closes.dropna(how="all")


def main() -> None:
    """Fetch the panel, then print the three views."""
    print("=" * 92)
    print(f"Downloading daily adjusted closes from {START}")
    print("=" * 92)
    prices = _download()
    returns = np.log(prices / prices.shift(1)).dropna(how="all")
    bench = returns[BENCHMARK]
    print(f"Panel: {returns.shape[0]} days by {returns.shape[1]} instruments, "
          f"{returns.index[0].date()} to {returns.index[-1].date()}\n")

    # ---- View 1: whole-window correlation against the benchmark ----
    print("=" * 92)
    print(f"Correlation of daily returns against {BENCHMARK}, whole window")
    print("=" * 92)
    print(f"{'symbol':<8}{'description':<26}{'corr vs SPY':>13}{'corr vs AAPL':>14}"
          f"{'ann. return':>13}{'ann. vol':>11}")
    print("-" * 92)
    stats = []
    aapl = returns["AAPL"]
    for symbol in returns.columns:
        pair = pd.concat([bench, returns[symbol]], axis=1).dropna()
        if len(pair) < MIN_OVERLAP_DAYS:
            continue
        corr = pair.iloc[:, 0].corr(pair.iloc[:, 1])
        pair_a = pd.concat([aapl, returns[symbol]], axis=1).dropna()
        corr_a = pair_a.iloc[:, 0].corr(pair_a.iloc[:, 1])
        series = returns[symbol].dropna()
        stats.append((corr, symbol, corr_a,
                      series.mean() * 252, series.std() * np.sqrt(252)))
    stats.sort()
    for corr, symbol, corr_a, ann_ret, ann_vol in stats:
        print(f"{symbol:<8}{UNIVERSE.get(symbol, ''):<26}{corr:>13.3f}{corr_a:>14.3f}"
              f"{ann_ret:>12.1%}{ann_vol:>11.1%}")

    # ---- View 2: the drawdown subsample ----
    print("\n" + "=" * 92)
    print(f"Crisis subsample: {BENCHMARK}'s worst decile of daily returns")
    print("A hedge must show a positive mean return here. A merely low correlation is not enough.")
    print("=" * 92)
    threshold = bench.quantile(CRASH_QUANTILE)
    crash = returns[bench <= threshold]
    print(f"Threshold: {BENCHMARK} daily return <= {threshold:.2%}, {len(crash)} days\n")
    print(f"{'symbol':<8}{'description':<26}{'full corr':>11}{'crisis corr':>13}"
          f"{'crisis mean':>13}{'hedge?':>9}")
    print("-" * 92)
    for corr, symbol, _ca, _ar, _av in stats:
        # The benchmark against itself yields a duplicate-column frame, and it
        # answers nothing anyway.
        if symbol == BENCHMARK:
            continue
        sub = crash[[BENCHMARK, symbol]].dropna()
        if len(sub) < 20:
            continue
        mean_ret = sub[symbol].mean()
        verdict = "yes" if mean_ret > 0.001 else ("flat" if mean_ret > -0.001 else "no")
        print(f"{symbol:<8}{UNIVERSE.get(symbol, ''):<26}{corr:>11.3f}"
              f"{sub[BENCHMARK].corr(sub[symbol]):>13.3f}{mean_ret:>13.2%}{verdict:>9}")

    # ---- View 3: regime stability, year by year ----
    print("\n" + "=" * 92)
    print(f"Correlation against {BENCHMARK} by calendar year")
    print("Watch TLT and IEF across 2022: the classic bond hedge inverted.")
    print("=" * 92)
    yearly = {}
    for _c, symbol, _ca, _ar, _av in stats:
        if symbol == BENCHMARK:
            continue
        row = {}
        for year, group in returns.groupby(returns.index.year):
            sub = group[[BENCHMARK, symbol]].dropna()
            row[year] = (sub[BENCHMARK].corr(sub[symbol])
                         if len(sub) >= 120 else np.nan)
        yearly[symbol] = row
    print(pd.DataFrame(yearly).T.round(2).to_string())


if __name__ == "__main__":
    main()
