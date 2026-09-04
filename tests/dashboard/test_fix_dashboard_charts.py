"""Regression tests for the B0 dashboard charts and signal diagnostics.

Responsibility: prove that a B0 dashboard assembles the shared strategy
parameters, prices every relevant book symbol, renders A0 diagnostics from
the B0 tree, plots cumulative profit or loss net of capital flows, and keeps
the per-position marked-value chart available.

Out of scope: browser layout and venue connectivity, which are covered by
visual acceptance and read-only live checks outside pytest.

Public functions: None. Pytest collects the test functions directly.

Constants: None.

Inputs: dashboard source assets. Outputs: temporary ledger journals only.

Change log:
    2026-09-04  Created for the B0 signal and chart regression repair.
    2026-09-04  Restored the per-position marked-value chart contract.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest


def _write_journal(path: Path, rows: list[dict]) -> None:
    """Write a test-only journal under pytest's temporary directory."""
    path.write_text("".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8")


def _event(at: str, event_type: str, payload: dict) -> dict:
    """Build one minimal shadow-ledger event for a read-side test."""
    return {"ts_utc": at, "event_id": f"{event_type}|{at}",
            "event_type": event_type, "payload": payload}


def test_b0_context_uses_shared_params_and_prices_the_full_book(
        tmp_path, monkeypatch):
    """The raw B0 YAML has no trade_symbols, so direct loading prices nothing."""
    from trading212.dashboard import context

    params = {
        "a0_params": {"trade_symbols": ["AAPL", "MSFT"],
                      "state_symbol": "QQQ"},
        "a1_params": {},
        "trade_symbols": ["AAPL", "MSFT"],
        "state_symbol": "QQQ",
        "fx_symbol": "GBPUSD=X",
    }
    calls = []
    monkeypatch.setattr(context, "load_config", lambda venue, env: {
        "_env": "live",
        "execution": {"strategy": {"name": "b0", "version": "0.0.1"}},
    })
    fake_cycle = SimpleNamespace(
        assemble_params=lambda cfg: calls.append(cfg) or params)
    monkeypatch.setattr(context, "session_cycle", fake_cycle, raising=False)
    monkeypatch.setattr(context, "execution_state_dir",
                        lambda venue, env: tmp_path)
    fake_archive = SimpleNamespace(read_stream=lambda root, name, limit: [{
        "strategy_id": "b0_v0_0_1", "a1_names": ["PLTR", "AAPL"]}])
    monkeypatch.setattr(context, "archive", fake_archive, raising=False)
    monkeypatch.setattr(context, "records_dir",
                        lambda venue, env: tmp_path, raising=False)

    ctx = context.AppContext("live")
    ctx.book_state = lambda: {"positions": {"NVDA": 1.0, "AAPL": 0.5}}
    try:
        assert calls and ctx.params is params
        assert ctx.watch_symbols() == [
            "AAPL", "MSFT", "NVDA", "PLTR", "QQQ", "GBPUSD=X"]
    finally:
        ctx.close()


def test_b0_signal_view_uses_read_only_seams_and_a0_subtree(
        tmp_path, monkeypatch):
    """B0 needs the four-argument S6 contract and must never call S4."""
    from trading212.dashboard import signal_view

    as_of = (pd.Timestamp.now(tz="UTC").tz_convert("America/New_York").date()
             - timedelta(days=1))
    params = {
        "history_start": "2010-01-04",
        "decision_time_local": "15:30",
        "exchange_tz": "America/New_York",
        "trade_symbols": ["AAPL"],
        "state_symbol": "QQQ",
        "fx_symbol": "GBPUSD=X",
        "a0_params": {"trade_symbols": ["AAPL"], "state_symbol": "QQQ"},
        "a1_params": {},
    }
    portfolio = SimpleNamespace(
        cash_gbp=Decimal("100"), available_cash_gbp=Decimal("100"),
        positions={"AAPL": Decimal("1")}, pending_signed_qty={})

    class _Ledger:
        positions = {"AAPL": Decimal("1")}

        def portfolio_view(self, fee_buffer):
            assert fee_buffer == Decimal("0.005")
            return portfolio

    ctx = SimpleNamespace(
        env="live", strategy_id="b0_v0_0_1", signal_name="b0",
        signal_version="0.0.1", params=params, state_dir=tmp_path,
        cfg={"risk": {"fee_buffer": 0.005}}, ledger=lambda: _Ledger())
    injection = {"view_symbols": ["AAPL", "QQQ", "GBPUSD=X"],
                 "thin": [], "sessions": [as_of], "rank_as_of": as_of,
                 "a1_book": {}, "a1_rank": None, "a0_rows": {}}
    calls = []
    monkeypatch.setattr(signal_view.market_data, "us_sessions",
                        lambda start, end: [as_of])
    monkeypatch.setattr(signal_view.market_data, "load_b0_injection",
                        lambda *args, **kwargs: calls.append((args, kwargs))
                        or dict(injection))
    monkeypatch.setattr(signal_view.market_data, "load_frames",
                        lambda *args, **kwargs: {symbol: pd.DataFrame()
                                                for symbol in injection[
                                                    "view_symbols"]})
    monkeypatch.setattr(signal_view.market_data, "build_view",
                        lambda frames, cutoff: SimpleNamespace(now=cutoff))
    monkeypatch.setattr(signal_view.market_data, "refresh_for_decision",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("dashboard must not call S4")))

    def diagnostics(view, book, received_params, received_injection):
        assert book is portfolio and received_params is params
        assert received_injection["sessions"] == [as_of]
        return {
            "as_of": as_of.isoformat(), "strategy": "b0",
            "a0": {
                "gates": {"trend": {"ma": 100.0}, "vol": {}},
                "symbols": {"AAPL": {"trigger": 100.0,
                                      "margin_pct": -10.0, "on": False}},
            },
            "a1": {}, "allocation": {}, "attribution": {},
        }

    monkeypatch.setattr(signal_view, "load_module", lambda *args:
                        SimpleNamespace(signal_diagnostics=diagnostics))
    signal_view._cache.clear()
    out = signal_view.live_signals(ctx, {
        "AAPL": {"ok": True, "price": 110.0, "age_sec": 2},
        "QQQ": {"ok": True, "price": 105.0},
    })

    assert calls, "S3 must build the B0 injection"
    assert out["strategy"] == "b0"
    assert out["a0"]["symbols"]["AAPL"]["live_margin_pct"] == pytest.approx(10.0)
    assert out["a0"]["gates"]["trend"]["live_margin_pct"] == pytest.approx(5.0)


def test_cumulative_pnl_follows_adopted_capital_and_removes_flows(tmp_path):
    """An old retired book and a later deposit must not become trading PnL."""
    from trading212.dashboard import pnl

    _write_journal(tmp_path / "a0_v0_0_1_journal.jsonl.retired-old", [
        _event("2026-08-01T00:00:00+00:00", "INIT",
               {"allocated_cash_gbp": "500"}),
    ])
    _write_journal(tmp_path / "a0_v0_0_1_journal.jsonl.retired-current", [
        _event("2026-08-23T11:02:34+00:00", "INIT",
               {"allocated_cash_gbp": "1000"}),
        _event("2026-09-02T20:00:00+00:00", "NOTE_RECONCILE", {}),
    ])
    _write_journal(tmp_path / "b0_v0_0_1_journal.jsonl", [
        _event("2026-09-03T10:51:21+00:00", "BOOK_ADOPTED",
               {"from_strategy_id": "a0_v0_0_1"}),
        _event("2026-09-04T01:00:00+00:00", "ALLOCATION_CHANGED",
               {"delta_gbp": "100"}),
    ])
    rows = [
        {"ts": "2026-08-20T00:00:00+00:00", "equity_gbp": 900.0,
         "cash_gbp": 100.0, "holdings_gbp": 800.0},
        {"ts": "2026-09-02T23:00:00+00:00", "equity_gbp": 1010.0,
         "cash_gbp": 100.0, "holdings_gbp": 910.0},
        {"ts": "2026-09-04T02:00:00+00:00", "equity_gbp": 1120.0,
         "cash_gbp": 200.0, "holdings_gbp": 920.0},
        {"ts": "2026-09-04T02:01:00+00:00", "equity_gbp": 0.0,
         "cash_gbp": None, "holdings_gbp": 0.0},
    ]

    enriched, meta = pnl.add_cumulative_pnl(
        rows, tmp_path, "b0_v0_0_1")

    assert meta["ok"] is True and meta["net_allocated_gbp"] == 1100.0
    assert [row["cumulative_pnl_gbp"] for row in enriched] == [
        None, 10.0, 20.0, None]


def test_history_route_adds_cumulative_pnl(tmp_path, monkeypatch):
    """The chart route, not browser arithmetic, owns the PnL caliber."""
    from trading212.dashboard import api

    now = pd.Timestamp.now(tz="UTC").isoformat()
    monkeypatch.setattr(api.snapshots, "read_samples",
                        lambda *args, **kwargs: [
                            {"ts": now, "equity_gbp": 1012.5,
                             "cash_gbp": 112.5,
                             "holdings_gbp": 900.0}])
    called = []

    def add(rows, state_dir, strategy_id):
        called.append((state_dir, strategy_id))
        enriched = [{**row, "cumulative_pnl_gbp": 12.5} for row in rows]
        return enriched, {"ok": True, "net_allocated_gbp": 1000.0}

    monkeypatch.setattr(api, "pnl", SimpleNamespace(add_cumulative_pnl=add),
                        raising=False)
    ctx = SimpleNamespace(env="live", state_dir=tmp_path,
                          strategy_id="b0_v0_0_1")
    status, body = api.get_history(ctx, "1D")

    assert status == 200 and called == [(tmp_path, "b0_v0_0_1")]
    assert body["rows"][0]["cumulative_pnl_gbp"] == 12.5
    assert body["pnl"]["net_allocated_gbp"] == 1000.0


def test_cumulative_pnl_fails_closed_on_malformed_capital_history(tmp_path):
    """Malformed ledger evidence must blank PnL instead of guessing or 500."""
    from trading212.dashboard import pnl

    _write_journal(tmp_path / "b0_v0_0_1_journal.jsonl", [
        {"event_type": "INIT", "payload": {"allocated_cash_gbp": "1000"}},
    ])
    rows = [{"ts": "2026-09-04T02:00:00+00:00", "equity_gbp": 1010.0,
             "cash_gbp": 110.0, "holdings_gbp": 900.0}]

    enriched, meta = pnl.add_cumulative_pnl(
        rows, tmp_path, "b0_v0_0_1")

    assert meta["ok"] is False
    assert enriched[0]["cumulative_pnl_gbp"] is None


def test_assets_keep_pnl_and_positions_but_remove_holdings_history():
    """The page must restore per-position value without the old history chart."""
    root = Path(__file__).resolve().parents[2] / "trading212" / "dashboard" / "assets"
    index = (root / "index.html").read_text(encoding="utf-8")
    app = (root / "app.js").read_text(encoding="utf-8")
    labels = json.loads((root / "labels.json").read_text(encoding="utf-8"))

    assert 'id="pnl-chart"' in index
    assert 'id="equity-chart"' not in index
    assert 'id="positions-chart"' in index
    assert 'id="positions-empty"' in index
    assert "function drawPositions" in app
    assert "paintTable(); drawPositions();" in app
    assert "cumulative_pnl_gbp" in app
    assert "live.a0 || live" in app
    assert "A0 \u4ea4\u6613\u770b\u677f" not in index
    assert labels["app"]["title"] == "\u4ea4\u6613\u770b\u677f"
    assert labels["charts"]["pnl_title"] == "\u7d2f\u8ba1\u635f\u76ca"
    assert labels["charts"].get("equity_title") != (
        "\u6301\u4ed3\u5e02\u503c\u53d8\u5316")
    assert labels["charts"]["positions_title"] == (
        "\u5404\u53ea\u80a1\u7968\u7684\u6301\u4ed3\u5e02\u503c")
