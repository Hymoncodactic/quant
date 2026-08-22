"""Cutoff-view and freshness-gate tests for the live market data layer."""

from __future__ import annotations

import pandas as pd
import pytest

from trading212.execution.market_data import assert_fresh, build_view


def _frame(dates_utc, ccy="USD", close=100.0):
    ts = pd.to_datetime(dates_utc, utc=True)
    return pd.DataFrame({"ts": ts, "open": close, "high": close + 1,
                         "low": close - 1, "close": close,
                         "volume": 1000.0, "quote_ccy": ccy})


DAY = pd.Timestamp("2026-08-20")


def test_view_drops_bars_after_the_decision_day():
    # A US bar stamped 2026-08-21 04:00 UTC (local date 08-21) is the NEXT
    # session and must be invisible at the 08-20 decision; keeping it would
    # be lookahead. Discriminative: with the cutoff broken the close would
    # be 999.
    frame = pd.concat([_frame(["2026-08-19 04:00", "2026-08-20 04:00"]),
                       _frame(["2026-08-21 04:00"], close=999.0)],
                      ignore_index=True)
    view = build_view({"NVDA": frame}, DAY)
    assert len(view.bars("NVDA", 10)) == 2
    assert view.bar("NVDA").close == 100.0


def test_view_keeps_fx_bar_of_previous_london_day():
    # The GBPUSD daily bar for London date 2026-08-20 is stamped 23:00 UTC
    # 2026-08-19 during BST; it must count as the 08-20 bar, and the
    # in-progress 08-21 bar (23:00 UTC 08-20) must be dropped.
    frame = _frame(["2026-08-18 23:00", "2026-08-19 23:00"], ccy="USD")
    in_progress = _frame(["2026-08-20 23:00"], close=888.0)
    view = build_view({"GBPUSD=X": pd.concat([frame, in_progress],
                                             ignore_index=True)}, DAY)
    bars = view.bars("GBPUSD=X", 10)
    assert len(bars) == 2
    assert bars[-1].close == 100.0


def test_fresh_passes_when_all_series_end_on_the_decision_day():
    frames = {"NVDA": _frame(["2026-08-19 04:00", "2026-08-20 04:00"]),
              "QQQ": _frame(["2026-08-20 04:00"]),
              "GBPUSD=X": _frame(["2026-08-19 23:00"])}
    assert_fresh(frames, DAY, exact_symbols=["NVDA", "QQQ"],
                 fx_symbol="GBPUSD=X")


def test_stale_trade_symbol_fails_the_gate():
    frames = {"NVDA": _frame(["2026-08-19 04:00"]),  # missing 08-20
              "QQQ": _frame(["2026-08-20 04:00"]),
              "GBPUSD=X": _frame(["2026-08-19 23:00"])}
    with pytest.raises(RuntimeError, match="NVDA"):
        assert_fresh(frames, DAY, exact_symbols=["NVDA", "QQQ"],
                     fx_symbol="GBPUSD=X")


def test_overrunning_bar_does_not_satisfy_freshness():
    # Only a bar dated exactly the decision day satisfies the gate; a bar
    # dated one day AFTER (in-progress session) must not stand in for it.
    frames = {"NVDA": _frame(["2026-08-19 04:00", "2026-08-21 04:00"]),
              "QQQ": _frame(["2026-08-20 04:00"]),
              "GBPUSD=X": _frame(["2026-08-19 23:00"])}
    with pytest.raises(RuntimeError, match="NVDA"):
        assert_fresh(frames, DAY, exact_symbols=["NVDA", "QQQ"],
                     fx_symbol="GBPUSD=X")


def test_ancient_fx_fails_the_gate():
    frames = {"NVDA": _frame(["2026-08-20 04:00"]),
              "QQQ": _frame(["2026-08-20 04:00"]),
              "GBPUSD=X": _frame(["2026-08-01 23:00"])}
    with pytest.raises(RuntimeError, match="GBPUSD"):
        assert_fresh(frames, DAY, exact_symbols=["NVDA", "QQQ"],
                     fx_symbol="GBPUSD=X")
