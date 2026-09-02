"""Ticker mapping and the venue session calendar.

Responsibility: prove the mapping refuses to guess, and that sessions,
full-session detection and the 15:30 decision key are derived from the
venue's real event shape rather than from assumptions about it.

Out of scope: market data, covered by tests/execution/test_market_data_view.py.

Public functions: None. Pytest collects the test functions directly.

Constants:
    CAL  list  A synthetic calendar mirroring the real US schedule shape
               verified 2026-08-21: no CLOSE events at all, the regular close
               marked by AFTER_HOURS_OPEN, and the weekend by
               AFTER_HOURS_CLOSE.

Inputs: None.
Outputs: None.

Change log:
    2026-08-21  Created for the daily cycle.
    2026-08-22  Rewritten for the hourly arm: Session records, decision_key(),
                half-day rejection. The daily helpers this file used to cover
                were removed with the daily cycle.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from trading212.execution.instruments import (current_session, decision_key,
                                              last_full_session,
                                              market_is_open, order_ticker,
                                              session_events, session_on,
                                              sessions, validate_mapping)

# Thu 08-20 and Fri 08-21 are full sessions (09:30-16:00 NY); Mon 08-24 is a
# half day closing 13:00. All stamps UTC under EDT.
CAL = [{"id": 53, "name": "NASDAQ", "workingSchedules": [
    {"id": 71, "timeEvents": [
        {"date": "2026-08-20T08:00:00Z", "type": "PRE_MARKET_OPEN"},
        {"date": "2026-08-20T13:30:00Z", "type": "OPEN"},
        {"date": "2026-08-20T20:00:00Z", "type": "AFTER_HOURS_OPEN"},
        {"date": "2026-08-21T08:00:00Z", "type": "PRE_MARKET_OPEN"},
        {"date": "2026-08-21T13:30:00Z", "type": "OPEN"},
        {"date": "2026-08-21T20:00:00Z", "type": "AFTER_HOURS_OPEN"},
        {"date": "2026-08-22T00:00:00Z", "type": "AFTER_HOURS_CLOSE"},
        {"date": "2026-08-24T13:30:00Z", "type": "OPEN"},
        {"date": "2026-08-24T17:00:00Z", "type": "AFTER_HOURS_OPEN"},
    ]}]}]


def _sessions():
    return sessions(session_events(CAL, 71))


def test_meta_maps_to_the_us_listing_not_the_european_twin():
    assert order_ticker("META") == "FB_US_EQ"


def test_unmapped_symbol_raises_rather_than_guessing():
    # A symbol absent from BOTH tables. KO used to serve here and no longer
    # can: the wide universe map covers the whole S&P 1500 candidate pool now,
    # which is the point of it.
    with pytest.raises(KeyError):
        order_ticker("NOT_A_REAL_SYMBOL")


def test_the_wide_map_is_read_from_the_reference_file(tmp_path, monkeypatch):
    """The pool half of the mapping comes from the built file, A0's from code."""
    import json

    import common.paths
    from trading212.execution import instruments as ins

    monkeypatch.setattr(common.paths, "DIR_REFERENCE", tmp_path)
    ins._universe_cache.clear()
    (tmp_path / "t212_universe_ticker_map_20260903.json").write_text(json.dumps(
        {"map": {"ZTS": {"ticker": "ZTS_US_EQ"},
                 "AMBIG": {"ticker": None},
                 # A file entry may never override a hand-verified A0 name:
                 # META trades as FB_US_EQ and nothing derives that.
                 "META": {"ticker": "META_US_EQ"}}}))
    table = ins.universe_ticker_map()
    assert table["ZTS"] == "ZTS_US_EQ"
    assert table["META"] == "FB_US_EQ"
    assert "AMBIG" not in table
    assert ins.ticker_map_for(["ZTS", "AMBIG", "META"]) == {
        "ZTS": "ZTS_US_EQ", "META": "FB_US_EQ"}
    ins._universe_cache.clear()


def test_sessions_are_folded_from_open_to_regular_close():
    ss = _sessions()
    assert [str(s.date_ny) for s in ss] == ["2026-08-20", "2026-08-21",
                                            "2026-08-24"]
    assert ss[0].close_utc == pd.Timestamp("2026-08-20T20:00:00Z")


def test_half_day_is_detected_from_the_calendar():
    ss = _sessions()
    assert [s.is_full for s in ss] == [True, True, False]


def test_decision_key_is_local_1530_not_a_fixed_utc_time():
    ss = _sessions()
    assert decision_key(ss[0]) == pd.Timestamp("2026-08-20T19:30:00Z")


def test_decision_key_refuses_a_half_day():
    ss = _sessions()
    with pytest.raises(ValueError, match="15:30"):
        decision_key(ss[2])


def test_current_session_spans_only_regular_hours():
    ss = _sessions()
    inside = current_session(ss, pd.Timestamp("2026-08-20T19:30:00Z"))
    assert inside is not None and str(inside.date_ny) == "2026-08-20"
    # After-hours is not a session for this purpose.
    assert current_session(ss, pd.Timestamp("2026-08-20T20:30:00Z")) is None
    assert current_session(ss, pd.Timestamp("2026-08-20T12:00:00Z")) is None


def test_last_full_session_skips_the_half_day():
    ss = _sessions()
    got = last_full_session(ss, pd.Timestamp("2026-08-24T18:00:00Z"))
    assert str(got.date_ny) == "2026-08-21"


def test_session_on_looks_up_by_local_date():
    ss = _sessions()
    assert session_on(ss, dt.date(2026, 8, 21)) is not None
    assert session_on(ss, dt.date(2026, 8, 22)) is None


def test_market_is_open_excludes_pre_and_after_hours():
    ev = session_events(CAL, 71)
    assert market_is_open(ev, pd.Timestamp("2026-08-20T14:00:00Z"))
    assert not market_is_open(ev, pd.Timestamp("2026-08-20T12:00:00Z"))
    assert not market_is_open(ev, pd.Timestamp("2026-08-20T20:30:00Z"))


class _FakeClient:
    def __init__(self, instruments_):
        self._instruments = instruments_

    def instruments(self):
        return self._instruments


def test_validate_mapping_rejects_a_foreign_listing():
    fake = _FakeClient([{"ticker": "FB_US_EQ", "currencyCode": "EUR",
                         "type": "ETF"}])
    with pytest.raises(RuntimeError, match="META"):
        validate_mapping(fake, ["META"])


def test_validate_mapping_accepts_the_verified_listing():
    fake = _FakeClient([{"ticker": "FB_US_EQ", "currencyCode": "USD",
                         "type": "STOCK", "workingScheduleId": 71}])
    assert validate_mapping(fake, ["META"])["META"]["workingScheduleId"] == 71


# ============================================================================
# Universe schedule agreement (added 2026-08-29 after the pre-live probe found
# DELL/ORCL/TSM on schedule 56 while the cycle times everything off 71)
# ============================================================================

def _schedule(schedule_id, date_iso, close_hour_utc):
    return {"id": schedule_id, "name": f"X{schedule_id}", "workingSchedules": [
        {"id": schedule_id, "timeEvents": [
            {"date": f"{date_iso}T13:30:00.000Z", "type": "OPEN"},
            {"date": f"{date_iso}T{close_hour_utc}:00:00.000Z",
             "type": "AFTER_HOURS_OPEN"},
            {"date": f"{date_iso}T23:00:00.000Z", "type": "AFTER_HOURS_CLOSE"},
        ]}]}


def test_agreeing_schedules_report_no_divergence():
    from datetime import date
    from trading212.execution.instruments import schedule_divergences
    calendar = [_schedule(71, "2026-08-31", "20"),
                _schedule(56, "2026-08-31", "20")]
    assert schedule_divergences(calendar, {71, 56}, date(2026, 8, 31)) == []


def test_diverging_close_is_reported():
    """If NYSE ever closed at a different hour than NASDAQ, the three
    NYSE-listed names would be timed against the wrong close."""
    from datetime import date
    from trading212.execution.instruments import schedule_divergences
    calendar = [_schedule(71, "2026-08-31", "20"),
                _schedule(56, "2026-08-31", "18")]
    problems = schedule_divergences(calendar, {71, 56}, date(2026, 8, 31))
    assert len(problems) == 1 and "schedule 56" in problems[0]


def test_missing_session_on_one_schedule_is_reported():
    from datetime import date
    from trading212.execution.instruments import schedule_divergences
    calendar = [_schedule(71, "2026-08-31", "20"),
                _schedule(56, "2026-09-01", "20")]
    problems = schedule_divergences(calendar, {71, 56}, date(2026, 8, 31))
    assert any("no session" in p for p in problems)
