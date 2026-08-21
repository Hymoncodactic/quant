"""Feed tests: quality gate, BST daily alignment, calendar asymmetry, FX
availability. Discrimination design: fixplans/validation/02_test_plan.md
U9, U10, R1-shape, and the FX no-lookahead rule."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backtest.engine.feed import BarFeed, FxSeries, validate_frame
from backtest.t212.instruments import exchange_tz
from tests.backtest.conftest import bar_frame, daily_ts, fx_frame


def _ldn(d: str) -> pd.Timestamp:
    return daily_ts(d, "Europe/London")


def _nyc(d: str) -> pd.Timestamp:
    return daily_ts(d, "America/New_York")


# ---------------------------------------------------------------------------
# Quality gate (R1 shape: the gate must fail on a corrupted row)
# ---------------------------------------------------------------------------

def test_gate_accepts_clean_frame():
    frame = bar_frame([(_ldn("2026-06-29"), 10, 11, 9, 10.5, 100)], "GBp")
    validate_frame(frame, "X")


@pytest.mark.parametrize("mutation, message", [
    ({"high": 10.4}, "OHLC"),            # high < close
    ({"low": 10.6}, "OHLC"),             # low > open is fine; low > min(o,c)? o=10 -> violated
    ({"open": -1.0, "high": -1.0, "low": -1.0, "close": -1.0}, "non-positive"),
    ({"volume": -5.0}, "negative volume"),
    ({"quote_ccy": "EUR"}, "quote_ccy"),
])
def test_gate_rejects_each_corruption(mutation, message):
    frame = bar_frame([(_ldn("2026-06-29"), 10, 11, 9, 10.5, 100)], "GBp")
    for column, value in mutation.items():
        frame.loc[0, column] = value
    with pytest.raises(ValueError, match=message):
        validate_frame(frame, "X")


def test_gate_rejects_duplicate_ts():
    ts = _ldn("2026-06-29")
    frame = bar_frame([(ts, 10, 11, 9, 10.5, 100),
                       (ts, 10, 11, 9, 10.5, 100)], "GBp")
    with pytest.raises(ValueError, match="monoton|increasing"):
        validate_frame(frame, "X")


# ---------------------------------------------------------------------------
# U9: BST daily alignment. The London bar for trading day 2026-06-29 carries
# raw UTC ts 2026-06-28 23:00; UTC-date alignment would split the two symbols
# onto different days -- the assertion below fails on that implementation.
# ---------------------------------------------------------------------------

def test_daily_alignment_across_bst_boundary():
    ldn = bar_frame([(_ldn("2026-06-29"), 10, 11, 9, 10, 100)], "GBp")
    nyc = bar_frame([(_nyc("2026-06-29"), 20, 21, 19, 20, 100)], "USD")
    assert ldn["ts"].iloc[0].hour == 23 and ldn["ts"].iloc[0].day == 28
    feed = BarFeed({"SGLN.L": ldn, "AAPL": nyc}, exchange_tz, daily=True)
    steps = list(feed)
    assert len(steps) == 1
    _, key, bars = steps[0]
    assert key == pd.Timestamp("2026-06-29")
    assert set(bars) == {"SGLN.L", "AAPL"}


# ---------------------------------------------------------------------------
# U10: calendar asymmetry -- 2026-07-03 shape (US closed, LSE open) must keep
# the timeline key with only the London bar, never drop or shift it.
# ---------------------------------------------------------------------------

def test_calendar_asymmetry_keeps_partial_days():
    ldn_days = ["2026-07-02", "2026-07-03", "2026-07-06"]
    nyc_days = ["2026-07-02", "2026-07-06"]
    ldn = bar_frame([(_ldn(d), 10, 11, 9, 10, 100) for d in ldn_days], "GBp")
    nyc = bar_frame([(_nyc(d), 20, 21, 19, 20, 100) for d in nyc_days], "USD")
    feed = BarFeed({"SGLN.L": ldn, "AAPL": nyc}, exchange_tz, daily=True)
    seen = {str(key.date()): set(bars) for _, key, bars in feed}
    assert seen["2026-07-03"] == {"SGLN.L"}
    assert seen["2026-07-02"] == {"SGLN.L", "AAPL"}
    assert seen["2026-07-06"] == {"SGLN.L", "AAPL"}


# ---------------------------------------------------------------------------
# FX availability: a fill at day t's open may only see day t-1's close.
# Discrimination: the two closes differ, so using day t's own close (the
# lookahead bug) yields a different number.
# ---------------------------------------------------------------------------

def test_fx_close_only_available_after_bar_closes():
    fx = FxSeries(fx_frame(["2026-01-05", "2026-01-06"], [1.20, 1.30]), 86400)
    query_day6 = pd.Timestamp("2026-01-06").tz_localize("UTC")
    assert fx.rate_at(query_day6) == Decimal("1.2")
    query_day7 = pd.Timestamp("2026-01-07").tz_localize("UTC")
    assert fx.rate_at(query_day7) == Decimal("1.3")


def test_fx_refuses_to_extrapolate_backwards():
    fx = FxSeries(fx_frame(["2026-01-05"], [1.25]), 86400)
    with pytest.raises(ValueError, match="no FX rate"):
        fx.rate_at(pd.Timestamp("2026-01-04").tz_localize("UTC"))
