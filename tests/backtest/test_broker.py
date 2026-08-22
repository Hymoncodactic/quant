"""Broker simulator tests: fill timing, admission checks, order lifecycle and
fault toggles.

Responsibility: pin the behavior of backtest/t212/broker_sim.py that decides
whether a simulated fill could have happened in reality. Four groups are
covered. Fill timing: a market order fills at the next bar's open, never on
the bar it was submitted on, and queues across missing bars until the market
reopens. Admission: quantity precision, minimum order value, unknown symbol,
overselling and the buying-power buffer each reject with their own reason
string. Lifecycle: sell reservations, DAY expiry against GTC survival,
volume-capped partial fills carried across bars, and the rejected-but-live
duplicate. Fault switches: the cancel race, the outage window and the
reduce-only window. Discrimination design is recorded in
docs/backtest/validation/02_test_plan.md items U3 to U6, U12, U14 and U15, plus the
fault catalog behaviors F5 to F13; the discriminating numbers are stated in
the section comments, for instance the next-bar-open case where the
submission bar closes at 110 and the next bar opens at 120, so that a
same-bar-close implementation would produce 110 and a same-bar-open one 100.

Out of scope: fee and tax arithmetic, which is covered by
tests/backtest/test_costs.py; the engine loop around the broker, covered by
tests/backtest/test_engine.py; the 2026-08-20 review findings, covered by
tests/backtest/test_review_regressions.py; the 2026-08-21 conservatism
findings, covered by tests/backtest/test_conservatism_and_metrics.py.

Public functions:
    test_market_order_fills_next_bar_open(zero_spread)
        A market order fills at the following bar's open, at that bar's step.
    test_no_fill_on_submission_bar(zero_spread)
        With no further bar delivered the order stays NEW and unfilled.
    test_market_order_queues_until_market_reopens(zero_spread)
        Bars absent from the feed do not fill the order; the next session's
        open does.
    test_quantity_precision_reject()
        Under the F8 switch a quantity finer than four decimal places is
        rejected while the four-decimal one is admitted.
    test_minimum_order_value_reject(zero_spread)
        An order below the minimum notional is rejected.
    test_unknown_symbol_reject()
        A symbol the broker has no market data for is rejected.
    test_oversell_reject(zero_spread)
        Selling equity that is not held is rejected.
    test_buying_power_buffer(zero_spread)
        Under the F9 switch the same buy fails against the buffered free cash
        and passes without the buffer.
    test_sell_reservation(zero_spread)
        Under the F10 switch shares reserved by a pending sell cannot be sold
        a second time.
    test_day_expires_gtc_survives(zero_spread)
        Under the F15 switch the same order expires as DAY and survives as
        GTC.
    test_partial_fill_carries_across_bars(zero_spread)
        Under the F13 switch a 25-share order fills 10, 10 then 5 across three
        bars and ends FILLED.
    test_reject_with_live_duplicate(zero_spread)
        Under the F5 and F6 switches the client sees a reject while a live
        duplicate fills on the next bar.
    test_cancel_race_toggle(zero_spread)
        Under the F7 switch the cancel loses at probability 1 and wins at
        probability 0.
    test_outage_window_blocks_submissions(zero_spread)
        Under the F3 switch submissions inside the window are rejected and
        those outside it are admitted.
    test_reduce_only_window(zero_spread)
        Under the F4 switch buys inside the window are rejected and sells are
        admitted.
    test_limit_buy_prices(zero_spread)
        A gapped open fills at the better open, a touch fills exactly at the
        limit, and an unreached limit does not fill.

Public classes: None.

Constants:
    D
        Alias of decimal.Decimal. Quantities, prices and cash are Decimal
        throughout the engine, so the tests never introduce a float where the
        production path uses a Decimal.
    TZ_NY
        "America/New_York". Time zone the synthetic daily bars are stamped in;
        it matches what backtest/t212/instruments.exchange_tz returns for the
        unmapped test symbols.

Inputs: None. Every bar is synthesized in process; no path under data/ is
read.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec. bar_frame and
                cost_cfg_clean are recorded as imported but unused in this
                module at the time of writing; the import line is left as it
                stands because changing code is outside the scope of this
                change.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from backtest.engine.types import (OrderSpec, OrderStatus, OrderType,
                                   TimeInForce)
from tests.backtest.conftest import (bar_frame, cost_cfg_clean, daily_ts,
                                     faults_off, mk_broker, mk_ledger)

D = Decimal
TZ_NY = "America/New_York"


def _bars(symbol: str, date: str, o: float, h: float, l: float, c: float,
          v: float = 1e9, ccy: str = "GBP") -> dict:
    from backtest.engine.types import Bar
    return {symbol: Bar(ts=daily_ts(date, TZ_NY), open=o, high=h, low=l,
                        close=c, volume=v, quote_ccy=ccy)}


def _key(date: str) -> pd.Timestamp:
    return pd.Timestamp(date)


def _prime(broker, ledger, symbol="TEST", date="2026-01-05", price=100.0):
    """Deliver one bar so the broker has market data to validate against."""
    broker.process_bar(0, _key(date), _bars(symbol, date, price, price,
                                            price, price), ledger)


# ---------------------------------------------------------------------------
# U3 + U4: next-bar-open fill, never same-bar. The submission bar closes at
# 110 and the next bar opens at 120; a same-bar-close implementation fills at
# 110, a same-bar-open one at 100. Only 120 passes.
# ---------------------------------------------------------------------------

def test_market_order_fills_next_bar_open(zero_spread):
    broker, ledger = mk_broker(), mk_ledger()
    broker.process_bar(0, _key("2026-01-05"),
                       _bars("TEST", "2026-01-05", 100, 111, 99, 110), ledger)
    order = broker.submit(OrderSpec("TEST", D("1")), _key("2026-01-05"), 0, ledger)
    assert order.status is OrderStatus.NEW
    fills = broker.process_bar(1, _key("2026-01-06"),
                               _bars("TEST", "2026-01-06", 120, 125, 119, 121),
                               ledger)
    assert len(fills) == 1
    assert fills[0].price == D("120")
    assert fills[0].step == 1 and order.submitted_step == 0


def test_no_fill_on_submission_bar(zero_spread):
    broker, ledger = mk_broker(), mk_ledger()
    _prime(broker, ledger)
    order = broker.submit(OrderSpec("TEST", D("1")), _key("2026-01-05"), 0, ledger)
    # No further bar arrives: the order must still be NEW, not filled.
    assert order.status is OrderStatus.NEW and order.filled_qty == D("0")


# ---------------------------------------------------------------------------
# F16: market order queues across missing bars (market closed) and fills at
# the next session's open.
# ---------------------------------------------------------------------------

def test_market_order_queues_until_market_reopens(zero_spread):
    broker, ledger = mk_broker(), mk_ledger()
    _prime(broker, ledger)
    broker.submit(OrderSpec("TEST", D("1")), _key("2026-01-05"), 0, ledger)
    assert broker.process_bar(1, _key("2026-01-06"), {}, ledger) == []
    assert broker.process_bar(2, _key("2026-01-07"), {}, ledger) == []
    fills = broker.process_bar(3, _key("2026-01-08"),
                               _bars("TEST", "2026-01-08", 105, 106, 104, 105),
                               ledger)
    assert len(fills) == 1 and fills[0].price == D("105")


# ---------------------------------------------------------------------------
# U5/U6 shape: admission rejects. Precision (4 dp cap), minimum order value,
# unknown symbol, oversell.
# ---------------------------------------------------------------------------

def test_quantity_precision_reject():
    faults = faults_off()
    faults.switches["F8_quantity_precision"] = True
    broker, ledger = mk_broker(faults=faults), mk_ledger()
    _prime(broker, ledger)
    bad = broker.submit(OrderSpec("TEST", D("0.12345")), _key("2026-01-05"), 0,
                        ledger)
    good = broker.submit(OrderSpec("TEST", D("0.1234")), _key("2026-01-05"), 0,
                         ledger)
    assert bad.status is OrderStatus.REJECTED
    assert bad.reason == "quantity_precision_mismatch"
    assert good.status is OrderStatus.NEW


def test_minimum_order_value_reject(zero_spread):
    broker, ledger = mk_broker(), mk_ledger()
    _prime(broker, ledger, price=100.0)
    small = broker.submit(OrderSpec("TEST", D("0.009")), _key("2026-01-05"), 0,
                          ledger)
    assert small.status is OrderStatus.REJECTED
    assert small.reason == "order_value_below_minimum"


def test_unknown_symbol_reject():
    broker, ledger = mk_broker(), mk_ledger()
    order = broker.submit(OrderSpec("NEVER", D("1")), _key("2026-01-05"), 0,
                          ledger)
    assert order.status is OrderStatus.REJECTED
    assert order.reason == "no_market_data"


def test_oversell_reject(zero_spread):
    broker, ledger = mk_broker(), mk_ledger()
    _prime(broker, ledger)
    order = broker.submit(OrderSpec("TEST", D("-1")), _key("2026-01-05"), 0,
                          ledger)
    assert order.status is OrderStatus.REJECTED
    assert order.reason == "selling_equity_not_owned"


# ---------------------------------------------------------------------------
# F9: buying-power buffer. A buy of ~959 GBP against 1000 cash passes without
# the buffer and fails with it (0.95 * 1000 = 950 < 959).
# ---------------------------------------------------------------------------

def test_buying_power_buffer(zero_spread):
    on = faults_off()
    on.switches["F9_buying_power_buffer"] = True
    for faults, expected in ((on, OrderStatus.REJECTED),
                             (faults_off(), OrderStatus.NEW)):
        broker, ledger = mk_broker(faults=faults), mk_ledger("1000")
        _prime(broker, ledger, price=95.9)
        order = broker.submit(OrderSpec("TEST", D("10")), _key("2026-01-05"), 0,
                              ledger)
        assert order.status is expected
        if expected is OrderStatus.REJECTED:
            assert order.reason == "insufficient_free_for_stocks_buy"


# ---------------------------------------------------------------------------
# F10: shares reserved by a pending sell cannot be sold again.
# ---------------------------------------------------------------------------

def test_sell_reservation(zero_spread):
    faults = faults_off()
    faults.switches["F10_sell_reservation"] = True
    broker, ledger = mk_broker(faults=faults), mk_ledger()
    broker.process_bar(0, _key("2026-01-05"),
                       _bars("TEST", "2026-01-05", 100, 100, 100, 100), ledger)
    broker.submit(OrderSpec("TEST", D("10")), _key("2026-01-05"), 0, ledger)
    broker.process_bar(1, _key("2026-01-06"),
                       _bars("TEST", "2026-01-06", 100, 100, 100, 100), ledger)
    first = broker.submit(OrderSpec("TEST", D("-6")), _key("2026-01-06"), 1, ledger)
    second = broker.submit(OrderSpec("TEST", D("-6")), _key("2026-01-06"), 1, ledger)
    assert first.status is OrderStatus.NEW
    assert second.status is OrderStatus.REJECTED
    assert second.reason == "selling_equity_not_owned"


# ---------------------------------------------------------------------------
# U15: DAY expires at the next exchange-local day, GTC survives. Same order,
# same bars, two different fates.
# ---------------------------------------------------------------------------

def test_day_expires_gtc_survives(zero_spread):
    faults = faults_off()
    faults.switches["F15_day_expiry"] = True
    for tif, expected in ((TimeInForce.DAY, OrderStatus.CANCELLED),
                          (TimeInForce.GOOD_TILL_CANCEL, OrderStatus.NEW)):
        broker, ledger = mk_broker(faults=faults), mk_ledger()
        _prime(broker, ledger)
        order = broker.submit(
            OrderSpec("TEST", D("1"), OrderType.LIMIT, limit_price=D("90"),
                      tif=tif), _key("2026-01-05"), 0, ledger)
        broker.process_bar(1, _key("2026-01-06"),
                           _bars("TEST", "2026-01-06", 100, 101, 99, 100),
                           ledger)
        assert order.status is expected, tif
        if expected is OrderStatus.CANCELLED:
            assert order.reason == "day_expired"


# ---------------------------------------------------------------------------
# U12 / F13: volume participation cap forces partial fills across bars.
# Volume 100 and 10% participation admit 10 shares per bar: 25 shares need
# three bars (10 + 10 + 5). An uncapped implementation fills 25 at once.
# ---------------------------------------------------------------------------

def test_partial_fill_carries_across_bars(zero_spread):
    faults = faults_off()
    faults.switches["F13_partial_fill"] = True
    broker, ledger = mk_broker(faults=faults), mk_ledger()
    _prime(broker, ledger)
    order = broker.submit(OrderSpec("TEST", D("25")), _key("2026-01-05"), 0,
                          ledger)
    qtys = []
    for i, day in enumerate(["2026-01-06", "2026-01-07", "2026-01-08"], 1):
        fills = broker.process_bar(i, _key(day),
                                   _bars("TEST", day, 100, 101, 99, 100,
                                         v=100.0), ledger)
        qtys.extend(f.quantity for f in fills)
    assert qtys == [D("10"), D("10"), D("5")]
    assert order.status is OrderStatus.FILLED


# ---------------------------------------------------------------------------
# F5/F6: rejected-but-actually-live. With reject and duplicate probabilities
# forced to 1, the client sees REJECTED yet a live order fills next bar --
# the documented non-idempotency trap.
# ---------------------------------------------------------------------------

def test_reject_with_live_duplicate(zero_spread):
    faults = faults_off(reject_prob=1.0, duplicate_prob=1.0)
    faults.switches["F5_reject_random"] = True
    faults.switches["F6_duplicate_on_retry"] = True
    broker, ledger = mk_broker(faults=faults), mk_ledger()
    _prime(broker, ledger)
    visible = broker.submit(OrderSpec("TEST", D("1")), _key("2026-01-05"), 0,
                            ledger)
    assert visible.status is OrderStatus.REJECTED
    assert visible.reason == "undefined_error"
    live = broker.open_orders("TEST")
    assert len(live) == 1 and live[0].reason == "duplicate_of_rejected_submit"
    fills = broker.process_bar(1, _key("2026-01-06"),
                               _bars("TEST", "2026-01-06", 100, 101, 99, 100),
                               ledger)
    assert len(fills) == 1     # the position moved despite the visible reject


# ---------------------------------------------------------------------------
# F7: cancel race. Probability 1 means every cancel racing a fillable order
# loses (the fill happens); probability 0 means the cancel wins.
# ---------------------------------------------------------------------------

def test_cancel_race_toggle(zero_spread):
    for prob, filled in ((1.0, True), (0.0, False)):
        faults = faults_off(cancel_race_prob=prob)
        faults.switches["F7_cancel_race"] = True
        broker, ledger = mk_broker(faults=faults), mk_ledger()
        _prime(broker, ledger)
        order = broker.submit(OrderSpec("TEST", D("1")), _key("2026-01-05"), 0,
                              ledger)
        assert broker.cancel(order.order_id) is True
        fills = broker.process_bar(1, _key("2026-01-06"),
                                   _bars("TEST", "2026-01-06", 100, 101, 99,
                                         100), ledger)
        assert bool(fills) is filled
        expected = OrderStatus.FILLED if filled else OrderStatus.CANCELLED
        assert order.status is expected


# ---------------------------------------------------------------------------
# F3: outage window blocks submission inside it and only inside it.
# ---------------------------------------------------------------------------

def test_outage_window_blocks_submissions(zero_spread):
    faults = faults_off(
        outage_windows=[("2026-01-06T00:00:00", "2026-01-07T00:00:00")])
    faults.switches["F3_outage_window"] = True
    broker, ledger = mk_broker(faults=faults), mk_ledger()
    _prime(broker, ledger)
    inside = broker.submit(OrderSpec("TEST", D("1")), _key("2026-01-06"), 1,
                           ledger)
    outside = broker.submit(OrderSpec("TEST", D("1")), _key("2026-01-07"), 2,
                            ledger)
    assert inside.status is OrderStatus.REJECTED
    assert inside.reason == "outage_window"
    assert outside.status is OrderStatus.NEW


# ---------------------------------------------------------------------------
# F4: reduce-only window rejects buys, admits sells.
# ---------------------------------------------------------------------------

def test_reduce_only_window(zero_spread):
    faults = faults_off(
        reduce_only_windows=[("TEST", "2026-01-06T00:00:00",
                              "2026-01-08T00:00:00")])
    faults.switches["F4_reduce_only_window"] = True
    broker, ledger = mk_broker(faults=faults), mk_ledger()
    broker.process_bar(0, _key("2026-01-05"),
                       _bars("TEST", "2026-01-05", 100, 100, 100, 100), ledger)
    broker.submit(OrderSpec("TEST", D("5")), _key("2026-01-05"), 0, ledger)
    broker.process_bar(1, _key("2026-01-06"),
                       _bars("TEST", "2026-01-06", 100, 100, 100, 100), ledger)
    buy = broker.submit(OrderSpec("TEST", D("1")), _key("2026-01-06"), 1, ledger)
    sell = broker.submit(OrderSpec("TEST", D("-1")), _key("2026-01-06"), 1, ledger)
    assert buy.status is OrderStatus.REJECTED and buy.reason == "reduce_only"
    assert sell.status is OrderStatus.NEW


# ---------------------------------------------------------------------------
# Limit order matching through the broker: gap-open fills at the better open,
# intra-bar touch fills exactly at the limit.
# ---------------------------------------------------------------------------

def test_limit_buy_prices(zero_spread):
    broker, ledger = mk_broker(), mk_ledger()
    _prime(broker, ledger)
    gap = broker.submit(OrderSpec("TEST", D("1"), OrderType.LIMIT,
                                  limit_price=D("102"),
                                  tif=TimeInForce.GOOD_TILL_CANCEL),
                        _key("2026-01-05"), 0, ledger)
    touch = broker.submit(OrderSpec("TEST", D("1"), OrderType.LIMIT,
                                    limit_price=D("99"),
                                    tif=TimeInForce.GOOD_TILL_CANCEL),
                          _key("2026-01-05"), 0, ledger)
    miss = broker.submit(OrderSpec("TEST", D("1"), OrderType.LIMIT,
                                   limit_price=D("90"),
                                   tif=TimeInForce.GOOD_TILL_CANCEL),
                         _key("2026-01-05"), 0, ledger)
    fills = broker.process_bar(1, _key("2026-01-06"),
                               _bars("TEST", "2026-01-06", 101, 103, 98, 102),
                               ledger)
    by_order = {f.order_id: f for f in fills}
    assert by_order[gap.order_id].price == D("101")    # open below limit
    assert by_order[touch.order_id].price == D("99")   # exactly the limit
    assert miss.order_id not in by_order               # low 98 > 90
