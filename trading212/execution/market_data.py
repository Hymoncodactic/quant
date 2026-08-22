"""Daily-bar inputs for the live A0 cycle: refresh, load, cutoff view.

Responsibility: refresh the local curated daily bars for the strategy's
symbols (through trading212/ingest/yahoo_bars.py, the single fetch/store
implementation), load their history, enforce the freshness gate, and expose
a cutoff-safe view satisfying the strategy's MarketView duck type.
Not responsible for: fetching mechanics (yahoo_bars.py) or the decision on
WHEN the cycle runs (daily_cycle.py).

View contract: the strategy (trading212/strategy/a0_v0_0_1.py) reads only
view.now, view.bar(symbol) and view.bars(symbol, n), and bar objects expose
ts/open/high/low/close/volume/quote_ccy. LiveBar and LiveMarketView mirror
backtest/engine/{types.Bar, feed.MarketView} deliberately WITHOUT importing
them: the execution layer must not depend on backtest/ (ARCHITECTURE.md
section 2 keeps backtest importable without any trading code and vice
versa). Field names are kept identical so the same strategy module runs on
both.

Timing contract (mirrors the backtest's daily semantics): a decision for
trading day D+1's open uses bars whose exchange-local date is <= D, where D
is the last completed US trading day. The FX daily bar dated D completes at
midnight Europe/London after the US close, so the decide phase must run
after London midnight and before the next US open; daily_cycle enforces the
window, this module enforces the bar-level cutoff and freshness.

Public functions:
    group_for(symbol)                 Universe group of one symbol
    refresh_daily_bars(symbols)       Re-fetch and store full daily history
    load_daily_history(symbols, start)  {symbol: DataFrame} from the local store
    build_view(frames, decision_day)  Cutoff LiveMarketView
    assert_fresh(frames, expected_day, exact_symbols, fx_symbol)  Freshness gate

Public classes:
    LiveBar, LiveMarketView
"""

from __future__ import annotations

__all__ = ["group_for", "refresh_daily_bars", "load_daily_history",
           "build_view", "assert_fresh", "LiveBar", "LiveMarketView"]

from dataclasses import dataclass

import pandas as pd

from common.logging_setup import get_logger
from common.paths import equity_interval_dir
from trading212.ingest.yahoo_bars import UNIVERSE, fetch_interval, write_daily

log = get_logger("t212.execution")

# ============================================================================
# [1] Constants
# ============================================================================

# Exchange time zone per symbol family, for converting a bar's UTC timestamp
# to its exchange-local date. Same rule as backtest/t212/instruments.py
# exchange_tz(): London listings and Yahoo FX series carry Europe/London
# local-midnight daily stamps (docs/data/t212/DATA_SPEC.md section 3,
# verified 2026-08-20); everything else in the A0 universe is US.
_TZ_LONDON = "Europe/London"
_TZ_NEW_YORK = "America/New_York"

# A completed FX bar must exist within this many calendar days of the
# decision day; GBPUSD=X trades continuously, so a wider gap means the feed
# is broken, not that markets were shut.
_FX_MAX_AGE_DAYS = 5


@dataclass(frozen=True)
class LiveBar:
    """One OHLCV bar; field-compatible with backtest/engine/types.Bar."""
    ts: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_ccy: str


# ============================================================================
# [2] Refresh and load
# ============================================================================

def group_for(symbol: str) -> str:
    """Return the universe group holding one symbol (raises when unknown)."""
    for group, members in UNIVERSE.items():
        if symbol in members:
            return group
    raise KeyError(f"{symbol!r} is not in the ingest universe "
                   f"(trading212/ingest/yahoo_bars.UNIVERSE)")


def refresh_daily_bars(symbols: list[str]) -> dict[str, int]:
    """Re-fetch and store the full daily history of every symbol.

    The full window is refetched, not appended: adjusted prices are
    retroactive, so appending across a split or dividend leaves the stored
    series inconsistent (same rule as scripts/update_data.py). Returns the
    per-symbol row count fetched; a zero row count is left to the freshness
    gate to reject, so one flaky fetch does not kill the refresh of others.
    """
    rows: dict[str, int] = {}
    for symbol in symbols:
        frame = fetch_interval(symbol, "1d", None, None)
        rows[symbol] = len(frame)
        if frame.empty:
            log.warning("[bars] refresh returned nothing for %s", symbol)
            continue
        write_daily(group_for(symbol), symbol, frame)
    log.info("[bars] refreshed %d symbols: %s", len(symbols), rows)
    return rows


def load_daily_history(symbols: list[str], start: str) -> dict[str, pd.DataFrame]:
    """Load stored daily bars for every symbol from `start` (ISO date) on.

    Returns ts-sorted frames with the project bar schema. Missing symbols
    raise: the strategy's warmup logic decides what to do with a SHORT
    history, but a symbol with NO local data means the pipeline never ran.
    """
    frames: dict[str, pd.DataFrame] = {}
    start_ts = pd.Timestamp(start, tz="UTC")
    for symbol in symbols:
        folder = equity_interval_dir(group_for(symbol), symbol, "1d")
        files = sorted(folder.glob("*.parquet")) if folder.is_dir() else []
        if not files:
            raise FileNotFoundError(f"no stored daily bars for {symbol} under "
                                    f"{folder}; run the data refresh first")
        frame = pd.concat([pd.read_parquet(path) for path in files],
                          ignore_index=True)
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        frame = (frame[frame["ts"] >= start_ts]
                 .drop_duplicates(subset=["ts"], keep="last")
                 .sort_values("ts").reset_index(drop=True))
        frames[symbol] = frame
    return frames


# ============================================================================
# [3] Cutoff view and freshness gate
# ============================================================================

def _local_dates(frame: pd.DataFrame, symbol: str) -> pd.Series:
    """Exchange-local calendar date of every bar (as naive Timestamps)."""
    tz = _TZ_LONDON if symbol.endswith(".L") or symbol.endswith("=X") \
        else _TZ_NEW_YORK
    return frame["ts"].dt.tz_convert(tz).dt.normalize().dt.tz_localize(None)


def assert_fresh(frames: dict[str, pd.DataFrame], expected_day: pd.Timestamp,
                 exact_symbols: list[str], fx_symbol: str) -> None:
    """Freshness gate: refuse to decide on stale or overrunning data.

    exact_symbols (trade universe and the state symbol) must have their
    newest completed bar dated exactly expected_day -- older means the
    refresh failed or the symbol is suspended, and a suspended symbol must
    be handled explicitly, not priced at its last close (same rationale as
    the engine's stale-position guard). The FX series must have a completed
    bar within _FX_MAX_AGE_DAYS of expected_day.

    Raises:
        RuntimeError: Any symbol violates its freshness requirement.
    """
    problems: list[str] = []
    for symbol in exact_symbols:
        dates = _local_dates(frames[symbol], symbol)
        completed = dates[dates <= expected_day]
        newest = completed.max() if not completed.empty else None
        if newest is None or newest != expected_day:
            problems.append(f"{symbol}: newest completed bar {newest}, "
                            f"expected {expected_day.date()}")
    fx_dates = _local_dates(frames[fx_symbol], fx_symbol)
    fx_completed = fx_dates[fx_dates <= expected_day]
    if fx_completed.empty or \
            (expected_day - fx_completed.max()).days > _FX_MAX_AGE_DAYS:
        problems.append(f"{fx_symbol}: no completed FX bar within "
                        f"{_FX_MAX_AGE_DAYS} days of {expected_day.date()}")
    if problems:
        raise RuntimeError("freshness gate failed: " + "; ".join(problems))


def build_view(frames: dict[str, pd.DataFrame],
               decision_day: pd.Timestamp) -> "LiveMarketView":
    """Build the cutoff view: bars with exchange-local date <= decision_day.

    decision_day is the last completed US trading day (naive midnight
    Timestamp from instruments.last_completed_trading_day). Bars dated after
    it -- including any in-progress bar Yahoo returns for the current
    session -- are dropped, which is what makes the live view equivalent to
    the backtest's MarketView at the same step.
    """
    history: dict[str, list[LiveBar]] = {}
    for symbol, frame in frames.items():
        keep = _local_dates(frame, symbol) <= decision_day
        bars = [LiveBar(ts=row.ts, open=float(row.open), high=float(row.high),
                        low=float(row.low), close=float(row.close),
                        volume=float(row.volume), quote_ccy=str(row.quote_ccy))
                for row in frame[keep].itertuples(index=False)]
        history[symbol] = bars
    return LiveMarketView(history, decision_day)


class LiveMarketView:
    """Cutoff-enforced market view; duck-type of backtest MarketView."""

    def __init__(self, history: dict[str, list[LiveBar]],
                 now_key: pd.Timestamp) -> None:
        self._history = history
        self.now = now_key

    def symbols(self) -> list[str]:
        return [s for s, bars in self._history.items() if bars]

    def bar(self, symbol: str) -> LiveBar | None:
        """Latest completed bar for the symbol, None before its first bar."""
        bars = self._history.get(symbol) or []
        return bars[-1] if bars else None

    def bars(self, symbol: str, n: int) -> list[LiveBar]:
        """Last n completed bars, oldest first."""
        bars = self._history.get(symbol) or []
        return bars[-n:]
