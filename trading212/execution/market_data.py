"""Market data for the live A0 hourly cycle: refresh, load, cutoff view.

Responsibility: keep the curated store current for the symbols A0 needs,
read it back, and present the exact information set the backtest's 1h arm
sees at a decision -- hourly bars up to and including the 15:30 decision bar,
plus the adjusted daily rows the intraday shim splices today's session onto.

Trading 212 publishes no market data (WORKING_MEMORY open item 2: the
equity price, quote and candle endpoints all answer 404), so bars come from
the same Yahoo pipeline the backtest was built on, through
trading212/ingest/yahoo_bars.py. Using a different source or a different
adjustment convention would make live and backtest incomparable
(fixplans/t212/a0/02_execution.md section 5).

Why the intraday view keeps the decision bar: the shim drops it itself with
a trailing slice, and it needs the bar present to prove the session is
actually trading at that key. That mirrors the engine exactly, which hands
the strategy the current bar and lets the shim discard it
(trading212/strategy/a0_intraday_v0_0_1.py, information rule).

Out of scope: fetching and partition naming, which belong to
trading212/ingest/yahoo_bars.py; the splice arithmetic and the synthetic
daily bar, which belong to trading212/strategy/a0_intraday_v0_0_1.py; the
session calendar, which belongs to trading212/execution/instruments.py;
deciding when to run, which belongs to
trading212/execution/session_cycle.py.

Public functions:
    group_for(symbol)                       Universe group holding one symbol.
    refresh_bars(symbols, interval)         Re-fetch and store one interval.
    load_frames(symbols, interval, start, end)  Read stored bars per symbol.
    daily_rows(symbols, start, end)         Shim-format adjusted daily rows.
    build_view(frames, cutoff_ts)           Cutoff-enforced LiveMarketView.
    assert_intraday_ready(frames, decision_key, trade_symbols, state_symbol,
                          fx_symbol)        Freshness gate; returns the trade
                                            symbols that lack a decision bar.
    us_sessions(start, end)                 US trading sessions from the
                                            local SPY daily partitions.
    refresh_for_decision(params, session, key)  Seam S4: every refresh one
                                            decision needs, time boxed;
                                            returns the names left thin.
    load_b0_injection(params, as_of, held)  Seam S3: the read-only injection
                                            object B0 and the dashboard share.
    a1_book_from_records(records_root)      The previous A1 book, from the
                                            a1_plan stream.
    a1_universe_for(params, as_of, held)    The A1 names one session may
                                            touch, previous book AND the
                                            pick it is about to make.

Public classes:
    LiveBar          One OHLCV bar; field-compatible with the engine's Bar.
    LiveMarketView   Read-only view exposing bar/bars/symbols/now.

Constants:
    GROUPS           tuple  Curated group search order, mirroring
                            backtest/t212/data_source.py GROUPS so both sides
                            resolve a symbol to the same partitions.
    FX_SYMBOL        str    "GBPUSD=X".
    FX_LAG_MINUTES   int    90. US equity 1h bars stamp on the half hour and
                            FX 1h bars on the hour, so the FX bar in force at
                            a 15:30 decision always starts 90 minutes earlier
                            and its close is 30 minutes stale. Verified on
                            690 of 691 backtest decision keys; the one
                            exception was an FX data hole, which this gate
                            now refuses rather than silently mispricing.
    FX_CURRENT_LAG_MINUTES int  30. The in-progress FX bar at a decision.
                            Its presence is what makes the strategy's
                            positional lookup land on the same bar the cost
                            path resolves to by time.
    A1_REFRESH_BUDGET_SEC  int  120. Whole-batch ceiling on the short-window
                            refresh of the A1 names. The decision window is
                            about half an hour and an A0-only decide already
                            measured 88 to 104 seconds, so an unbounded loop
                            over forty names would walk past the submission
                            instant and abort the session. A name the budget
                            cuts off is reported thin, not fatal.
    A1_REFRESH_ATTEMPTS int  2, and A1_REFRESH_BACKOFF_SEC 4. Deliberately far
                            shorter than the ingest ladder (6 attempts, 8 to
                            128 seconds): inside a decision, giving up on one
                            name is cheap and being late is not.
    A1_INTRADAY_DAYS int    7. Days of 1h history fetched for an A1 name. The
                            strategy reads today's decision bar and the one
                            before it; a week covers a long weekend.
    RANK_STALE_FREEZE_SESSIONS int  3. Beyond this many sessions of ranking
                            staleness the A1 leg is frozen rather than traded
                            on an old ranking.
    RANK_STALE_FREEZE_DAYS int  7. The same freeze in CALENDAR days. Session
                            staleness alone is circular: it is counted on the
                            SPY series, and the pass that stalls the ranking
                            table is the same pass that stalls SPY, so the two
                            drift together and the session counter can read
                            zero while the table is genuinely old. Measured
                            2026-09-03: a table from 2026-08-31 reported
                            rank_stale_sessions = 0. Either threshold freezes.
    MAX_SESSION_CALENDAR_LAG_DAYS int  5. How far the stored session list may
                            trail the day being decided before the rotation
                            counter is no longer trustworthy. Beyond it the
                            injection reports session_calendar_stale and the
                            cycle refuses to decide, because a session missing
                            from the middle of the list shifts every index
                            after it and silently moves the rotation.
    SESSION_SYMBOL   str    "SPY". The session calendar's single source of
                            truth: its stored daily bars ARE the US trading
                            days, half days included. The venue calendar is
                            not usable for this -- its cached span measured
                            six weeks on 2026-09-02, and refresh_calendar
                            overwrites the cache rather than merging it.
    INTRADAY_SESSIONS_LOADED int  40. Sessions of 1h history handed to the
                            shim. It only reads today's bars and the previous
                            session's last bar, so a deeper window changes no
                            number and only costs memory.

Inputs:
    data/t212/curated/<group>/<symbol>/<interval>/*.parquet
Outputs:
    data/t212/curated/<group>/<symbol>/<interval>/*.parquet   (refresh_bars)

Change log:
    2026-08-21  Created for the daily A0 cycle: daily refresh, daily cutoff
                view, day-granularity freshness gate.
    2026-08-22  Rewritten for the hourly arm. The daily-only loader became a
                general one; added daily_rows() for the shim, an hourly
                cutoff view keyed on the decision bar, and a freshness gate
                that pins the FX bar to decision_key minus FX_LAG_MINUTES.
                The old five-day FX tolerance was a daily-scale rule and is
                useless at 1h.
    2026-08-23  The freshness gate also requires the in-progress FX bar,
                because the strategy reaches its rate positionally and
                would otherwise drop an hour back unnoticed. A trade
                symbol missing its decision bar is now reported rather
                than fatal: the backtest keeps deciding and lets that
                one order queue.
"""

from __future__ import annotations

__all__ = ["group_for", "refresh_bars", "load_frames", "daily_rows",
           "build_view", "assert_intraday_ready", "us_sessions",
           "refresh_for_decision", "load_b0_injection", "a1_book_from_records",
           "a1_universe_for",
           "LiveBar", "LiveMarketView",
           "GROUPS", "FX_SYMBOL", "FX_LAG_MINUTES", "FX_CURRENT_LAG_MINUTES",
           "INTRADAY_SESSIONS_LOADED", "SESSION_SYMBOL"]

from dataclasses import dataclass

import fcntl
import time

import pandas as pd

from common.logging_setup import get_logger
from common.paths import (a1_rank_path, equity_curated_root,
                          equity_interval_dir)
from trading212 import archive
from trading212.execution.strategy_loader import load_module
from trading212.ingest.yahoo_bars import (UNIVERSE, fetch_interval, write_daily,
                                          write_intraday)

log = get_logger("t212.execution")

GROUPS = ("us_equity", "us_etf", "uk_tradable")
FX_SYMBOL = "GBPUSD=X"
FX_LAG_MINUTES = 90
FX_CURRENT_LAG_MINUTES = 30
INTRADAY_SESSIONS_LOADED = 40
SESSION_SYMBOL = "SPY"
A1_REFRESH_BUDGET_SEC = 120
A1_REFRESH_ATTEMPTS = 2
A1_REFRESH_BACKOFF_SEC = 4
A1_INTRADAY_DAYS = 7
RANK_STALE_FREEZE_SESSIONS = 3
RANK_STALE_FREEZE_DAYS = 7
MAX_SESSION_CALENDAR_LAG_DAYS = 5

_TZ_LONDON = "Europe/London"
_TZ_NEW_YORK = "America/New_York"

# Fetch spans per interval, mirroring trading212/ingest/yahoo_bars.INTERVALS:
# the daily series is refetched whole because adjustment is retroactive, and
# 1h is capped by the vendor at 730 sessions.
_FETCH_SPAN = {"1d": (None, None), "1h": (730, None)}

# The interval the live cycle decides on. Kept here rather than
# imported from session_cycle, which imports this module.
_LIVE_INTERVAL = "1h"


@dataclass(frozen=True)
class LiveBar:
    """One OHLCV bar; field-compatible with backtest/engine/types.py Bar."""
    ts: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_ccy: str


# ============================================================================
# [1] Refresh and load
# ============================================================================

def group_for(symbol: str) -> str:
    """Return the universe group holding one symbol.

    The configured universe is consulted first, then the disk. B0 trades names
    from a 1,500-strong candidate pool that was never listed in UNIVERSE --
    they arrived through scripts/20260823_ingest_b0_universe.py and live under
    us_equity -- so a configuration-only lookup would raise for most of the
    book.
    """
    for group, members in UNIVERSE.items():
        if symbol in members:
            return group
    for group in GROUPS:
        if (equity_curated_root() / group / symbol).is_dir():
            return group
    raise KeyError(f"{symbol!r} is not in the ingest universe "
                   f"(trading212/ingest/yahoo_bars.py UNIVERSE) and has no "
                   f"stored partitions under {equity_curated_root()}")


def refresh_bars(symbols: list[str], interval: str) -> dict[str, int]:
    """Re-fetch and store one interval for every symbol.

    The whole span is refetched rather than appended: adjustment is
    retroactive, so an ex-dividend date rewrites the entire daily series and
    an appended file would straddle two scales. Returns rows fetched per
    symbol; an empty fetch is logged and left to the freshness gate to
    reject, so one flaky symbol does not abort the others.

    Serialized across processes: the curated store is shared by BOTH
    environments, and the live and paper daemons reach their decision
    instant simultaneously. The blocking flock makes the second refresher
    wait for the first instead of interleaving writes and stale-sibling
    deletions in the same partition directories.
    """
    if interval not in _FETCH_SPAN:
        raise ValueError(f"unsupported interval {interval!r}, "
                         f"expected one of {sorted(_FETCH_SPAN)}")
    lookback, chunk = _FETCH_SPAN[interval]
    rows: dict[str, int] = {}
    lock_path = equity_curated_root() / ".refresh.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        return _refresh_bars_locked(symbols, interval, lookback, chunk, rows)


def _refresh_bars_locked(symbols: list[str], interval: str, lookback, chunk,
                         rows: dict[str, int]) -> dict[str, int]:
    """The refresh body, run while holding the cross-process store lock."""
    for symbol in symbols:
        frame = fetch_interval(symbol, interval, lookback, chunk)
        rows[symbol] = len(frame)
        if frame.empty:
            log.warning("[bars] %s refresh returned nothing for %s",
                        interval, symbol)
            continue
        group = group_for(symbol)
        if interval == "1d":
            write_daily(group, symbol, frame)
        else:
            write_intraday(group, symbol, interval, frame)
    log.info("[bars] refreshed %s for %d symbols: %s", interval, len(symbols), rows)
    return rows


def _exchange_tz(symbol: str) -> str:
    """IANA zone of a symbol's exchange.

    London listings and the Yahoo FX series carry London-local stamps; every
    other symbol in the A0 universe is US. Same rule as
    backtest/t212/instruments.py exchange_tz().
    """
    return _TZ_LONDON if symbol.endswith(".L") or symbol.endswith("=X") \
        else _TZ_NEW_YORK


def _symbol_dir(symbol: str, interval: str):
    for group in GROUPS:
        candidate = equity_interval_dir(group, symbol, interval)
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        f"no {interval} data for {symbol} under {equity_curated_root()} "
        f"(groups {GROUPS}); run the refresh first")


def load_frames(symbols: list[str], interval: str, start: str,
                end: str, missing: str = "raise"
                ) -> dict[str, pd.DataFrame]:
    """Read stored bars per symbol, sliced by exchange-local day.

    The window is interpreted in the exchange's local calendar, not raw UTC,
    because a London bar stamps 23:00 UTC of the previous day during BST.

    Args:
        missing: "raise" (default) keeps the original contract -- a symbol with
            no stored partition raises, because silently dropping one of A0's
            eighteen would change the slot count and every quantity with it.
            "skip" omits such a symbol from the result instead, and is used
            ONLY for the wide A1 half of a B0 universe: 1,477 of the 1,501
            stored equities have no 1h partition at all, so a name entering the
            book for the first time, or one whose short-window fetch failed,
            has an empty directory. Raising there would kill the whole session
            over a name the `thin` mechanism exists to tolerate. The caller
            compares the key sets and treats what is absent as thin.
    """
    if missing not in ("raise", "skip"):
        raise ValueError(f"missing must be 'raise' or 'skip', got {missing!r}")
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        try:
            folder = _symbol_dir(symbol, interval)
        except FileNotFoundError:
            if missing == "skip":
                continue
            raise
        parts = sorted(folder.glob("*.parquet"))
        if not parts:
            if missing == "skip":
                continue
            raise FileNotFoundError(f"{folder} holds no parquet files")
        frame = pd.concat([pd.read_parquet(p) for p in parts],
                          ignore_index=True)
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        frame = (frame.drop_duplicates(subset=["ts"], keep="last")
                 .sort_values("ts").reset_index(drop=True))
        local_day = frame["ts"].dt.tz_convert(_exchange_tz(symbol)).dt.date
        mask = (local_day >= pd.Timestamp(start).date()) & \
               (local_day <= pd.Timestamp(end).date())
        frame = frame.loc[mask].reset_index(drop=True)
        if symbol == FX_SYMBOL:
            # Yahoo stamps the FX close from a different session cut than the
            # high and low, leaving close outside [low, high] by up to ~6e-4
            # relative. Only the close is ever read, so enveloping the
            # untouched fields changes no number and only keeps the frame
            # self-consistent. Same repair as backtest/t212/data_source.py.
            frame["high"] = frame[["high", "open", "close"]].max(axis=1)
            frame["low"] = frame[["low", "open", "close"]].min(axis=1)
        out[symbol] = frame
    return out


def daily_rows(symbols: list[str], start: str,
               end: str) -> dict[str, list[tuple]]:
    """Adjusted daily rows in the shim's format, ascending.

    Each row is (iso_local_date, open, high, low, close), matching
    scripts/20260822_a0_intraday_backtest.py daily_history() exactly, so the
    live shim receives the same structure the backtest fed it.
    """
    frames = load_frames(symbols, "1d", start, end)
    out: dict[str, list[tuple]] = {}
    for symbol, frame in frames.items():
        local = frame["ts"].dt.tz_convert(_TZ_NEW_YORK).dt.date
        out[symbol] = [
            (d.isoformat(), float(o), float(h), float(l), float(c))
            for d, o, h, l, c in zip(local, frame["open"], frame["high"],
                                     frame["low"], frame["close"])]
    return out


def us_sessions(start, end) -> list:
    """US trading sessions in [start, end], from the stored SPY daily bars.

    Seam S2 of fixplans/t212/b0/00_coordination.md. A half day is one session
    like any other, which is what a1_spec.md section 5 counts and therefore
    what the rebalance rotation must count; excluding them would shift every
    rebalance after the first half day in the window.

    Read-only: it opens parquet files and nothing else, so the dashboard may
    call it inside a request.
    """
    frames = load_frames([SESSION_SYMBOL], "1d", str(start), str(end))
    frame = frames[SESSION_SYMBOL]
    days = frame["ts"].dt.tz_convert(_TZ_NEW_YORK).dt.date
    return sorted(set(days))


# ============================================================================
# [1b] Seams S3 and S4: the decision's refresh and its read-only injection
# ============================================================================

def _write_intraday_merged(group: str, symbol: str, interval: str,
                           frame: pd.DataFrame) -> None:
    """Store a SHORT intraday window without truncating the month it lands in.

    write_intraday names a file after the span it holds and deletes any
    earlier file for the same month, so handing it a seven-day frame would
    replace a full month of hourly bars with seven days of them. The months
    the new frame touches are therefore read back first and merged, newest
    row winning, before the whole month is written again.
    """
    if frame.empty:
        return
    folder = equity_interval_dir(group, symbol, interval)
    months = set(frame["ts"].dt.to_period("M"))
    parts = sorted(folder.glob("*.parquet")) if folder.is_dir() else []
    existing = []
    for path in parts:
        stored = pd.read_parquet(path)
        stored["ts"] = pd.to_datetime(stored["ts"], utc=True)
        stored = stored.loc[stored["ts"].dt.to_period("M").isin(months)]
        if not stored.empty:
            existing.append(stored)
    merged = pd.concat(existing + [frame], ignore_index=True) if existing \
        else frame
    merged = (merged.drop_duplicates(subset=["ts"], keep="last")
              .sort_values("ts").reset_index(drop=True))
    write_intraday(group, symbol, interval, merged)


def _refresh_one_short(symbol: str, interval: str, days: int) -> int:
    """Fetch a few days of one symbol's intraday bars and merge them in.

    Returns the number of rows stored; zero means the fetch came back empty
    and the caller should report the name thin rather than raise.
    """
    frame = fetch_interval(symbol, interval, days, None)
    if frame.empty:
        return 0
    lock_path = equity_curated_root() / ".refresh.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _write_intraday_merged(group_for(symbol), symbol, interval, frame)
    return len(frame)


def refresh_for_decision(params: dict, session, key: pd.Timestamp,
                         a1_symbols: list[str] | None = None) -> list[str]:
    """Seam S4: every refresh one decision needs. Returns the thin names.

    Two halves. The A0 half is the existing path, unchanged: the eighteen
    trade symbols, the state symbol and FX at 1h, and the daily series too,
    because adjustment is retroactive and an ex-dividend date rewrites the
    splice factor the shim applies to today's session.

    The A1 half is new and is where the time pressure sits. It fetches a short
    intraday window per name, under a whole-batch budget: a decision that
    walks past its submission instant is worse than a decision taken without a
    fresh price for one name, because a late market order fills at the next
    open and silently swaps the caliber. A name that times out, fails or comes
    back empty is returned in the thin list, which the B0 module reads as
    "freeze this leg" rather than "sell it".

    Only session_cycle.decide calls this. The dashboard must not: it takes the
    store lock and hits the network.
    """
    trade_symbols = list(params["trade_symbols"])
    state_symbol = params["state_symbol"]
    fx_symbol = params["fx_symbol"]
    refresh_bars(trade_symbols + [state_symbol, fx_symbol], _LIVE_INTERVAL)
    # SESSION_SYMBOL is in the daily list because the session calendar -- and
    # with it the whole A1 rotation counter -- is derived from its stored bars.
    # Left out, the list stops at the last pre-market update: measured
    # 2026-09-03, SPY's lake ended 2026-08-31 while two sessions had passed,
    # which collapsed the session list and made every session read as
    # rotation zero.
    refresh_bars(trade_symbols + [state_symbol, SESSION_SYMBOL], "1d")

    thin: list[str] = []
    extra = [s for s in (a1_symbols or []) if s not in set(trade_symbols)]
    if not extra:
        return thin
    started = time.monotonic()
    for symbol in extra:
        if time.monotonic() - started > A1_REFRESH_BUDGET_SEC:
            remaining = extra[extra.index(symbol):]
            log.warning("[bars] short-window budget %ds spent; %d A1 name(s) "
                        "left unrefreshed and reported thin: %s",
                        A1_REFRESH_BUDGET_SEC, len(remaining),
                        ", ".join(remaining[:10]))
            thin.extend(remaining)
            break
        rows = 0
        for attempt in range(1, A1_REFRESH_ATTEMPTS + 1):
            try:
                rows = _refresh_one_short(symbol, _LIVE_INTERVAL,
                                          A1_INTRADAY_DAYS)
            except Exception as exc:                # noqa: BLE001
                log.warning("[bars] %s short refresh attempt %d failed: %s",
                            symbol, attempt, exc)
                rows = 0
            if rows:
                break
            if attempt < A1_REFRESH_ATTEMPTS:
                time.sleep(A1_REFRESH_BACKOFF_SEC)
        if not rows:
            thin.append(symbol)
    if thin:
        log.warning("[bars] %d A1 name(s) without a fresh intraday window: %s",
                    len(thin), ", ".join(sorted(thin)[:10]))
    return thin


def a1_book_from_records(records_root=None) -> dict[str, float]:
    """The previous A1 book, from the newest a1_plan record.

    An empty result means no rotation has been recorded yet, which is exactly
    the first-rebalance case the buffer band already handles by taking the
    plain top twenty. Order is preserved, because the band keeps its members
    in book order.
    """
    rows = archive.read_stream(records_root, "a1_plan", limit=1)
    if not rows:
        return {}
    return {str(entry["symbol"]): float(entry.get("weight") or 0.0)
            for entry in (rows[0].get("book") or [])}


def _latest_rank_table(target, sessions_before: list):
    """(frame, session) of the newest ranking table at or before `target`.

    Walks backwards through real sessions rather than calendar days, so the
    staleness it reports is measured in the same unit the rotation counts in.
    """
    for offset, day in enumerate(reversed(sessions_before)):
        path = a1_rank_path(day)
        if path.is_file():
            return pd.read_parquet(path), day, offset
    return None, None, None


def a1_universe_for(params: dict, as_of, held=None, records_root=None) -> dict:
    """The A1 names one session may touch, without loading any daily history.

    Split out of load_b0_injection because of an ordering problem that cost
    the first rotation everything: the refresh has to know which names to
    fetch BEFORE the injection is built, and the names it must fetch include
    the ones this session is about to PICK -- not just the ones the last
    rotation chose. Building the view from the previous book alone left every
    newly selected name without a bar, priced at zero, and therefore targeted
    to zero: the first B0 night would have sold the two names A0 and A1 share
    and bought none of the eighteen new ones.

    Read-only: rank parquet and the a1_plan record, nothing else.

    Returns the previous book, the prospective pick, their union, and the
    ranking table with its staleness, so the caller refreshes and the
    injection is built from the same set.
    """
    as_of = pd.Timestamp(str(as_of)).date()
    a1_params = params.get("a1_params") or {}
    all_sessions = us_sessions("2000-01-01", str(as_of))
    before = [d for d in all_sessions if d < as_of]
    frame, rank_as_of, stale = _latest_rank_table(as_of, before)
    stale_days = None if rank_as_of is None else (as_of - rank_as_of).days
    frozen = frame is None or (
        (stale is not None and stale > RANK_STALE_FREEZE_SESSIONS)
        or (stale_days is not None and stale_days > RANK_STALE_FREEZE_DAYS))

    book = a1_book_from_records(records_root)
    pick: list[str] = []
    if frame is not None and not frozen and a1_params:
        try:
            module = load_module("a1", "0.0.1")
            pick = module.select(frame, book, a1_params)
        except Exception as exc:                   # noqa: BLE001
            # A ranking that cannot be read is a frozen leg, not a crash: the
            # A0 half of the session is still worth taking.
            log.error("[bars] could not derive the prospective A1 book: %s",
                      exc)
            frozen = True
    names = sorted(set(book) | set(pick) | set(held or []))
    return {"book": book, "pick": pick, "names": names, "rank": frame,
            "rank_as_of": rank_as_of, "rank_stale_sessions": stale,
            "rank_stale_days": stale_days, "a1_frozen": frozen,
            "sessions_all": all_sessions}


def load_b0_injection(params: dict, as_of, held=None,
                      records_root=None) -> dict:
    """Seam S3: the read-only injection B0 and the dashboard both consume.

    Strictly read-only -- no network call, no lock, no write -- because the
    dashboard calls it inside a request and a panel refresh must never be able
    to move the live book or block a decision.

    The ranking table is the previous session's by design: the whole pool
    cannot be ranked inside the decision window, so the ranking is computed
    after the previous close (decision A3). When that file is absent the most
    recent one is used instead and the gap is reported in rank_stale_sessions;
    past RANK_STALE_FREEZE_SESSIONS the A1 leg is frozen, because rotating a
    book on a ranking that old is a different strategy from the one that was
    tested.

    The previous book comes from the a1_plan record stream and never from the
    positions: a rejected order leaves the two disagreeing, and rebuilding the
    band from holdings would then quietly drop a name the plan still holds
    (decision A12).
    """
    as_of = pd.Timestamp(str(as_of)).date()
    a0_params = params.get("a0_params") or params
    a1_params = params.get("a1_params") or {}
    universe = a1_universe_for(params, as_of, held=held,
                               records_root=records_root)
    anchor = str(a1_params.get("rebalance_anchor")
                 or params.get("live_from"))
    sessions_list = [d for d in universe["sessions_all"]
                     if d >= pd.Timestamp(anchor).date()]
    if as_of not in sessions_list and (not sessions_list
                                       or as_of > sessions_list[-1]):
        # The decision runs at 15:30, INSIDE the session it trades. SPY's own
        # daily bar for that session does not exist yet at that instant, so
        # the session list built from stored bars stops at yesterday. Leaving
        # it there would make B0 read today as "not a session" and return no
        # targets, which the cycle treats as an abort -- every session, for
        # ever. The caller has already proven today is a regular session
        # against the venue calendar (session_cycle.decide refuses otherwise),
        # so today is appended rather than inferred from price data that has
        # not been published yet.
        sessions_list = list(sessions_list) + [as_of]
    if "history_start" not in params:
        raise KeyError(
            "params['history_start'] is required: the volatility gate's "
            "expanding percentile is sensitive to the daily start date, so a "
            "silent default would let the live gate differ from the "
            "configured one")
    history_start = str(params["history_start"])

    a0_symbols = list(a0_params["trade_symbols"]) + [a0_params["state_symbol"]]
    a0_rows = daily_rows(a0_symbols, history_start, str(as_of))

    frame = universe["rank"]
    if frame is None:
        log.error("[bars] no A1 ranking table at or before %s; the A1 leg "
                  "has nothing to rotate on", as_of)

    # The session list drives the rotation counter, and a session missing from
    # the middle of it shifts every index after it. The lag is measured on the
    # STORED list, before today is appended -- measuring it afterwards always
    # reads zero, because the appended day is today by construction.
    stored = universe["sessions_all"]
    newest = stored[-1] if stored else None
    lag_days = None if newest is None else (as_of - newest).days
    calendar_stale = bool(lag_days is not None
                          and lag_days > MAX_SESSION_CALENDAR_LAG_DAYS)
    if calendar_stale:
        log.error("[bars] the stored session list ends %s, %d calendar days "
                  "before %s; the rotation counter is not trustworthy",
                  newest, lag_days, as_of)

    held_names = sorted(set(held or []))
    a1_names = sorted(set(universe["names"]) - set(a0_symbols))
    view_symbols = sorted(set(a0_symbols) | set(universe["names"])
                          | {params["fx_symbol"]})
    return {
        "a0_rows": a0_rows,
        "a0_mode": "rows",
        "a1_rank": frame,
        "rank_as_of": universe["rank_as_of"],
        "rank_stale_sessions": (None if universe["rank_stale_sessions"] is None
                                else int(universe["rank_stale_sessions"])),
        "rank_stale_days": universe["rank_stale_days"],
        "a1_frozen": universe["a1_frozen"],
        "a1_book": universe["book"],
        "a1_pick": universe["pick"],
        "a1_names": a1_names,
        "sessions": sessions_list,
        "session_calendar_stale": calendar_stale,
        "session_lag_days": lag_days,
        "as_of": as_of,
        "thin": [],
        "held": held_names,
        "view_symbols": view_symbols,
    }


# ============================================================================
# [2] Cutoff view and freshness gate
# ============================================================================

def build_view(frames: dict[str, pd.DataFrame],
               cutoff_ts: pd.Timestamp) -> "LiveMarketView":
    """Build the view: every bar whose ts is at or before cutoff_ts.

    cutoff_ts is the decision bar's own timestamp, so the decision bar is
    included and everything after it -- notably any later in-progress bar the
    vendor may already publish -- is dropped. The shim then discards the
    decision bar itself, which is what makes the live information set
    identical to the backtest's at the same key.
    """
    history: dict[str, list[LiveBar]] = {}
    for symbol, frame in frames.items():
        keep = frame["ts"] <= cutoff_ts
        history[symbol] = [
            LiveBar(ts=row.ts, open=float(row.open), high=float(row.high),
                    low=float(row.low), close=float(row.close),
                    volume=float(row.volume), quote_ccy=str(row.quote_ccy))
            for row in frame.loc[keep].itertuples(index=False)]
    return LiveMarketView(history, cutoff_ts)


def assert_intraday_ready(frames: dict[str, pd.DataFrame],
                          decision_key: pd.Timestamp,
                          trade_symbols: list[str], state_symbol: str,
                          fx_symbol: str,
                          soft_symbols: list[str] | None = None) -> list[str]:
    """Refuse to decide unless every series is exactly where it must be.

    Three conditions, each a way the decision would silently diverge from the
    backtest:
      1. Every trade symbol and the state symbol has a bar AT decision_key.
         That bar is the session's own 15:30 stub; its absence means the
         session is not trading normally or the feed is behind.
      2. Each of those symbols also has the preceding bar, which carries the
         information the decision is actually computed on.
      3. The FX series has a bar at exactly decision_key minus
         FX_LAG_MINUTES. The backtest's availability rule always lands there;
         under an FX hole it would silently fall back to a much older rate
         and price the slots wrong, so the gate pins the bar instead of
         tolerating staleness.

    soft_symbols relaxes condition 2, and only condition 2, for the A1 half of
    a B0 book. Those names are re-sized every session out of whatever capital
    is left, so a missing information bar costs one name its re-size; A0's
    eighteen are the caliber the recorded numbers were measured on, and a hole
    there still stops the session.
    """
    problems: list[str] = []
    thin: list[str] = []
    prior_key = decision_key - pd.Timedelta(hours=1)
    # The state symbol and FX are never soft: the whole session's timing and
    # every price in GBP depend on them, so a hole there is not one name's
    # problem.
    soft = set(soft_symbols or []) - {state_symbol, fx_symbol}
    for symbol in list(trade_symbols) + [state_symbol] + sorted(soft):
        stamps = set(frames[symbol]["ts"])
        newest = frames[symbol]["ts"].max() if not frames[symbol].empty else None
        if decision_key not in stamps:
            if symbol == state_symbol and symbol not in soft:
                problems.append(f"{symbol}: no bar at decision key "
                                f"{decision_key} (newest {newest})")
            else:
                # One trade symbol without a bar at the key is not a reason
                # to skip the session. The backtest decides anyway and lets
                # that symbol's order queue; aborting here would cost every
                # other symbol its decision and put the live book on a path
                # the baseline never took.
                thin.append(symbol)
            continue
        if prior_key not in stamps:
            if symbol in soft:
                thin.append(symbol)
            else:
                problems.append(
                    f"{symbol}: missing the information bar {prior_key}")
    fx_stamps = set(frames[fx_symbol]["ts"])
    newest_fx = frames[fx_symbol]["ts"].max() if not frames[fx_symbol].empty else None
    fx_key = decision_key - pd.Timedelta(minutes=FX_LAG_MINUTES)
    if fx_key not in fx_stamps:
        problems.append(f"{fx_symbol}: no bar at {fx_key} "
                        f"(decision key minus {FX_LAG_MINUTES}m; newest {newest_fx})")
    fx_current = decision_key - pd.Timedelta(minutes=FX_CURRENT_LAG_MINUTES)
    if fx_current not in fx_stamps:
        problems.append(f"{fx_symbol}: no bar at {fx_current} (the in-progress "
                        f"bar; without it the strategy would size off a rate an "
                        f"hour older than the cost path uses; newest {newest_fx})")
    if problems:
        raise RuntimeError("intraday freshness gate failed: " + "; ".join(problems))
    if thin:
        log.warning("[bars] no decision-key bar for %s; deciding without a "
                    "fresh price for them", ", ".join(sorted(thin)))
    return thin


class LiveMarketView:
    """Cutoff-enforced market view; duck type of the engine's MarketView."""

    def __init__(self, history: dict[str, list[LiveBar]],
                 now_key: pd.Timestamp) -> None:
        self._history = history
        self.now = now_key

    def symbols(self) -> list[str]:
        return [s for s, bars in self._history.items() if bars]

    def bar(self, symbol: str) -> LiveBar | None:
        """Latest bar at or before the cutoff, None before the first one."""
        bars = self._history.get(symbol) or []
        return bars[-1] if bars else None

    def bars(self, symbol: str, n: int) -> list[LiveBar]:
        """Last n bars at or before the cutoff, oldest first."""
        bars = self._history.get(symbol) or []
        return bars[-n:]

    def next_bar(self, symbol: str) -> None:
        """Always None: the live view has no probe arm."""
        return None
