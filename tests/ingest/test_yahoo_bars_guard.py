"""The half-formed daily bar guard in trading212/ingest/yahoo_bars.py."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from trading212.ingest import yahoo_bars as yb


@pytest.fixture()
def raw():
    """A yfinance-shaped frame with two exchange-local daily bars."""
    index = pd.DatetimeIndex([
        pd.Timestamp("2026-09-01", tz="America/New_York"),
        pd.Timestamp("2026-09-02", tz="America/New_York")])
    return pd.DataFrame({"Open": [1.0, 2.0], "High": [1.5, 2.5],
                         "Low": [0.5, 1.5], "Close": [1.2, 2.2],
                         "Volume": [10.0, 20.0]}, index=index)


def test_todays_forming_bar_is_dropped(raw, monkeypatch):
    """Catches: storing a session in progress as if it had closed."""
    monkeypatch.setattr(yb, "quote_currency", lambda ticker: "USD")
    kept = yb._tidy(raw, "AAPL")
    assert len(kept) == 2
    guarded = yb._tidy(raw, "AAPL", drop_from=date(2026, 9, 2))
    assert len(guarded) == 1
    assert guarded["ts"].dt.tz_convert("America/New_York").dt.date.tolist() \
        == [date(2026, 9, 1)]


def test_the_guard_reads_the_exchange_local_date_not_utc(raw, monkeypatch):
    """A NY bar stamps 04:00 UTC; a UTC reading would shift the cut by a day."""
    monkeypatch.setattr(yb, "quote_currency", lambda ticker: "USD")
    guarded = yb._tidy(raw, "AAPL", drop_from=date(2026, 9, 1))
    assert guarded.empty


def test_a_london_listing_is_cut_on_london_dates(monkeypatch):
    monkeypatch.setattr(yb, "quote_currency", lambda ticker: "GBP")
    assert yb.exchange_tz("VOD.L") == "Europe/London"
    assert yb.exchange_tz("GBPUSD=X") == "Europe/London"
    assert yb.exchange_tz("AAPL") == "America/New_York"
