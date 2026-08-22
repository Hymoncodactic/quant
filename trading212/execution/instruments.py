"""T212 instrument mapping and the venue's market calendar.

Responsibility: translate strategy symbols (Yahoo-style, e.g. "AAPL") to the
venue's order tickers (e.g. "AAPL_US_EQ"), validate the mapping against the
live metadata endpoint, and answer session-calendar questions (next open,
last completed trading day) from the venue's own working schedules.
Not responsible for: market data (market_data.py) or any order logic.

Mapping provenance (S4): GET /api/v0/equity/metadata/instruments fetched
2026-08-21 against the live host, stored at
data/reference/t212_instruments_20260821.json. Every entry below was matched
by shortName and filtered to the USD US primary listing (ticker suffix
_US_EQ, type STOCK). META's US listing trades as FB_US_EQ; METAl_EQ and
METAm_EQ are European ETFs that happen to share the short name.

Public functions:
    order_ticker(symbol)                     Venue ticker for one strategy symbol
    validate_mapping(client, symbols)        Assert every mapping against live metadata
    refresh_calendar(client, cache_path)     Fetch working schedules, cache to disk
    load_calendar(cache_path)                Load the cached schedules
    session_events(calendar, schedule_id)    Sorted (ts_utc, type) list
    next_event(events, now_utc, kinds)       First matching event at or after now
    last_completed_trading_day(events, now)  Exchange-local date of the last CLOSE
    market_is_open(events, now_utc)          Whether the regular session is open

Public constants:
    A0_ORDER_TICKERS, US_SCHEDULE_ID_NASDAQ, CALENDAR_STALE_DAYS
"""

from __future__ import annotations

__all__ = ["order_ticker", "validate_mapping", "refresh_calendar",
           "load_calendar", "session_events", "next_event",
           "last_completed_trading_day", "market_is_open",
           "A0_ORDER_TICKERS", "US_SCHEDULE_ID_NASDAQ", "CALENDAR_STALE_DAYS"]

import json
from pathlib import Path

import pandas as pd

from common.logging_setup import get_logger

log = get_logger("t212.execution")

# ============================================================================
# [1] Constants
# ============================================================================

# Strategy symbol -> venue order ticker. Source: instrument metadata fetched
# 2026-08-21 (data/reference/t212_instruments_20260821.json); each ticker was
# verified to exist with currencyCode=USD and type=STOCK.
A0_ORDER_TICKERS: dict[str, str] = {
    "AAPL": "AAPL_US_EQ", "AMAT": "AMAT_US_EQ", "AMD": "AMD_US_EQ",
    "AMZN": "AMZN_US_EQ", "AVGO": "AVGO_US_EQ", "DELL": "DELL_US_EQ",
    "GOOGL": "GOOGL_US_EQ", "INTC": "INTC_US_EQ", "LRCX": "LRCX_US_EQ",
    "META": "FB_US_EQ",   # US listing keeps the pre-rename ticker FB
    "MRVL": "MRVL_US_EQ", "MSFT": "MSFT_US_EQ", "MU": "MU_US_EQ",
    "NVDA": "NVDA_US_EQ", "ORCL": "ORCL_US_EQ", "PLTR": "PLTR_US_EQ",
    "TSLA": "TSLA_US_EQ", "TSM": "TSM_US_EQ",
}

# NASDAQ regular-session schedule id observed 2026-08-21 in the exchanges
# metadata (exchange 53 NASDAQ -> schedules [109, 71, 110]; the A0 universe
# instruments carry workingScheduleId 71 or 56 (NYSE)). Schedule ids are
# venue-assigned and could change; validate_mapping() re-reads them from the
# live metadata on every run instead of trusting this constant.
US_SCHEDULE_ID_NASDAQ = 71

# The venue publishes about six weeks of forward calendar (observed span
# 2026-08-03 .. 2026-09-14 on 2026-08-21). A cache older than this many days
# may no longer cover "today" and must be refreshed before use.
CALENDAR_STALE_DAYS = 5

# Regular-session boundary semantics, verified on the ACTUAL schedule-71/56
# payload of 2026-08-21 (data/reference/t212_exchanges_20260821.json): US
# schedules carry NO plain CLOSE event at all -- each day runs
# OVERNIGHT_OPEN 00:00Z, PRE_MARKET_OPEN 08:00Z, OPEN 13:30Z,
# AFTER_HOURS_OPEN 20:00Z, with AFTER_HOURS_CLOSE only at the weekend
# boundary. The regular session therefore ENDS at AFTER_HOURS_OPEN. CLOSE
# and the BREAK pair exist on other exchanges' schedules (spec TimeEvent
# enum) and are honored for generality.
_REGULAR_OPEN_KINDS = ("OPEN", "BREAK_END")
_REGULAR_CLOSE_KINDS = ("CLOSE", "AFTER_HOURS_OPEN", "AFTER_HOURS_CLOSE",
                        "BREAK_START")
# Kinds that anchor a COMPLETED trading day (a break or the after-hours end
# does not finish the day's bar; the regular close does).
_DAY_CLOSE_KINDS = ("CLOSE", "AFTER_HOURS_OPEN")


# ============================================================================
# [2] Mapping
# ============================================================================

def order_ticker(symbol: str) -> str:
    """Return the venue order ticker for one strategy symbol.

    Raises:
        KeyError: The symbol has no verified mapping. Never guess a ticker
            from the symbol name; METAs European ETF twins show why.
    """
    if symbol not in A0_ORDER_TICKERS:
        raise KeyError(f"no verified T212 ticker mapping for {symbol!r}; add it "
                       f"to A0_ORDER_TICKERS after checking the metadata endpoint")
    return A0_ORDER_TICKERS[symbol]


def validate_mapping(client, symbols: list[str]) -> dict[str, dict]:
    """Assert the static mapping against the venue's live instrument metadata.

    Checks per symbol: the mapped ticker exists, quotes in USD, is a STOCK.
    Returns the metadata entry per symbol so callers can read
    workingScheduleId / maxOpenQuantity without a second fetch.

    Raises:
        RuntimeError: Any symbol fails any check. The execution layer must
            not trade a universe whose identity it cannot prove.
    """
    index = {inst.get("ticker"): inst for inst in client.instruments()}
    result: dict[str, dict] = {}
    problems: list[str] = []
    for symbol in symbols:
        ticker = order_ticker(symbol)
        meta = index.get(ticker)
        if meta is None:
            problems.append(f"{symbol}: ticker {ticker} absent from metadata")
            continue
        if meta.get("currencyCode") != "USD" or meta.get("type") != "STOCK":
            problems.append(f"{symbol}: {ticker} is {meta.get('currencyCode')}"
                            f"/{meta.get('type')}, expected USD/STOCK")
            continue
        result[symbol] = meta
    if problems:
        raise RuntimeError("instrument mapping validation failed: "
                           + "; ".join(problems))
    log.info("[instruments] mapping validated for %d symbols", len(result))
    return result


# ============================================================================
# [3] Calendar
# ============================================================================

def refresh_calendar(client, cache_path: Path) -> list[dict]:
    """Fetch the venue's exchange schedules and cache them to disk.

    The venue refreshes this data every 10 minutes and publishes roughly six
    weeks forward; the cache exists so `settle` and `status` phases do not
    burn the 1-per-30s rate limit re-fetching what `decide` already saw.
    """
    calendar = client.exchanges()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".writing")
    tmp.write_text(json.dumps({"fetched_at_utc": str(pd.Timestamp.now(tz="UTC")),
                               "exchanges": calendar}), encoding="utf-8")
    tmp.replace(cache_path)
    return calendar


def load_calendar(cache_path: Path) -> list[dict]:
    """Load cached schedules; raise when missing or stale.

    Raises:
        FileNotFoundError: No cache present; call refresh_calendar first.
        RuntimeError: The cache is older than CALENDAR_STALE_DAYS.
    """
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    fetched = pd.Timestamp(payload["fetched_at_utc"])
    age_days = (pd.Timestamp.now(tz="UTC") - fetched).total_seconds() / 86400
    if age_days > CALENDAR_STALE_DAYS:
        raise RuntimeError(f"exchange calendar cache is {age_days:.1f} days old; "
                           f"refresh it before trading against it")
    return payload["exchanges"]


def session_events(calendar: list[dict], schedule_id: int) -> list[tuple[pd.Timestamp, str]]:
    """Return the (ts_utc, type) event list of one working schedule, sorted."""
    for exchange in calendar:
        for schedule in exchange.get("workingSchedules") or []:
            if schedule.get("id") == schedule_id:
                events = [(pd.Timestamp(ev["date"]), ev["type"])
                          for ev in schedule.get("timeEvents") or []]
                events.sort()
                if not events:
                    raise RuntimeError(f"schedule {schedule_id} has no events")
                return events
    raise RuntimeError(f"schedule {schedule_id} not present in the calendar")


def next_event(events: list[tuple[pd.Timestamp, str]], now_utc: pd.Timestamp,
               kinds: tuple[str, ...]) -> tuple[pd.Timestamp, str]:
    """First event of one of the given kinds at or after now_utc.

    Raises:
        RuntimeError: The calendar window ends before such an event; the
            caller must refresh the calendar rather than extrapolate.
    """
    for ts, kind in events:
        if kind in kinds and ts >= now_utc:
            return ts, kind
    raise RuntimeError(f"no {kinds} event at or after {now_utc} in the cached "
                       f"calendar window; refresh the calendar")


def last_completed_trading_day(events: list[tuple[pd.Timestamp, str]],
                               now_utc: pd.Timestamp,
                               tz_name: str = "America/New_York") -> pd.Timestamp:
    """Exchange-local date (naive midnight) of the last regular-session close
    before now.

    This is the freshness anchor for daily bars: after the regular close,
    that trading day's bar is complete and must be present in the local
    store before the strategy may decide. On US schedules the close marker
    is AFTER_HOURS_OPEN (see _DAY_CLOSE_KINDS provenance above).
    """
    last_close = None
    for ts, kind in events:
        if kind in _DAY_CLOSE_KINDS and ts <= now_utc:
            last_close = ts
    if last_close is None:
        raise RuntimeError(f"no regular-close event at or before {now_utc} in "
                           f"the cached calendar window; the window may start "
                           f"too late")
    return pd.Timestamp(last_close.tz_convert(tz_name).date())


def market_is_open(events: list[tuple[pd.Timestamp, str]],
                   now_utc: pd.Timestamp) -> bool:
    """Whether the regular session is open at now_utc.

    Pre-market, after-hours and overnight count as closed: A0 submits while
    closed so queued market orders fill at the next regular open. The open
    interval is [OPEN, AFTER_HOURS_OPEN) on US schedules (see the boundary
    provenance above); CLOSE and BREAK events are honored where they exist.
    """
    state_open = False
    for ts, kind in events:
        if ts > now_utc:
            break
        if kind in _REGULAR_OPEN_KINDS:
            state_open = True
        elif kind in _REGULAR_CLOSE_KINDS:
            state_open = False
    return state_open
