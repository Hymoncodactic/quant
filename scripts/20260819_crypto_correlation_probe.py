"""One-off probe measuring realized correlations between liquid Binance pairs.

Responsibility: fetch daily bars for a fixed candidate list from the read-only
Binance market-data mirror, compute the Pearson correlation of daily log returns
against BTCUSDT, and print three views of the result: the whole window, the
subsample of BTC's worst decile of days, and one coefficient per calendar year.
The premise under test is that a liquid crypto asset exists which is negatively
correlated with BTC. A coefficient alone cannot settle that, because a low
whole-window number can coexist with heavy losses on exactly the days a hedge is
supposed to pay, so the drawdown subsample and the year-by-year table are
reported beside it. Stablecoins are included as a control group whose near-zero
correlation is what gives the measurement discriminating power.

Out of scope: persisting anything to the data lake, which belongs to
scripts/20260819_ingest_crypto_phase1.py; reusable archive access, which belongs
to crypto_trading/ingest/binance_archive.py; the written conclusions, which
belong to research/notes/20260819_negative_correlation_findings.md. This is a
one-off probe and is not part of the production pipeline.

Public functions:
    main()   Fetch the panel, compute the three views, and print them.

Constants:
    BASE              str   Read-only market-data mirror host,
                            https://data-api.binance.vision. It is reachable
                            from this host without credentials, unlike
                            api.binance.com, which answers HTTP 451 here.
    INTERVAL          str   Bar period requested, "1d".
    LIMIT             int   Bars per request, 1000. Source: the endpoint's own
                            maximum, which is why the window starts in 2024 and
                            excludes the 2021-2022 bear market. Extending it
                            means pulling monthly archives from
                            data.binance.vision, which reach back to 2017-08.
    SLEEP_SEC         float Pause between requests, 0.25 seconds. Self-imposed
                            throttle, far under the documented ceiling of 6000
                            weight per minute.
    MIN_OVERLAP_DAYS  int   Minimum overlapping days for a reported
                            coefficient, 60. Below that a correlation is too
                            noisy to report.
    CRASH_QUANTILE    float Quantile defining the crisis subsample, 0.10, that
                            is BTC's worst decile of daily returns.
    CANDIDATES        list  Symbols probed: liquid majors, privacy coins,
                            gold-backed tokens, and stablecoins as the control
                            group.

Inputs:
    GET https://data-api.binance.vision/api/v3/klines
Outputs:
    stdout only. No file is written.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["main"]

import json
import time
import urllib.request
from typing import Optional

import numpy as np
import pandas as pd

BASE = "https://data-api.binance.vision"
INTERVAL = "1d"
LIMIT = 1000                      # endpoint maximum, roughly 2.7 years of daily bars
SLEEP_SEC = 0.25                  # self-imposed throttle, far under 6000 weight/minute
MIN_OVERLAP_DAYS = 60             # below this a correlation is too noisy to report
CRASH_QUANTILE = 0.10             # "crisis" means BTC's worst decile of daily returns

CANDIDATES = [
    # Liquid majors
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "TRXUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "BCHUSDT",
    "DOTUSDT", "TONUSDT", "SUIUSDT",
    # Privacy coins, sometimes framed as a safe haven
    "ZECUSDT", "XMRUSDT", "DASHUSDT",
    # Gold-backed tokens, the only genuine cross-asset diversifier on the venue
    "PAXGUSDT", "XAUTUSDT",
    # Stablecoins: correlation should be near zero. They are the control group
    # that gives the measurement its discriminating power.
    "USDCUSDT", "FDUSDUSDT", "TUSDUSDT",
]


def _fetch_klines(symbol: str) -> Optional[pd.DataFrame]:
    """Fetch daily bars for one symbol.

    Returns:
        Frame indexed by UTC bar-open time with close and quote_volume columns,
        or None when the symbol is absent or the request failed. A failure is
        reported and skipped rather than raised: one delisted symbol should not
        abort the whole panel.
    """
    url = f"{BASE}/api/v3/klines?symbol={symbol}&interval={INTERVAL}&limit={LIMIT}"
    req = urllib.request.Request(url, headers={"User-Agent": "quant-research/1.0"})
    try:
        raw = urllib.request.urlopen(req, timeout=30).read()
    except Exception as exc:
        print(f"  {symbol:<12} fetch failed: {type(exc).__name__}")
        return None
    rows = json.loads(raw)
    if not rows:
        print(f"  {symbol:<12} no data")
        return None
    frame = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore"])
    # open_time is the bar's opening instant in UTC milliseconds. Verified against
    # the spacing of consecutive bars, which equals the bar period exactly.
    frame["dt"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame["close"] = frame["close"].astype(float)
    frame["quote_volume"] = frame["quote_volume"].astype(float)
    out = frame.set_index("dt")[["close", "quote_volume"]]
    print(f"  {symbol:<12} {len(out):>5} bars  {out.index[0].date()} to {out.index[-1].date()}"
          f"  mean daily turnover {out['quote_volume'].mean()/1e6:>8,.1f}m")
    return out


def _classify(corr: float) -> str:
    """Label a correlation coefficient for the summary table."""
    if corr < -0.1:
        return "negative"
    if corr < 0.15:
        return "roughly uncorrelated"
    if corr < 0.5:
        return "weakly positive"
    return "strongly positive"


def main() -> None:
    """Fetch the panel, compute the three views, and print them."""
    print("=" * 78)
    print("Fetching daily bars from data-api.binance.vision")
    print("=" * 78)
    closes, turnover = {}, {}
    for symbol in CANDIDATES:
        frame = _fetch_klines(symbol)
        if frame is not None:
            closes[symbol] = frame["close"]
            turnover[symbol] = frame["quote_volume"].mean()
        time.sleep(SLEEP_SEC)

    prices = pd.DataFrame(closes).sort_index()
    returns = np.log(prices / prices.shift(1)).dropna(how="all")
    print(f"\nReturn panel: {returns.shape[0]} days by {returns.shape[1]} instruments")
    print(f"Window: {returns.index[0].date()} to {returns.index[-1].date()}")

    btc = returns["BTCUSDT"]

    # ---- View 1: whole-window correlation against BTC ----
    print("\n" + "=" * 78)
    print("Correlation of daily log returns against BTCUSDT, whole window")
    print("=" * 78)
    print(f"{'symbol':<12}{'corr':>10}{'overlap':>10}{'turnover (m)':>16}   classification")
    print("-" * 78)
    stats = []
    for symbol in returns.columns:
        if symbol == "BTCUSDT":
            continue
        pair = pd.concat([btc, returns[symbol]], axis=1).dropna()
        if len(pair) < MIN_OVERLAP_DAYS:
            continue
        stats.append((pair.iloc[:, 0].corr(pair.iloc[:, 1]), symbol, len(pair),
                      turnover.get(symbol, float("nan"))))
    stats.sort()
    for corr, symbol, n, turn in stats:
        print(f"{symbol:<12}{corr:>10.3f}{n:>10}{turn/1e6:>16,.1f}   {_classify(corr)}")

    # ---- View 2: behavior when BTC falls hard ----
    # A hedge is only worth holding if it holds up precisely when the majors do
    # not. A low whole-window coefficient can coexist with heavy losses on exactly
    # those days, so the mean return in the subsample matters more than the
    # coefficient does.
    print("\n" + "=" * 78)
    print("Crisis subsample: BTC's worst decile of daily returns")
    print("=" * 78)
    threshold = btc.quantile(CRASH_QUANTILE)
    crash = returns[btc <= threshold]
    print(f"Threshold: BTC daily return <= {threshold:.2%}, {len(crash)} days\n")
    print(f"{'symbol':<12}{'full corr':>12}{'crisis corr':>13}{'mean return':>14}{'BTC same days':>16}")
    print("-" * 78)
    for corr, symbol, _n, _turn in stats:
        sub = crash[["BTCUSDT", symbol]].dropna()
        if len(sub) < 20:
            continue
        print(f"{symbol:<12}{corr:>12.3f}{sub['BTCUSDT'].corr(sub[symbol]):>13.3f}"
              f"{sub[symbol].mean():>14.2%}{sub['BTCUSDT'].mean():>16.2%}")

    # ---- View 3: stability across years ----
    # A coefficient that is negative in a single year and positive in the others
    # is noise, not a hedge.
    print("\n" + "=" * 78)
    print("Correlation by calendar year")
    print("=" * 78)
    yearly = {}
    for symbol in [s for _c, s, _n, _t in stats]:
        row = {}
        for year, group in returns.groupby(returns.index.year):
            sub = group[["BTCUSDT", symbol]].dropna()
            row[year] = (sub["BTCUSDT"].corr(sub[symbol])
                         if len(sub) >= MIN_OVERLAP_DAYS else np.nan)
        yearly[symbol] = row
    print(pd.DataFrame(yearly).T.round(3).to_string())


if __name__ == "__main__":
    main()
