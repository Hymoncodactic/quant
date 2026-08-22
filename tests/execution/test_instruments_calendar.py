"""Calendar and mapping tests for the instruments module."""

from __future__ import annotations

import pandas as pd
import pytest

from trading212.execution.instruments import (last_completed_trading_day,
                                              market_is_open, next_event,
                                              order_ticker, session_events,
                                              validate_mapping)

# Synthetic schedule mirroring the REAL shape of the venue's US schedules
# (verified 2026-08-21 on schedule 71: no plain CLOSE events; the regular
# close is marked by AFTER_HOURS_OPEN, the weekend by AFTER_HOURS_CLOSE).
# Thu 08-20 and Fri 08-21 trade 13:30-20:00Z, then Mon 08-24.
CAL = [{"id": 53, "name": "NASDAQ", "workingSchedules": [
    {"id": 71, "timeEvents": [
        {"date": "2026-08-20T00:00:00Z", "type": "OVERNIGHT_OPEN"},
        {"date": "2026-08-20T08:00:00Z", "type": "PRE_MARKET_OPEN"},
        {"date": "2026-08-20T13:30:00Z", "type": "OPEN"},
        {"date": "2026-08-20T20:00:00Z", "type": "AFTER_HOURS_OPEN"},
        {"date": "2026-08-21T00:00:00Z", "type": "OVERNIGHT_OPEN"},
        {"date": "2026-08-21T08:00:00Z", "type": "PRE_MARKET_OPEN"},
        {"date": "2026-08-21T13:30:00Z", "type": "OPEN"},
        {"date": "2026-08-21T20:00:00Z", "type": "AFTER_HOURS_OPEN"},
        {"date": "2026-08-22T00:00:00Z", "type": "AFTER_HOURS_CLOSE"},
        {"date": "2026-08-24T00:00:00Z", "type": "OVERNIGHT_OPEN"},
        {"date": "2026-08-24T08:00:00Z", "type": "PRE_MARKET_OPEN"},
        {"date": "2026-08-24T13:30:00Z", "type": "OPEN"},
        {"date": "2026-08-24T20:00:00Z", "type": "AFTER_HOURS_OPEN"},
    ]}]}]


def _events():
    return session_events(CAL, 71)


def test_meta_maps_to_fb_us_eq():
    # The US listing of META keeps the pre-rename ticker; guessing
    # META_US_EQ would route the order to a nonexistent instrument.
    assert order_ticker("META") == "FB_US_EQ"


def test_unmapped_symbol_raises():
    with pytest.raises(KeyError):
        order_ticker("KO")


def test_last_completed_day_before_the_close_is_the_prior_day():
    now = pd.Timestamp("2026-08-21T12:00:00Z")  # Friday pre-open
    assert last_completed_trading_day(_events(), now) == pd.Timestamp("2026-08-20")


def test_last_completed_day_after_the_close_is_that_day():
    now = pd.Timestamp("2026-08-21T21:00:00Z")
    assert last_completed_trading_day(_events(), now) == pd.Timestamp("2026-08-21")


def test_weekend_still_points_at_friday():
    now = pd.Timestamp("2026-08-23T09:00:00Z")  # Sunday
    assert last_completed_trading_day(_events(), now) == pd.Timestamp("2026-08-21")


def test_market_open_inside_and_outside_the_session():
    assert market_is_open(_events(), pd.Timestamp("2026-08-21T14:00:00Z"))
    # Pre-market (12:00Z, after PRE_MARKET_OPEN) is NOT the regular session.
    assert not market_is_open(_events(), pd.Timestamp("2026-08-21T12:00:00Z"))
    # After-hours (20:30Z, after AFTER_HOURS_OPEN) is closed for A0 purposes;
    # this is the discriminative case that caught the CLOSE-event assumption.
    assert not market_is_open(_events(), pd.Timestamp("2026-08-21T20:30:00Z"))
    # Overnight session (03:00Z next day) is closed as well.
    assert not market_is_open(_events(), pd.Timestamp("2026-08-21T03:00:00Z"))


def test_next_event_finds_the_coming_open():
    ts, kind = next_event(_events(), pd.Timestamp("2026-08-21T21:00:00Z"),
                          ("OPEN",))
    assert ts == pd.Timestamp("2026-08-24T13:30:00Z")
    assert kind == "OPEN"


def test_next_event_beyond_the_window_raises():
    with pytest.raises(RuntimeError):
        next_event(_events(), pd.Timestamp("2026-09-30T00:00:00Z"), ("OPEN",))


class FakeClient:
    def __init__(self, instruments):
        self._instruments = instruments

    def instruments(self):
        return self._instruments


def test_validate_mapping_rejects_wrong_listing():
    # A EUR ETF under the same short name must not validate as the US stock.
    fake = FakeClient([{"ticker": "FB_US_EQ", "currencyCode": "EUR",
                        "type": "ETF"}])
    with pytest.raises(RuntimeError, match="META"):
        validate_mapping(fake, ["META"])


def test_validate_mapping_accepts_the_verified_listing():
    fake = FakeClient([{"ticker": "FB_US_EQ", "currencyCode": "USD",
                        "type": "STOCK", "workingScheduleId": 71}])
    meta = validate_mapping(fake, ["META"])
    assert meta["META"]["workingScheduleId"] == 71
