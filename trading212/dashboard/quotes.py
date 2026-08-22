"""Delayed price quotes for the dashboard.

Responsibility: fetch a recent price for every symbol the dashboard shows,
in one request per poll, and say how stale each price is. Trading 212
publishes no market data at all, so quotes come from the same vendor the
strategy's bars come from, which also means the dashboard and the strategy
never disagree about what a symbol last traded at.

Freshness: the vendor's finest interval is one minute, so a quote can be up
to about one minute behind the market. That is inside the ceiling the
dashboard was specified to hold, and the age of every quote is reported
alongside it rather than assumed.

Out of scope: the strategy's own bar pipeline, which belongs to
trading212/ingest/yahoo_bars.py and must stay the only writer of the curated
store; sampling and persistence, which belong to collector.py and
snapshots.py. This module writes nothing.

Public functions:
    fetch_quotes(symbols)   Latest price and age per symbol, in one call.

Constants:
    QUOTE_INTERVAL  str  "1m", the vendor's finest bar.
    QUOTE_PERIOD    str  "1d", enough history to always contain a last bar
                         even right after an open.
    STALE_SECONDS   int  90. Above this age a quote is reported as stale so
                         the interface can grey it out instead of implying it
                         is live.

Inputs:
    The Yahoo bar endpoint, through the yfinance package.
Outputs:
    None.

Change log:
    2026-08-22  Created.
"""

from __future__ import annotations

__all__ = ["fetch_quotes", "QUOTE_INTERVAL", "QUOTE_PERIOD", "STALE_SECONDS"]

import warnings
from datetime import datetime, timezone

from common.logging_setup import get_logger

log = get_logger("t212.dashboard")

QUOTE_INTERVAL = "1m"
QUOTE_PERIOD = "1d"
STALE_SECONDS = 90


def fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    """Return {symbol: {price, ts, age_sec, stale, ok}} for every symbol.

    One batched request covers the whole list. A symbol the vendor does not
    return is reported with ok=False rather than dropped, because a missing
    symbol on a dashboard must look missing, not look flat.
    """
    result: dict[str, dict] = {s: {"ok": False, "price": None, "ts": None,
                                   "age_sec": None, "stale": True}
                               for s in symbols}
    if not symbols:
        return result
    try:
        import yfinance as yf
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            frame = yf.download(symbols, period=QUOTE_PERIOD,
                                interval=QUOTE_INTERVAL, progress=False,
                                auto_adjust=False, group_by="column")
    except Exception as exc:
        log.warning("[quotes] fetch failed: %r", exc)
        return result
    if frame is None or frame.empty:
        log.warning("[quotes] vendor returned nothing for %d symbols",
                    len(symbols))
        return result

    now = datetime.now(timezone.utc)
    closes = frame["Close"] if "Close" in frame else frame
    for symbol in symbols:
        try:
            series = closes[symbol] if symbol in getattr(closes, "columns", []) \
                else closes
            series = series.dropna()
            if series.empty:
                continue
            ts = series.index[-1].to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (now - ts).total_seconds()
            result[symbol] = {"ok": True, "price": float(series.iloc[-1]),
                              "ts": ts.isoformat(), "age_sec": round(age, 1),
                              "stale": age > STALE_SECONDS}
        except Exception as exc:
            log.warning("[quotes] %s unreadable: %r", symbol, exc)
    return result
