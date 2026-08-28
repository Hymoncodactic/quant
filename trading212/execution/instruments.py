"""Instrument mapping and the venue's own session calendar.

Responsibility: translate strategy symbols (Yahoo style, "AAPL") into the
venue's order tickers ("AAPL_US_EQ"), prove that mapping against the live
metadata endpoint, and turn the venue's working schedules into the session
facts the A0 hourly cycle needs: when a session opens and closes, whether it
is a full session, and the exact UTC instant of its 15:30 decision bar.

Session facts come from the venue calendar rather than from bar absence,
because inferring a half day from a missing 15:30 bar is an after-the-fact
guess and would also fire on a data outage
(fixplans/t212/a0/02_execution.md section 2.1).

Out of scope: market data of any kind, which belongs to
trading212/execution/market_data.py; order placement, which belongs to
trading212/execution/order_router.py; the decision itself, which belongs to
trading212/execution/session_cycle.py.

Public functions:
    order_ticker(symbol)                     Venue ticker for one strategy symbol.
    validate_mapping(client, symbols)         Prove every mapping against metadata.
    schedule_divergences(cal, ids, date_ny)   Whether the universe's schedules agree.
    refresh_calendar(client, cache_path)     Fetch working schedules, cache them.
    load_calendar(cache_path)                Load the cached schedules.
    session_events(calendar, schedule_id)    Sorted (ts_utc, type) event list.
    sessions(events)                         Regular sessions as Session records.
    session_on(sessions_, date_ny)           The session of one exchange-local date.
    current_session(sessions_, now_utc)      The session now falls inside, or None.
    last_full_session(sessions_, now_utc)    Most recent finished full session.
    decision_key(session)                    UTC instant of its 15:30 bar.
    market_is_open(events, now_utc)          Whether the regular session is open.

Public classes:
    Session   One regular trading session: local date, open, close, fullness.

Constants:
    A0_ORDER_TICKERS      dict  Strategy symbol to venue ticker. Source:
                                GET /api/v0/equity/metadata/instruments,
                                fetched 2026-08-21, stored at
                                data/reference/t212_instruments_20260821.json.
                                META's US listing is FB_US_EQ; METAl_EQ and
                                METAm_EQ are European ETFs sharing the short
                                name, so the ticker is never guessed.
    US_SCHEDULE_ID_NASDAQ int  71. Source: same fetch, exchange 53 NASDAQ.
    CALENDAR_STALE_DAYS   int  5. The venue publishes about six weeks of
                               forward calendar (observed span 2026-08-03 to
                               2026-09-14 on 2026-08-21), so a cache older
                               than this may not cover today.
    DECISION_TIME_NY      str  "15:30". The 1h decision bar's exchange-local
                               start time. Source:
                               fixplans/t212/a0/02_execution.md section 2.1.
    FULL_SESSION_CLOSE_NY str  "16:00". A session closing earlier is a half
                               day, which has no 15:30 bar and therefore no
                               decision. Source: same section.
    REGULAR_CLOSE_KINDS   tuple Event kinds that end the regular session. US
                               schedules carry no CLOSE event at all: the
                               regular close is marked by AFTER_HOURS_OPEN.
                               Verified 2026-08-21 on schedules 71 and 56.

Inputs:
    GET /api/v0/equity/metadata/instruments
    GET /api/v0/equity/metadata/exchanges
Outputs:
    data/t212/execution_state/exchange_calendar.json   cached schedules

Change log:
    2026-08-21  Created for the daily A0 cycle.
    2026-08-22  Rewritten for the hourly arm: Session records, decision_key(),
                full-session detection from the venue calendar. The daily
                helpers last_completed_trading_day() and next_event() were
                dropped; the hourly cycle asks about sessions, not days.
"""

from __future__ import annotations

__all__ = ["order_ticker", "validate_mapping", "refresh_calendar",
           "load_calendar", "session_events", "sessions", "session_on",
           "current_session", "last_full_session", "decision_key",
           "market_is_open", "Session",
           "A0_ORDER_TICKERS", "US_SCHEDULE_ID_NASDAQ", "CALENDAR_STALE_DAYS",
           "DECISION_TIME_NY", "FULL_SESSION_CLOSE_NY", "REGULAR_CLOSE_KINDS"]

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from common.logging_setup import get_logger

log = get_logger("t212.execution")

A0_ORDER_TICKERS: dict[str, str] = {
    "AAPL": "AAPL_US_EQ", "AMAT": "AMAT_US_EQ", "AMD": "AMD_US_EQ",
    "AMZN": "AMZN_US_EQ", "AVGO": "AVGO_US_EQ", "DELL": "DELL_US_EQ",
    "GOOGL": "GOOGL_US_EQ", "INTC": "INTC_US_EQ", "LRCX": "LRCX_US_EQ",
    "META": "FB_US_EQ",
    "MRVL": "MRVL_US_EQ", "MSFT": "MSFT_US_EQ", "MU": "MU_US_EQ",
    "NVDA": "NVDA_US_EQ", "ORCL": "ORCL_US_EQ", "PLTR": "PLTR_US_EQ",
    "TSLA": "TSLA_US_EQ", "TSM": "TSM_US_EQ",
}

US_SCHEDULE_ID_NASDAQ = 71
CALENDAR_STALE_DAYS = 5

DECISION_TIME_NY = "15:30"
FULL_SESSION_CLOSE_NY = "16:00"
EXCHANGE_TZ = "America/New_York"

_OPEN_KIND = "OPEN"
REGULAR_CLOSE_KINDS = ("CLOSE", "AFTER_HOURS_OPEN", "AFTER_HOURS_CLOSE",
                       "BREAK_START")
_REOPEN_KINDS = ("OPEN", "BREAK_END")


@dataclass(frozen=True)
class Session:
    """One regular trading session, in exchange-local terms.

    date_ny is the exchange-local calendar date; open_utc and close_utc are
    the regular session boundaries; is_full says the session closes at
    FULL_SESSION_CLOSE_NY and therefore has a 15:30 bar to decide on.
    """
    date_ny: date
    open_utc: pd.Timestamp
    close_utc: pd.Timestamp

    @property
    def is_full(self) -> bool:
        local = self.close_utc.tz_convert(EXCHANGE_TZ)
        return local.strftime("%H:%M") == FULL_SESSION_CLOSE_NY


# ============================================================================
# [1] Mapping
# ============================================================================

def order_ticker(symbol: str) -> str:
    """Return the venue order ticker for one strategy symbol.

    Raises KeyError for an unmapped symbol rather than deriving a ticker from
    the symbol name, because several strategy symbols have same-named foreign
    listings that would silently route the order elsewhere.
    """
    if symbol not in A0_ORDER_TICKERS:
        raise KeyError(f"no verified T212 ticker mapping for {symbol!r}; add it "
                       f"to A0_ORDER_TICKERS after checking the metadata endpoint")
    return A0_ORDER_TICKERS[symbol]


def validate_mapping(client, symbols: list[str]) -> dict[str, dict]:
    """Prove the static mapping against the venue's live instrument metadata.

    Per symbol: the mapped ticker exists, quotes in USD and is a STOCK. Any
    failure raises, because trading a universe whose identity cannot be
    proven is worse than not trading.
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


def schedule_divergences(calendar: list[dict], schedule_ids: set[int],
                         session_date) -> list[str]:
    """Whether every schedule the universe trades on agrees for one session.

    The cycle derives ONE decision key and close from
    US_SCHEDULE_ID_NASDAQ, but a US universe spans exchanges: NYSE-listed
    names carry a different working schedule id. Those schedules keep the
    same regular hours and holidays, so the single-schedule model has been
    correct in every cached session -- but nothing forced it to stay that
    way, and a divergence would time part of the universe against the wrong
    close while looking perfectly normal. Cheap to check, so it is checked.

    Returns a list of human-readable divergences; empty means agreement.
    """
    reference = None
    problems: list[str] = []
    for schedule_id in sorted(schedule_ids):
        found = session_on(sessions(session_events(calendar, schedule_id)),
                           session_date)
        if found is None:
            problems.append(f"schedule {schedule_id} has no session on "
                            f"{session_date}")
            continue
        shape = (found.open_utc, found.close_utc, found.is_full)
        if reference is None:
            reference = (schedule_id, shape)
        elif shape != reference[1]:
            problems.append(
                f"schedule {schedule_id} session {session_date} is "
                f"{shape} but schedule {reference[0]} is {reference[1]}")
    return problems


# ============================================================================
# [2] Calendar retrieval
# ============================================================================

def refresh_calendar(client, cache_path: Path) -> list[dict]:
    """Fetch the venue's exchange schedules and cache them atomically."""
    calendar = client.exchanges()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".writing")
    tmp.write_text(json.dumps({"fetched_at_utc": str(pd.Timestamp.now(tz="UTC")),
                               "exchanges": calendar}), encoding="utf-8")
    tmp.replace(cache_path)
    return calendar


def load_calendar(cache_path: Path) -> list[dict]:
    """Load cached schedules, refusing anything older than the stale bound."""
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    fetched = pd.Timestamp(payload["fetched_at_utc"])
    age_days = (pd.Timestamp.now(tz="UTC") - fetched).total_seconds() / 86400
    if age_days > CALENDAR_STALE_DAYS:
        raise RuntimeError(f"exchange calendar cache is {age_days:.1f} days old; "
                           f"refresh it before trading against it")
    return payload["exchanges"]


def session_events(calendar: list[dict],
                   schedule_id: int) -> list[tuple[pd.Timestamp, str]]:
    """Return one working schedule's (ts_utc, type) events, sorted."""
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


# ============================================================================
# [3] Sessions
# ============================================================================

def sessions(events: list[tuple[pd.Timestamp, str]]) -> list[Session]:
    """Fold the event list into regular sessions.

    A session runs from an OPEN to the next regular-close event. An OPEN with
    no following close inside the published window is dropped rather than
    given an assumed close time.
    """
    out: list[Session] = []
    pending_open: pd.Timestamp | None = None
    for ts, kind in events:
        if kind == _OPEN_KIND:
            pending_open = ts
        elif kind in REGULAR_CLOSE_KINDS and pending_open is not None:
            out.append(Session(
                date_ny=pending_open.tz_convert(EXCHANGE_TZ).date(),
                open_utc=pending_open, close_utc=ts))
            pending_open = None
    return out


def session_on(sessions_: list[Session], date_ny: date) -> Session | None:
    """The session whose exchange-local date is date_ny, if published."""
    for session in sessions_:
        if session.date_ny == date_ny:
            return session
    return None


def current_session(sessions_: list[Session],
                    now_utc: pd.Timestamp) -> Session | None:
    """The session whose regular hours contain now_utc, if any."""
    for session in sessions_:
        if session.open_utc <= now_utc < session.close_utc:
            return session
    return None


def last_full_session(sessions_: list[Session],
                      now_utc: pd.Timestamp) -> Session | None:
    """The most recent full session that has already closed."""
    done = [s for s in sessions_ if s.close_utc <= now_utc and s.is_full]
    return done[-1] if done else None


def decision_key(session: Session) -> pd.Timestamp:
    """UTC instant of the session's 15:30 decision bar.

    The 1h bar timestamp is the bar's START (docs/data/t212/DATA_SPEC.md
    section 3), so the decision key is the exchange-local 15:30 of that
    session converted to UTC: 19:30Z under EDT, 20:30Z under EST. Never a
    fixed UTC time.

    Raises:
        ValueError: The session is a half day and has no 15:30 bar.
    """
    if not session.is_full:
        raise ValueError(f"session {session.date_ny} closes at "
                         f"{session.close_utc.tz_convert(EXCHANGE_TZ):%H:%M} "
                         f"and has no {DECISION_TIME_NY} bar")
    hour, minute = (int(part) for part in DECISION_TIME_NY.split(":"))
    local = pd.Timestamp(session.date_ny, tz=EXCHANGE_TZ) \
        + pd.Timedelta(hours=hour, minutes=minute)
    return local.tz_convert("UTC")


def market_is_open(events: list[tuple[pd.Timestamp, str]],
                   now_utc: pd.Timestamp) -> bool:
    """Whether the regular session is open at now_utc.

    Pre-market, after-hours and overnight all count as closed: the regular
    session interval is [OPEN, AFTER_HOURS_OPEN) on US schedules.
    """
    state_open = False
    for ts, kind in events:
        if ts > now_utc:
            break
        if kind in _REOPEN_KINDS:
            state_open = True
        elif kind in REGULAR_CLOSE_KINDS:
            state_open = False
    return state_open
