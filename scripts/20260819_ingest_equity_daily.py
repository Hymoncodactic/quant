"""Equity ingest: daily OHLCV for the mega-cap universe and the hedge candidates.

Daily is the finest granularity available free and continuous for US equities.
Measured limits on this host, 2026-08-19: one-minute bars cap at 8 days per
request and about a month of history; five-minute at 60 days; hourly at 730 days;
daily reaches back to 1980. Intraday equity work therefore needs a paid source,
and this script deliberately builds the daily backbone instead.

The universe is split three ways because a UK retail investor cannot buy most of
what the US hedge literature discusses:

    us_equity   Mega-caps and peers. Available on Trading 212 as ordinary shares.
    us_etf      Reference only. US-domiciled ETFs are barred from UK retail sale
                by two independent obstacles: PRIIPs requires a Key Information
                Document that US managers will not produce, and FSMA 2000 s238
                bars promotion of unrecognized collective investment schemes.
                Downloaded for research comparability, not as trade candidates.
    uk_tradable UCITS and ETC equivalents listed in London. Availability on
                Trading 212 specifically is UNVERIFIED and must be checked in the
                app before any of these is treated as investable.

Prices are adjusted for splits and dividends. Unadjusted closes understate every
income-paying instrument, which is precisely the group most relevant to hedging.

Public functions:
    main()   Download and store the daily panels
"""

from __future__ import annotations

__all__ = ["main"]

import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pyarrow as pa

from common.paths import DIR_DATA
from common.store import write_table

warnings.filterwarnings("ignore")

# Take every bar the source will give rather than a fixed start date. Depth
# varies enormously by instrument: the mega-caps reach back to the 1980s and the
# consumer staples to the 1960s, while PLTR only lists from 2020. A hardcoded
# start silently discards decades from the older names, which are precisely the
# ones that carry more than one interest-rate and inflation regime.
PERIOD_MAX = "max"

# Yahoo throttles per host rather than banning by address, so a short exponential
# pause clears it. Measured 2026-08-19: query1 returned 429 while query2 served
# the same request successfully from this machine.
RETRY_BASE_SEC = 2.0
PACE_SEC = 0.6
VENUE = "t212"
LAYER = "curated"
PERIOD = "1d"

UNIVERSE: dict[str, dict[str, str]] = {
    "us_equity": {
        "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AMZN": "Amazon",
        "GOOGL": "Alphabet", "META": "Meta", "TSLA": "Tesla", "AVGO": "Broadcom",
        "AMD": "AMD", "MU": "Micron", "INTC": "Intel", "TSM": "TSMC",
        "ORCL": "Oracle", "PLTR": "Palantir", "MRVL": "Marvell", "LRCX": "Lam Research",
        "AMAT": "Applied Materials", "DELL": "Dell",
        # Defensive names, the part of the hedge question that survived testing
        "KO": "Coca-Cola", "PG": "Procter & Gamble", "JNJ": "Johnson & Johnson",
        "WMT": "Walmart", "NEM": "Newmont", "XOM": "Exxon",
    },
    "us_etf": {
        "SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000",
        "GLD": "Gold bullion", "GDX": "Gold miners",
        "TLT": "Treasuries 20y+", "IEF": "Treasuries 7-10y", "SHY": "Treasuries 1-3y",
        "BIL": "T-bills 1-3m",
        "XLU": "Utilities", "XLP": "Consumer staples", "XLV": "Healthcare",
        "UUP": "US dollar index",
        "SH": "Inverse S&P 500", "PSQ": "Inverse Nasdaq 100",
        "SQQQ": "-3x Nasdaq 100", "VXX": "VIX short-term futures",
    },
    "uk_tradable": {
        "SGLN.L": "iShares Physical Gold ETC, GBX",
        "IGLN.L": "iShares Physical Gold ETC, USD",
        "IB01.L": "iShares USD T-Bond 0-1yr UCITS",
        "IDTL.L": "iShares USD T-Bond 20+yr UCITS, USD",
        "IBTL.L": "iShares USD T-Bond 20+yr UCITS, GBP",
        "XSPS.L": "Xtrackers S&P 500 Inverse Daily Swap UCITS",
        "VUSA.L": "Vanguard S&P 500 UCITS",
        "CSPX.L": "iShares Core S&P 500 UCITS acc",
        "EQQQ.L": "Invesco Nasdaq 100 UCITS",
        "IUCS.L": "iShares S&P 500 Consumer Staples UCITS",
        "GBPUSD=X": "GBP/USD spot",
    },
}


def _quote_currencies(tickers: list[str]) -> dict[str, str]:
    """Return each ticker's exchange quote currency.

    London lists instruments in three different currencies and the price column
    alone does not reveal which. GBp is pence, so a GBp price is 100x a GBP price
    for the same value; mixing them silently inflates any turnover or return
    calculation by two orders of magnitude. Recording the currency alongside the
    price is the only way to make the panel safe to aggregate.

    US listings are uniformly USD, so the metadata call is only made for the
    London lines. That call is slow and is not worth paying 40 times over for an
    answer that is known.
    """
    import yfinance as yf
    out = {}
    for ticker in tickers:
        if not ticker.endswith(".L"):
            out[ticker] = "USD"
            continue
        try:
            out[ticker] = yf.Ticker(ticker).get_info().get("currency", "UNKNOWN")
        except Exception:
            out[ticker] = "UNKNOWN"
    return out


def _fetch_one(ticker: str, attempts: int = 4) -> pd.DataFrame:
    """Fetch one ticker's full daily history, retrying on transient throttling.

    Tickers are requested one at a time rather than as a batch. The batch path
    resolves period="max" to a single range starting in 1927 and applies it to
    every symbol at once, which Yahoo throttles: a 24-ticker batch returned data
    for 3 and "possibly delisted" for 21, none of which are delisted. One request
    per ticker asks Yahoo for that instrument's own listed range and succeeds.

    Failures are returned as an empty frame and reported by the caller. A silent
    skip that still prints a success line is worse than a visible gap.
    """
    import yfinance as yf
    for attempt in range(1, attempts + 1):
        try:
            frame = yf.Ticker(ticker).history(period=PERIOD_MAX, interval="1d",
                                              auto_adjust=True)
            if frame is not None and not frame.empty:
                out = frame[["Open", "High", "Low", "Close", "Volume"]].copy()
                out.columns = ["open", "high", "low", "close", "volume"]
                out.index.name = "ts"
                return out.reset_index()
        except Exception:
            pass
        if attempt < attempts:
            time.sleep(RETRY_BASE_SEC * (2 ** (attempt - 1)))
    return pd.DataFrame()


def main() -> None:
    """Download each universe group and write one Parquet file per symbol."""
    total_rows = total_bytes = 0
    print("=" * 92)
    print("Equity daily OHLCV, maximum available history, split-and-dividend adjusted")
    print("=" * 92)

    for group, members in UNIVERSE.items():
        tickers = list(members)
        print(f"\n[{group}] {len(tickers)} instruments")
        currencies = _quote_currencies(tickers)
        failed = []
        for ticker in tickers:
            part = _fetch_one(ticker)
            time.sleep(PACE_SEC)
            if part.empty:
                failed.append(ticker)
                print(f"  {ticker:<10} {members[ticker]:<38} FAILED after retries")
                continue
            part["ts"] = pd.to_datetime(part["ts"], utc=True)
            part["quote_ccy"] = currencies.get(ticker, "UNKNOWN")
            out = (DIR_DATA / VENUE / LAYER / group / ticker / PERIOD
                   / f"{ticker.replace('=', '_')}.parquet")
            write_table(pa.Table.from_pandas(part, preserve_index=False), out, sort_by="ts")
            size = out.stat().st_size
            total_rows += len(part)
            total_bytes += size
            print(f"  {ticker:<10} {members[ticker]:<38} {len(part):>6,} bars  "
                  f"{part.ts.min().date()} .. {part.ts.max().date()}  "
                  f"{currencies.get(ticker,'?'):>4}  {size/1024:>7,.0f} KB")
        if failed:
            print(f"  !! {len(failed)} FAILED in {group}: {failed}")

    print("\n" + "=" * 92)
    print(f"RESULT  {total_rows:,} rows, {total_bytes/1e6:.1f} MB parquet")
    print(f"Location: {DIR_DATA / VENUE / LAYER}")


if __name__ == "__main__":
    main()
