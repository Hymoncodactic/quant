"""Shared fixtures for the backtest tests: synthetic frames and pre-wired
broker components.

Responsibility: hold one copy of the data-construction and wiring helpers that
the test modules in this directory share, so that every test starts from the
same bar shape, the same timestamp convention and the same cost and fault
assumptions. Timestamps follow the real daily data, which stamps
exchange-local midnight expressed in UTC. The cost configuration used by the
helpers zeroes slippage and session widening so that expected fill prices and
fill instants can be asserted exactly rather than approximately.

Out of scope: assertions, which belong to the test modules themselves; any
reading of data/, since every test builds its data in process (the checks
against real downloaded data live in scripts/, see
fixplans/validation/02_test_plan.md section 2); the code under test, which
lives in backtest/engine/ and backtest/t212/.

Public functions:
    daily_ts(date_str, tz)
        Exchange-local midnight of a calendar date, returned in UTC.
    bar_frame(rows, quote_ccy)
        Build an OHLCV DataFrame from (ts, o, h, l, c, v) tuples and stamp one
        quote currency on every row. Price and volume columns are forced to
        float because pandas 3 refuses to place a float into a column it
        inferred as int64 from all-integer test literals.
    fx_frame(dates, rates)
        Build a GBPUSD daily frame in which each day's four prices equal the
        given rate.
    zero_spread(monkeypatch)
        Pytest fixture registering the test symbols with a zero half spread,
        so a fill price can be compared against an exact expected number.
    cost_cfg_clean()
        Cost configuration with no slippage, no session widening and the
        structural floor cooldown.
    faults_off(**overrides)
        Fault configuration with every switch off, with named fields
        overridden as requested.
    mk_broker(fx_dates, fx_rates, cost, faults, interval)
        A T212 broker simulator wired to a constant-rate FX series.
    mk_ledger(cash)
        A ledger opened with the given amount of GBP cash.

Public classes: None.

Constants: None at module level. The values that fix the behavior of the
helpers are defaults on the helpers themselves:
    zero_spread symbols
        "TEST", "USDX", "FAKE.L", "MIXA", "MIXB". Their half spread is set to
        Decimal("0") for the duration of the test.
    cost_cfg_clean
        slippage_bps 0, spread_session_multiplier 1, cooldown_bars 1. The
        cooldown value is the structural lower bound rather than a tuned
        number: one bar is the smallest gap the fill timing permits.
    mk_broker defaults
        FX dates 2026-01-01 through 2026-01-29 at a constant GBPUSD rate of
        1.25, FX bar duration 86400 seconds, interval "1d", daily mode on when
        the interval is "1d". The rate is arbitrary but constant so that
        conversion effects never move between two runs of the same test.
    mk_ledger default
        100000 GBP of opening cash, large enough that admission checks rather
        than exhausted cash decide the outcome of a test.

Inputs: None. Every frame is built in process; no path is read.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
    2026-08-22  Removed daily_rows() and flat_bar(): zero call sites in the
                repository, and neither is a pytest fixture.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backtest.engine.feed import FxSeries
from backtest.engine.ledger import Ledger
from backtest.t212 import instruments
from backtest.t212.broker_sim import T212BrokerSim
from backtest.t212.costs import CostConfig
from backtest.t212.faults import FaultConfig


def daily_ts(date_str: str, tz: str) -> pd.Timestamp:
    """Exchange-local midnight expressed in UTC, as the real daily data does."""
    return pd.Timestamp(date_str, tz=tz).tz_convert("UTC")


def bar_frame(rows: list[tuple], quote_ccy: str) -> pd.DataFrame:
    """rows: (ts, open, high, low, close, volume)."""
    frame = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close",
                                        "volume"])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    # Float columns explicitly: pandas 3 refuses to place a float into a
    # column that was inferred int64 from all-integer test literals.
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = frame[column].astype(float)
    frame["quote_ccy"] = quote_ccy
    return frame


def fx_frame(dates: list[str], rates: list[float]) -> pd.DataFrame:
    rows = [(daily_ts(d, "Europe/London"), r, r, r, r, 0.0)
            for d, r in zip(dates, rates)]
    return bar_frame(rows, "USD")


@pytest.fixture()
def zero_spread(monkeypatch):
    """Register test symbols with a zero half-spread for exact-price checks."""
    for sym in ("TEST", "USDX", "FAKE.L", "MIXA", "MIXB"):
        monkeypatch.setitem(instruments.HALF_SPREAD_BPS, sym, Decimal("0"))
    yield


def cost_cfg_clean() -> CostConfig:
    """No slippage, no session widening, structural-floor cooldown: exact
    expected fill prices and timings for unit tests."""
    return CostConfig(slippage_bps=Decimal("0"),
                      spread_session_multiplier=Decimal("1"),
                      cooldown_bars=1)


def faults_off(**overrides) -> FaultConfig:
    cfg = FaultConfig.all_off()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def mk_broker(fx_dates: list[str] | None = None, fx_rates: list[float] | None = None,
              cost: CostConfig | None = None, faults: FaultConfig | None = None,
              interval: str = "1d") -> T212BrokerSim:
    dates = fx_dates or [f"2026-01-{d:02d}" for d in range(1, 30)]
    rates = fx_rates or [1.25] * len(dates)
    fx = FxSeries(fx_frame(dates, rates), 86400)
    return T212BrokerSim(cost or cost_cfg_clean(), faults or faults_off(),
                         interval, fx, daily=(interval == "1d"))


def mk_ledger(cash: str = "100000") -> Ledger:
    return Ledger(initial_cash_gbp=Decimal(cash))
