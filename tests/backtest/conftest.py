"""Shared fixtures: synthetic frames and pre-wired broker components.

Every test builds its own data in-process; no test touches data/ (the real
data checks live in scripts/, see fixplans/validation/02_test_plan.md
section 2).
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


def daily_rows(dates: list[str], tz: str, ohlcv: list[tuple]) -> list[tuple]:
    """Zip exchange-local dates with (o, h, l, c, v) tuples."""
    return [(daily_ts(d, tz), *bar) for d, bar in zip(dates, ohlcv)]


def flat_bar(price: float, volume: float = 1e9) -> tuple:
    return (price, price, price, price, volume)


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
