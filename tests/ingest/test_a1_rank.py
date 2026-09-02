"""The pre-market A1 ranking pass: causality, E5, coverage, single source."""

from __future__ import annotations

import pandas as pd
import pytest

from tests.strategy.conftest import make_panel, ramp, sessions
from trading212.ingest import a1_rank


SYMBOLS = ["AAA", "BBB", "CCC"]


def _panel(n_days: int = 400):
    days = sessions("2025-01-01", n_days)
    closes = {s: ramp(100.0, step, n_days)
              for s, step in zip(SYMBOLS, (1.0, 0.5, 0.2))}
    return days, make_panel(SYMBOLS, days, closes)


def _params():
    params = a1_rank._params()
    params["liq_window"] = 252
    return params


def test_the_pass_delegates_to_the_strategy_module(monkeypatch):
    """Catches: a second copy of the admission rules in the ingest layer."""
    days, (closes, volumes) = _panel()
    seen = {}

    def spy(c, v, as_of, params):
        seen["as_of"] = as_of
        seen["require"] = params.get("require_verified_ticker")
        return pd.DataFrame({"symbol": SYMBOLS, "ticker": [None] * 3,
                             "close": [1.0] * 3, "score": [0.1] * 3,
                             "eligible": [True] * 3,
                             "elig_reason": ["ok"] * 3, "rank": [1, 2, 3]})

    monkeypatch.setattr(a1_rank._a1, "rank_table", spy)
    monkeypatch.setattr(a1_rank, "_universe_members", lambda params: SYMBOLS)
    monkeypatch.setattr(a1_rank, "load_panels",
                        lambda members: (closes, volumes, []))
    monkeypatch.setattr(a1_rank, "verified_tickers", lambda symbols: {})
    frame = a1_rank.build_table(days[-1], _params())
    assert seen["as_of"] == days[-1]
    assert list(frame.columns) == list(a1_rank.RANK_COLUMNS)
    assert set(frame["panel_as_of"]) == {days[-1].isoformat()}
    assert set(frame["code_version"]) == {"a1_v0_0_1"}


def test_a_session_the_pool_barely_covers_is_refused(monkeypatch):
    """Catches: ranking a half-updated lake and admitting 15 of 1,498 names."""
    days, (closes, volumes) = _panel()
    holed = closes.copy()
    holed.loc[holed.index[-1], ["BBB", "CCC"]] = float("nan")
    monkeypatch.setattr(a1_rank, "_universe_members", lambda params: SYMBOLS)
    monkeypatch.setattr(a1_rank, "load_panels",
                        lambda members: (holed, volumes, []))
    monkeypatch.setattr(a1_rank, "verified_tickers", lambda symbols: {})
    with pytest.raises(ValueError, match="has not finished this session"):
        a1_rank.build_table(days[-1], _params())


def test_the_default_session_is_the_newest_covered_one():
    days, (closes, _volumes) = _panel()
    holed = closes.copy()
    holed.loc[holed.index[-1], ["BBB", "CCC"]] = float("nan")
    assert a1_rank.latest_complete_session(holed) == days[-2]


def test_no_covered_session_is_an_error():
    days, (closes, _volumes) = _panel(3)
    empty = closes.copy()
    empty.loc[:, :] = float("nan")
    with pytest.raises(ValueError, match="coverage"):
        a1_rank.latest_complete_session(empty)


def test_e5_and_causality_hold_through_the_pass(monkeypatch):
    """The ranking is cut at the session and an unmapped name is not admitted."""
    days, (closes, volumes) = _panel()
    monkeypatch.setattr(a1_rank, "_universe_members", lambda params: SYMBOLS)
    monkeypatch.setattr(a1_rank, "load_panels",
                        lambda members: (closes, volumes, []))
    monkeypatch.setattr(a1_rank, "verified_tickers",
                        lambda symbols: {"AAA": "AAA_US_EQ",
                                         "CCC": "CCC_US_EQ"})
    params = dict(_params(), require_verified_ticker=True, mom_long=252,
                  mom_skip=21, min_history_bars=1,
                  min_dollar_volume_usd=1.0, max_zero_volume_share=0.99,
                  order_usd_for_participation=1e-6)
    frame = a1_rank.build_table(days[300], params).set_index("symbol")
    assert frame.loc["BBB", "elig_reason"] == "no_ticker"
    assert not frame.loc["BBB", "eligible"]
    assert frame.loc["AAA", "rank"] == 1
    assert set(frame["panel_as_of"]) == {days[300].isoformat()}


def test_the_written_table_round_trips(tmp_path, monkeypatch):
    days, (closes, volumes) = _panel()
    monkeypatch.setattr(a1_rank, "_universe_members", lambda params: SYMBOLS)
    monkeypatch.setattr(a1_rank, "load_panels",
                        lambda members: (closes, volumes, []))
    monkeypatch.setattr(a1_rank, "verified_tickers", lambda symbols: {})
    monkeypatch.setattr(a1_rank, "a1_rank_path",
                        lambda session: tmp_path / f"{session}.parquet")
    frame = a1_rank.build_table(days[300], _params())
    path = a1_rank.write_table(days[300], frame)
    assert path.is_file()
    assert list(pd.read_parquet(path).columns) == list(a1_rank.RANK_COLUMNS)
