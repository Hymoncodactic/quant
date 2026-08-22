"""Cutoff view and hourly freshness gate.

Responsibility: prove on synthetic hourly bars that the live view hides
everything after the decision key and that the freshness gate refuses every
way the information set could silently differ from the backtest's.

Out of scope: the real-data equivalence proof, which lives in
tests/execution/test_backtest_equivalence.py.

Public functions: None. Pytest collects the test functions directly.

Constants:
    KEY  pd.Timestamp  A synthetic 15:30 New York decision key in UTC.

Inputs: None.
Outputs: None.

Change log:
    2026-08-21  Created for the daily cycle.
    2026-08-22  Rewritten for the hourly arm: hourly grids, the decision-key
                cutoff, and the FX lag assertion that replaced the old
                five-day daily tolerance.
"""

from __future__ import annotations

import pandas as pd
import pytest

from trading212.execution.market_data import (FX_CURRENT_LAG_MINUTES,
                                              FX_LAG_MINUTES,
                                              assert_intraday_ready,
                                              build_view)

NY = "America/New_York"
KEY = (pd.Timestamp("2026-08-20", tz=NY)
       + pd.Timedelta(hours=15, minutes=30)).tz_convert("UTC")


def _equity_frame(stamps, close=100.0):
    ts = pd.DatetimeIndex(stamps)
    return pd.DataFrame({"ts": ts, "open": close, "high": close + 1,
                         "low": close - 1, "close": close, "volume": 1e6,
                         "quote_ccy": "USD"})


def _session_stamps(key, count=7):
    """The session's hourly grid ending at the decision key."""
    return [key - pd.Timedelta(hours=h) for h in range(count - 1, -1, -1)]


def _fx_frame(key):
    """FX bars on the hour.

    Two of them matter: the one in force, starting FX_LAG_MINUTES before the
    key, and the in-progress one starting FX_CURRENT_LAG_MINUTES before it.
    The strategy reaches the first only by taking the last-but-one bar, so it
    lands correctly only while the second is present.
    """
    base = key - pd.Timedelta(minutes=FX_LAG_MINUTES)
    stamps = [base - pd.Timedelta(hours=h) for h in range(5, -1, -1)]
    stamps.append(key - pd.Timedelta(minutes=FX_CURRENT_LAG_MINUTES))
    return _equity_frame(sorted(stamps), close=1.25)


def _frames(key=KEY):
    return {"NVDA": _equity_frame(_session_stamps(key)),
            "QQQ": _equity_frame(_session_stamps(key)),
            "GBPUSD=X": _fx_frame(key)}


def test_gate_passes_on_a_well_formed_session():
    assert assert_intraday_ready(_frames(), KEY, ["NVDA"], "QQQ",
                                 "GBPUSD=X") == []


def test_one_thin_trade_symbol_does_not_stop_the_session():
    """The backtest decides anyway and lets that symbol's order queue, so
    aborting here would cost every other symbol its decision."""
    frames = _frames()
    frames["NVDA"] = frames["NVDA"][frames["NVDA"]["ts"] != KEY].reset_index(drop=True)
    assert assert_intraday_ready(frames, KEY, ["NVDA"], "QQQ",
                                 "GBPUSD=X") == ["NVDA"]


def test_missing_state_symbol_bar_is_refused():
    """The state symbol gates the whole decision; without it there is none."""
    frames = _frames()
    frames["QQQ"] = frames["QQQ"][frames["QQQ"]["ts"] != KEY].reset_index(drop=True)
    with pytest.raises(RuntimeError, match="QQQ"):
        assert_intraday_ready(frames, KEY, ["NVDA"], "QQQ", "GBPUSD=X")


def test_missing_in_progress_fx_bar_is_refused():
    """Without it the strategy silently sizes off a rate an hour older than
    the cost path uses, and the pinned bar alone would not catch it."""
    frames = _frames()
    current = KEY - pd.Timedelta(minutes=FX_CURRENT_LAG_MINUTES)
    frames["GBPUSD=X"] = frames["GBPUSD=X"][
        frames["GBPUSD=X"]["ts"] != current].reset_index(drop=True)
    # Discriminative: the pinned key-90m bar is still there, so a gate that
    # only checked that one would have passed.
    assert (KEY - pd.Timedelta(minutes=FX_LAG_MINUTES)) in set(frames["GBPUSD=X"]["ts"])
    with pytest.raises(RuntimeError, match="in-progress"):
        assert_intraday_ready(frames, KEY, ["NVDA"], "QQQ", "GBPUSD=X")


def test_missing_information_bar_is_refused():
    """The 14:30 bar carries what the decision is computed on."""
    frames = _frames()
    prior = KEY - pd.Timedelta(hours=1)
    frames["QQQ"] = frames["QQQ"][frames["QQQ"]["ts"] != prior].reset_index(drop=True)
    with pytest.raises(RuntimeError, match="QQQ"):
        assert_intraday_ready(frames, KEY, ["NVDA"], "QQQ", "GBPUSD=X")


def test_fx_hole_is_refused_rather_than_tolerated():
    """An older FX bar must not silently stand in for the missing one."""
    frames = _frames()
    fx_key = KEY - pd.Timedelta(minutes=FX_LAG_MINUTES)
    frames["GBPUSD=X"] = frames["GBPUSD=X"][
        frames["GBPUSD=X"]["ts"] != fx_key].reset_index(drop=True)
    # Discriminative: plenty of earlier FX bars remain, so a "latest
    # available" rule would have passed here.
    assert not frames["GBPUSD=X"].empty
    with pytest.raises(RuntimeError, match="GBPUSD"):
        assert_intraday_ready(frames, KEY, ["NVDA"], "QQQ", "GBPUSD=X")


def test_view_cuts_off_at_the_decision_key():
    """Bars after the key, including any in-progress one, stay invisible."""
    frames = _frames()
    later = _equity_frame([KEY + pd.Timedelta(hours=1)], close=999.0)
    frames["NVDA"] = pd.concat([frames["NVDA"], later], ignore_index=True)
    view = build_view(frames, KEY)
    bars = view.bars("NVDA", 100)
    assert bars[-1].ts == KEY
    assert all(b.ts <= KEY for b in bars)
    assert all(b.close != 999.0 for b in bars)


def test_view_keeps_the_decision_bar_for_the_shim_to_drop():
    """The shim needs the decision bar present to prove the session trades."""
    view = build_view(_frames(), KEY)
    assert view.bar("QQQ").ts == KEY
    assert view.now == KEY
    assert view.next_bar("QQQ") is None
