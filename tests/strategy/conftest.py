"""Shared fixtures for the strategy-layer tests: synthetic panels and views.

Responsibility: hold one copy of the fake market view, fake portfolio and
panel builders that the A1 and B0 test modules share, so every test starts
from the same bar shape and the same date convention. Everything is built in
process; nothing here reads data/.

Out of scope: assertions, which belong to the test modules; the code under
test, which lives in trading212/strategy/.

Public functions:
    make_panel(symbols, days, closes, volumes)  Close and volume panels.
    ramp(start, step, n)                        A simple ascending series.

Public classes:
    FakeBar        Duck type of the engine's Bar (close is what matters).
    FakeView       Duck type of MarketView over a per-symbol bar list.
    FakePortfolio  Frozen dataclass matching engine.PortfolioView's fields.

Change log:
    2026-09-03  Created with the A1 and B0 modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

NY = "America/New_York"


@dataclass(frozen=True)
class FakeBar:
    """One bar; only ts and close are read by the strategies under test."""
    ts: pd.Timestamp
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    quote_ccy: str = "USD"


@dataclass(frozen=True)
class FakePortfolio:
    """Same fields as backtest/engine/engine.py PortfolioView."""
    cash_gbp: Decimal = Decimal("0")
    available_cash_gbp: Decimal = Decimal("0")
    positions: dict = field(default_factory=dict)
    pending_signed_qty: dict = field(default_factory=dict)


class FakeView:
    """Market view over a symbol -> [FakeBar] mapping, cut at `now`."""

    def __init__(self, history: dict, now) -> None:
        self._history = {s: list(b) for s, b in history.items()}
        self.now = pd.Timestamp(now, tz="UTC") if not isinstance(
            now, pd.Timestamp) else now

    def symbols(self) -> list:
        return [s for s, bars in self._history.items() if bars]

    def bar(self, symbol: str):
        bars = self._history.get(symbol) or []
        return bars[-1] if bars else None

    def bars(self, symbol: str, n: int) -> list:
        return (self._history.get(symbol) or [])[-n:]


def ny_ts(day) -> pd.Timestamp:
    """Exchange-local midnight of a date, expressed in UTC.

    The real daily partitions stamp bars this way, and the strategies read the
    exchange-local date off the timestamp. Using UTC midnight instead would
    put every bar on the previous NY day and silently shift every calendar
    test by one session.
    """
    return pd.Timestamp(str(day), tz=NY).tz_convert("UTC")


def sessions(start: str, count: int) -> list:
    """`count` consecutive weekday dates from start, as plain dates."""
    day = date.fromisoformat(start)
    out = []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def make_panel(symbols: list, days: list, closes: dict,
               volumes: dict | None = None):
    """(closes, volumes) panels indexed by date, columns in `symbols` order."""
    close_frame = pd.DataFrame({s: closes[s] for s in symbols}, index=days)
    if volumes is None:
        volume_frame = pd.DataFrame(
            {s: [1_000_000.0] * len(days) for s in symbols}, index=days)
    else:
        volume_frame = pd.DataFrame({s: volumes[s] for s in symbols},
                                    index=days)
    return close_frame, volume_frame


def ramp(start: float, step: float, n: int) -> list:
    """An ascending price series, the simplest thing with a positive score."""
    return [start + step * i for i in range(n)]
