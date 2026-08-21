"""Regression tests for the 2026-08-20 adversarial-review findings.

Each test names the finding it pins down; discrimination design notes inline.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pandas as pd
import pytest

from backtest.engine.matching import match_stop_limit
from backtest.engine.results import write_run
from backtest.engine.types import Bar, EngineConfig, OrderSpec, OrderStatus, \
    OrderType, TimeInForce
from backtest.t212.instruments import in_us_overlap
from tests.backtest.conftest import (cost_cfg_clean, daily_ts, faults_off,
                                     mk_broker, mk_ledger)

D = Decimal


def _bar(ts: pd.Timestamp, o: float, h: float, l: float, c: float,
         v: float = 1e9, ccy: str = "GBP") -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v, quote_ccy=ccy)


def _flat(ts: pd.Timestamp, px: float, v: float = 1e9) -> Bar:
    return _bar(ts, px, px, px, px, v)


# ---------------------------------------------------------------------------
# c0: mixed-exchange 1h grids interleave 30 minutes apart. An order decided on
# the :30-grid bar must NOT fill on the :00-grid bar half an interval later;
# the first legal fill bar opens a FULL interval after the decision key.
# Step-based eligibility fills at 15:00 (wrong); time-based waits for 16:00.
# ---------------------------------------------------------------------------

def test_mixed_grid_interleaved_timeline_no_lookahead(zero_spread):
    broker, ledger = mk_broker(interval="1h"), mk_ledger()
    t = lambda hhmm: pd.Timestamp(f"2026-01-05 {hhmm}", tz="UTC")
    # Step 0-1: one bar each so both symbols have market data.
    broker.process_bar(0, t("14:00"), {"MIXB": _flat(t("14:00"), 100.0)}, ledger)
    broker.process_bar(1, t("14:30"), {"MIXA": _flat(t("14:30"), 50.0)}, ledger)
    # Decision made at the 14:30 key (MIXA's bar, closes 15:30 real time).
    order = broker.submit(OrderSpec("MIXB", D("1")), t("14:30"), 1, ledger)
    assert order.status is OrderStatus.NEW
    fills_1500 = broker.process_bar(2, t("15:00"),
                                    {"MIXB": _flat(t("15:00"), 101.0)}, ledger)
    assert fills_1500 == []          # 30 minutes early: information not final
    fills_1530 = broker.process_bar(3, t("15:30"),
                                    {"MIXA": _flat(t("15:30"), 51.0)}, ledger)
    assert fills_1530 == []          # MIXB has no bar here
    fills_1600 = broker.process_bar(4, t("16:00"),
                                    {"MIXB": _flat(t("16:00"), 102.0)}, ledger)
    assert len(fills_1600) == 1
    assert fills_1600[0].ts == t("16:00")
    assert fills_1600[0].price == D("102.0")


# ---------------------------------------------------------------------------
# c13: a STOP's trigger is one-way. After a volume-capped partial fill on the
# trigger bar, the remainder must fill as a market leg on the next bar even
# though the next bar never touches the stop again. The old re-evaluation
# returns None there, so the expectations discriminate.
# ---------------------------------------------------------------------------

def test_stop_trigger_persists_after_partial_fill(zero_spread):
    faults = faults_off()
    faults.switches["F13_partial_fill"] = True
    broker, ledger = mk_broker(faults=faults), mk_ledger()
    k = lambda d: pd.Timestamp(d)
    broker.process_bar(0, k("2026-01-05"),
                       {"TEST": _flat(daily_ts("2026-01-05", "America/New_York"),
                                      100.0)}, ledger)
    order = broker.submit(
        OrderSpec("TEST", D("20"), OrderType.STOP, stop_price=D("105"),
                  tif=TimeInForce.GOOD_TILL_CANCEL), k("2026-01-05"), 0, ledger)
    assert order.status is OrderStatus.NEW
    f1 = broker.process_bar(1, k("2026-01-06"),
                            {"TEST": _bar(daily_ts("2026-01-06",
                                                   "America/New_York"),
                                          100, 106, 99, 100, v=100.0)}, ledger)
    assert [x.quantity for x in f1] == [D("10")]     # 10% of volume 100
    assert f1[0].price == D("105")                   # stop touch
    f2 = broker.process_bar(2, k("2026-01-07"),
                            {"TEST": _bar(daily_ts("2026-01-07",
                                                   "America/New_York"),
                                          95, 96, 94, 95, v=100.0)}, ledger)
    assert [x.quantity for x in f2] == [D("10")]     # market leg, no re-trigger
    assert f2[0].price == D("95")                    # next bar open
    assert order.status is OrderStatus.FILLED


# ---------------------------------------------------------------------------
# c11 + c12: stop-limit matching semantics under O-H-L-C.
# ---------------------------------------------------------------------------

def test_stop_limit_marketable_fills_at_stop_touch():
    bar = _bar(pd.Timestamp("2026-01-06", tz="UTC"), 100, 107, 99, 101)
    triggered, raw = match_stop_limit(True, D("105"), D("110"), bar)
    assert triggered and raw == D("105")   # not 110: that price never traded


def test_stop_limit_sell_needs_post_trigger_evidence():
    # Sell stop 95, limit 97 (non-marketable). High 104 printed BEFORE the
    # drop through 95, so it must not justify a 97 fill; only the close can.
    below = _bar(pd.Timestamp("2026-01-06", tz="UTC"), 100, 104, 94, 96)
    triggered, raw = match_stop_limit(False, D("95"), D("97"), below)
    assert triggered and raw is None
    above = _bar(pd.Timestamp("2026-01-07", tz="UTC"), 100, 104, 94, 98)
    triggered, raw = match_stop_limit(False, D("95"), D("97"), above)
    assert triggered and raw == D("97")


def test_stop_limit_sell_marketable_fills_at_stop():
    bar = _bar(pd.Timestamp("2026-01-06", tz="UTC"), 100, 104, 94, 96)
    triggered, raw = match_stop_limit(False, D("95"), D("93"), bar)
    assert triggered and raw == D("95")


# ---------------------------------------------------------------------------
# c2: the US/LSE overlap window must track both DST regimes. Winter samples:
# 14:00 UTC is BEFORE the US open (NY 09:00) and 16:00 UTC is inside the
# overlap (NY 11:00, London 16:00) -- the old fixed 13:30-15:30 UTC window
# gets both wrong.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts, expected", [
    ("2026-01-15 14:00", False),   # winter: NY 09:00, pre-open
    ("2026-01-15 16:00", True),    # winter: NY 11:00, London 16:00
    ("2026-01-15 16:30", False),   # winter: London 16:30, LSE closed
    ("2026-07-15 14:00", True),    # summer: NY 10:00, London 15:00
    ("2026-07-15 15:45", False),   # summer: London 16:45, LSE closed
    ("2026-07-15 13:00", False),   # summer: NY 09:00, pre-open
])
def test_us_overlap_tracks_dst(ts, expected):
    assert in_us_overlap(pd.Timestamp(ts, tz="UTC")) is expected


# ---------------------------------------------------------------------------
# c5/c15/c21: the engine floors order deltas to the venue's 4 dp order grid,
# so a non-grid target trades its dust-truncated size instead of being
# rejected with quantity_precision_mismatch on every bar forever.
# ---------------------------------------------------------------------------

def test_engine_floors_targets_to_venue_precision(zero_spread):
    from backtest.engine.engine import BacktestEngine
    from backtest.engine.feed import BarFeed, FxSeries
    from backtest.t212.broker_sim import T212BrokerSim
    from backtest.t212.costs import price_to_gbp
    from backtest.t212.instruments import exchange_tz
    from tests.backtest.conftest import bar_frame, fx_frame

    dates = [f"2026-01-{d:02d}" for d in (5, 6, 7, 8, 9)]
    frame = bar_frame([(daily_ts(d, "America/New_York"), 100, 100, 100, 100,
                        1e9) for d in dates], "GBP")
    fx = FxSeries(fx_frame([f"2026-01-{d:02d}" for d in range(1, 12)],
                           [1.25] * 11), 86400)
    faults = faults_off()
    faults.switches["F8_quantity_precision"] = True
    broker = T212BrokerSim(cost_cfg_clean(), faults, "1d", fx, daily=True)
    config = EngineConfig(symbols=["TEST"], interval="1d", start=dates[0],
                          end=dates[-1], initial_cash_gbp=D("10000"),
                          arm="unit", seed=1)

    def third_of_a_share(view, portfolio, params):
        return {"TEST": D("1") / D("3")}

    engine = BacktestEngine(config, BarFeed({"TEST": frame}, exchange_tz, True),
                            fx, broker, third_of_a_share, price_to_gbp)
    result = engine.run()
    assert len(result.trades) == 1
    assert result.trades["quantity"].iloc[0] == 0.3333
    rejected = result.orders[result.orders["reason"]
                             == "quantity_precision_mismatch"]
    assert rejected.empty


# ---------------------------------------------------------------------------
# U14 completions: F11 stale ticker, F14 auth outage, F12 pacing.
# ---------------------------------------------------------------------------

def test_stale_ticker_toggle(zero_spread):
    on = faults_off(stale_tickers={"TEST"})
    on.switches["F11_stale_ticker"] = True
    off = faults_off(stale_tickers={"TEST"})
    for faults, expected in ((on, OrderStatus.REJECTED),
                             (off, OrderStatus.NEW)):
        broker, ledger = mk_broker(faults=faults), mk_ledger()
        broker.process_bar(0, pd.Timestamp("2026-01-05"),
                           {"TEST": _flat(daily_ts("2026-01-05",
                                                   "America/New_York"),
                                          100.0)}, ledger)
        order = broker.submit(OrderSpec("TEST", D("1")),
                              pd.Timestamp("2026-01-05"), 0, ledger)
        assert order.status is expected
        if expected is OrderStatus.REJECTED:
            assert order.reason == "entity_not_found"


def test_auth_outage_toggle(zero_spread):
    windows = [("2026-01-05T00:00:00", "2026-01-06T00:00:00")]
    on = faults_off(auth_outage_windows=windows)
    on.switches["F14_auth_outage_window"] = True
    off = faults_off(auth_outage_windows=windows)
    for faults, expected in ((on, OrderStatus.REJECTED),
                             (off, OrderStatus.NEW)):
        broker, ledger = mk_broker(faults=faults), mk_ledger()
        broker.process_bar(0, pd.Timestamp("2026-01-05"),
                           {"TEST": _flat(daily_ts("2026-01-05",
                                                   "America/New_York"),
                                          100.0)}, ledger)
        order = broker.submit(OrderSpec("TEST", D("1")),
                              pd.Timestamp("2026-01-05"), 0, ledger)
        assert order.status is expected


def test_submit_pacing_on_one_minute_bars(zero_spread):
    on = faults_off()
    on.switches["F12_submit_pacing"] = True
    broker, ledger = mk_broker(faults=on, interval="1m"), mk_ledger()
    t0 = pd.Timestamp("2026-01-05 14:30", tz="UTC")
    broker.process_bar(0, t0, {"TEST": _flat(t0, 100.0)}, ledger)
    # Limit-type cap on 1m bars: 60s / 2s per request = 30 per bar.
    statuses = []
    for i in range(31):
        order = broker.submit(
            OrderSpec("TEST", D("1"), OrderType.LIMIT, limit_price=D("90"),
                      tif=TimeInForce.GOOD_TILL_CANCEL), t0, 0, ledger)
        statuses.append(order.status)
    assert statuses[:30] == [OrderStatus.NEW] * 30
    assert statuses[30] is OrderStatus.REJECTED


# ---------------------------------------------------------------------------
# U16 (file level): two identical runs write byte-identical result files.
# ---------------------------------------------------------------------------

def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_write_run_byte_identical(zero_spread, tmp_path):
    from backtest.engine.metrics import compute_metrics
    from backtest.t212.instruments import T212_ANNUALIZATION_DAYS
    from tests.backtest.test_engine import _config, _mk_engine, _buy_and_hold

    config = _config()
    hashes = []
    for run_dir in (tmp_path / "a", tmp_path / "b"):
        result = _mk_engine(config, _buy_and_hold).run()
        metrics = compute_metrics(result.equity, result.trades, 10000.0,
                                  T212_ANNUALIZATION_DAYS)
        paths = write_run(result, config, metrics, out_dir=run_dir)
        hashes.append({k: _sha(p) for k, p in paths.items() if p.exists()})
    assert hashes[0] == hashes[1]
    assert set(hashes[0]) == {"trades", "equity", "meta"}
