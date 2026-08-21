"""T212 Invest broker simulator: order admission, lifecycle, fills and costs.

Responsibility: everything between "the strategy wants an order" and "the
ledger books a fill", faithful to the venue contract and its documented faults
(fixplans/framework/03_order_lifecycle.md, fixplans/t212_faults/): submission
with pacing counters and cash reservation, eligibility expressed as a TIME
rather than a merged-timeline step count, DAY expiry at exchange-local
midnight, the one-shot cancel-versus-fill race, the per-symbol per-bar volume
participation budget, the cooldown between fills of different orders, the funds
gate re-applied at execution, and the release or re-reservation of frozen cash
after each fill. Reject reasons follow the venue's observed error vocabulary
where one exists (insufficient_free_for_stocks_buy, selling_equity_not_owned,
quantity_precision_mismatch, entity_not_found -- sources in
fixplans/framework/03_order_lifecycle.md section 1.4).

Out of scope: raw trigger and price rules (backtest/engine/matching.py); cost
arithmetic (backtest/t212/costs.py); fault parameters and their evaluation
(backtest/t212/faults.py); the submission checks themselves
(backtest/t212/admission.py); cash and position accounting
(backtest/engine/ledger.py).

Public classes:
    T212BrokerSim   The simulator, one instance per run; implements the
                    BrokerSim protocol of backtest/engine/broker.py. Lifecycle
                    entry points are submit, cancel and process_bar, alongside
                    the query surface that the engine and admission.py read.

Constants:
    ZERO        Decimal("0"), so Decimal comparisons never mix in a float.
    _QTY_STEP   Decimal("0.00000001"), that is 8 decimal places: the maximum
                observed holding precision. It is the quantization grid for
                volume-capped fills and the order quantity step whenever the
                F8 precision fault is off.

Inputs: None. Pure computation over the bars, ledger and FX series passed in;
    no file or network access.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["T212BrokerSim"]

from decimal import Decimal, ROUND_DOWN

import pandas as pd

from backtest.engine.feed import FxSeries
from backtest.engine.ledger import Ledger
from backtest.engine.matching import (match_limit, match_market, match_stop,
                                      match_stop_limit)
from backtest.engine.types import (Bar, Fill, Order, OrderSpec, OrderStatus,
                                   OrderType, TimeInForce)
from backtest.t212.admission import admission_reason, estimated_buy_cost
from backtest.t212.costs import CostConfig, apply_spread, fill_cash_and_costs
from backtest.t212.faults import FaultConfig, FaultEngine
from backtest.t212.instruments import (exchange_tz, half_spread_bps,
                                       in_us_overlap, security_kind)

ZERO = Decimal("0")
_QTY_STEP = Decimal("0.00000001")  # 8 dp: max observed holding precision


class T212BrokerSim:
    """Order lifecycle simulator for a GBP Invest account."""

    def __init__(self, cost_cfg: CostConfig, fault_cfg: FaultConfig,
                 interval: str, fx: FxSeries, daily: bool) -> None:
        self.cost_cfg = cost_cfg
        self.faults = FaultEngine(fault_cfg, interval)
        self.fx = fx
        self.daily = daily
        self.orders: dict[int, Order] = {}
        self._next_id = 1
        self._last_bar: dict[str, Bar] = {}
        self._market_submits = 0
        self._pending_submits = 0
        self._current_step = -1
        self._current_key: pd.Timestamp | None = None
        # One bar interval as wall time. Eligibility is TIME-based: merged
        # multi-exchange timelines interleave (US :30 grid vs LSE :00 grid),
        # so step counts would leak the decision bar's unfinished close.
        self._interval = pd.Timedelta(seconds=self.faults.bar_seconds)
        # F13 volume participation is capped per SYMBOL per bar across all
        # orders (zipline's per-asset semantics), not per order.
        self._bar_volume_used: dict[str, Decimal] = {}
        # Cooldown bookkeeping: last fill (key, order_id) per symbol.
        self._last_fill: dict[str, tuple[pd.Timestamp, int]] = {}

    # ------------------------------------------------------------------
    # [1] Queries
    # ------------------------------------------------------------------

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self.orders.values() if o.is_open
                and (symbol is None or o.spec.symbol == symbol)]

    def pending_signed_qty(self, symbol: str) -> Decimal:
        """Net unfilled signed quantity across open orders of one symbol."""
        return sum((o.remaining_qty for o in self.open_orders(symbol)), ZERO)

    def pending_sell_qty(self, symbol: str) -> Decimal:
        """Shares reserved by open sell-side orders (positive number)."""
        return sum((-o.remaining_qty for o in self.open_orders(symbol)
                    if o.remaining_qty < ZERO), ZERO)

    def order_quantity_step(self) -> Decimal:
        """Smallest order-quantity increment the venue accepts right now:
        4 dp while F8 is on (catalog F8), else the 8 dp holding grid. The
        engine floors order deltas to this so a fractional target is
        dust-truncated once, not rejected on every bar forever."""
        decimals = self.faults.qty_decimals()
        return Decimal(1).scaleb(-decimals) if decimals is not None else _QTY_STEP

    def fx_query_ts(self, key: pd.Timestamp) -> pd.Timestamp:
        return key.tz_localize("UTC") if key.tzinfo is None else key

    def last_bar(self, symbol: str) -> Bar | None:
        """Most recent bar seen for one symbol, None before its first bar."""
        return self._last_bar.get(symbol)

    @property
    def market_submits(self) -> int:
        """Market-order submissions accepted during the current bar (F12)."""
        return self._market_submits

    @property
    def pending_submits(self) -> int:
        """Limit/stop-type submissions accepted during the current bar (F12)."""
        return self._pending_submits

    def _local_date(self, symbol: str, key: pd.Timestamp) -> str:
        if self.daily:
            return str(key.date())
        return str(key.tz_convert(exchange_tz(symbol)).date())

    # ------------------------------------------------------------------
    # [2] Submission
    # ------------------------------------------------------------------

    def submit(self, spec: OrderSpec, key: pd.Timestamp, step: int,
               ledger: Ledger) -> Order:
        """Admission-check one order request; return the NEW or REJECTED order."""
        order = Order(order_id=self._next_id, spec=spec, submitted_ts=key,
                      submitted_step=step, eligible_ts=key + self._interval,
                      submitted_local_date=self._local_date(spec.symbol, key))
        self._next_id += 1
        # Admission runs BEFORE the order is registered: pending-quantity and
        # pending-count checks must not see the order that is being admitted.
        reason = admission_reason(self, spec, key, step, ledger)
        self.orders[order.order_id] = order
        if reason is not None:
            order.status, order.reason = OrderStatus.REJECTED, reason
            return order

        rejected, duplicate_live = self.faults.reject_roll()
        if rejected:
            order.status, order.reason = OrderStatus.REJECTED, "undefined_error"
            if duplicate_live:
                self._accept(order, key, step, ledger,
                             reason="duplicate_of_rejected_submit")
            return order

        self._accept(order, key, step, ledger, reason="")
        return order

    def _accept(self, order: Order, key: pd.Timestamp, step: int,
                ledger: Ledger, reason: str) -> None:
        """Admit one order: pacing counters, latency, cash reservation.

        A duplicate born from a rejected submit (F6) is a second live order
        object so the audit trail shows both the client-visible rejection and
        the venue-side live order.
        """
        spec = order.spec
        if reason:
            live = Order(order_id=self._next_id, spec=spec, submitted_ts=key,
                         submitted_step=step, eligible_ts=key + self._interval,
                         reason=reason,
                         submitted_local_date=order.submitted_local_date)
            self._next_id += 1
            self.orders[live.order_id] = live
            order = live
        if spec.order_type is OrderType.MARKET:
            self._market_submits += 1
        else:
            self._pending_submits += 1
        bar = self._last_bar.get(spec.symbol)
        # Eligibility is a TIME: submission key plus whole bar intervals.
        # Resting order types get exactly one interval (their latency is the
        # trigger itself); a triggered stop draws its market-leg latency at
        # trigger time in _raw_price.
        extra = self.faults.latency_extra_bars(spec.symbol, bar, spec.order_type)
        order.eligible_ts = key + extra * self._interval
        if spec.quantity > ZERO:
            est = estimated_buy_cost(self, spec, bar, key)
            order.reserved_gbp = est
            ledger.reserve(order.order_id, est)

    def cancel(self, order_id: int) -> bool:
        """Request cancellation. True = request accepted (NOT a guarantee:
        the venue races cancels against fills, OpenAPI v0 cancelOrder)."""
        order = self.orders.get(order_id)
        if order is None or not order.is_open:
            return False
        order.cancel_requested = True
        return True

    # ------------------------------------------------------------------
    # [4] Bar processing
    # ------------------------------------------------------------------

    def process_bar(self, step: int, key: pd.Timestamp, bars: dict[str, Bar],
                    ledger: Ledger) -> list[Fill]:
        """Advance one bar: expiries, cancels, matching. Returns fills."""
        self._current_step, self._current_key = step, key
        self._market_submits = 0
        self._pending_submits = 0
        self._bar_volume_used = {}
        fills: list[Fill] = []
        for order in list(self.orders.values()):
            if not order.is_open:
                continue
            self._expire_if_day_over(order, key, ledger)
            if not order.is_open:
                continue
            bar = bars.get(order.spec.symbol)
            fill = self._settle_one(order, bar, step, key, ledger)
            if fill is not None:
                fills.append(fill)
        for symbol, bar in bars.items():
            self._last_bar[symbol] = bar
            self.faults.observe_bar(symbol, bar)
        return fills

    def _expire_if_day_over(self, order: Order, key: pd.Timestamp,
                            ledger: Ledger) -> None:
        """F15: DAY orders die at exchange-local midnight (spec TimeValidity).
        Market orders carry no timeValidity and queue until the market opens
        (F16), so they never expire here."""
        if order.spec.order_type is OrderType.MARKET:
            return
        if order.spec.tif is not TimeInForce.DAY:
            return
        if not self.faults.cfg.on("F15_day_expiry"):
            return
        if self._local_date(order.spec.symbol, key) > order.submitted_local_date:
            self._close(order, OrderStatus.CANCELLED, "day_expired", ledger)

    def _settle_one(self, order: Order, bar: Bar | None, step: int,
                    key: pd.Timestamp, ledger: Ledger) -> Fill | None:
        """Cancel resolution and matching for one open order on one bar.

        Eligibility is compared in TIME (key >= eligible_ts), never in
        merged-timeline steps; see __init__ for why steps are not intervals.
        """
        raw = None
        if bar is not None and key >= order.eligible_ts \
                and self._cooldown_ok(order, key):
            raw = self._raw_price(order, bar, key)
        if order.cancel_requested:
            # A cancel resolves exactly once: either it wins now, or it loses
            # to a fill that IS possible on this bar. Re-rolling every bar
            # would compound one request into many independent races.
            order.cancel_requested = False
            fill_possible = raw is not None and self._cap_allows(order, bar)
            if self.faults.cancel_succeeds(fill_possible):
                self._close(order, OrderStatus.CANCELLED, "canceled", ledger)
                return None
            order.reason = "cancel_lost_race"
        if raw is None:
            return None
        return self._fill(order, bar, raw, step, key, ledger)

    def _cooldown_ok(self, order: Order, key: pd.Timestamp) -> bool:
        """Cooldown between fills of DIFFERENT orders on one symbol (hard
        list item 7; knob in CostConfig). 1 = structural floor, always
        passes; same-order F13 rollover is one execution episode, exempt."""
        cooldown = self.cost_cfg.cooldown_bars
        last = self._last_fill.get(order.spec.symbol)
        if cooldown <= 1 or last is None:
            return True
        last_key, last_order_id = last
        if last_order_id == order.order_id:
            return True
        return key >= last_key + cooldown * self._interval

    def _cap_allows(self, order: Order, bar: Bar | None) -> bool:
        """Whether the per-symbol bar volume budget still admits any shares."""
        if bar is None:
            return False
        cap = self.faults.volume_cap_shares(bar)
        if cap is None:
            return True
        used = self._bar_volume_used.get(order.spec.symbol, ZERO)
        return (cap - used).quantize(_QTY_STEP, rounding=ROUND_DOWN) > ZERO

    def _raw_price(self, order: Order, bar: Bar,
                   key: pd.Timestamp) -> Decimal | None:
        spec = order.spec
        is_buy = spec.quantity > ZERO
        if spec.order_type is OrderType.MARKET:
            return match_market(bar)
        if spec.order_type is OrderType.LIMIT:
            return match_limit(is_buy, spec.limit_price, bar)
        if spec.order_type is OrderType.STOP:
            if order.triggered:
                # The conversion to a market leg is one-way (venue: trigger on
                # LTP, then trade as a market order); a partially filled or
                # latency-deferred remainder must NOT re-test the stop.
                return match_market(bar)
            raw = match_stop(is_buy, spec.stop_price, bar)
            if raw is None:
                return None
            order.triggered = True
            # Execution latency applies to the market leg and is drawn at
            # TRIGGER time; drawing it at submission would gate trigger
            # detection itself and silently miss trigger events.
            extra = self.faults.latency_extra_bars(spec.symbol, bar,
                                                   OrderType.MARKET)
            if extra <= 1:
                return raw
            order.eligible_ts = key + (extra - 1) * self._interval
            return None
        if order.triggered:
            return match_limit(is_buy, spec.limit_price, bar)
        triggered, raw = match_stop_limit(is_buy, spec.stop_price,
                                          spec.limit_price, bar)
        order.triggered = order.triggered or triggered
        return raw

    def half_spread(self, symbol: str, ts: pd.Timestamp) -> Decimal:
        """Half spread in bps, widened outside the US overlap for LSE lines
        (fixplans/framework/04_cost_model.md section 4.4)."""
        hs = half_spread_bps(symbol)
        if symbol.endswith(".L") and not in_us_overlap(self.fx_query_ts(ts)):
            hs = hs * self.cost_cfg.spread_session_multiplier
        return hs

    def _fill(self, order: Order, bar: Bar, raw: Decimal, step: int,
              key: pd.Timestamp, ledger: Ledger) -> Fill | None:
        """Price, cap, cost and book one execution against this bar."""
        spec = order.spec
        is_buy = spec.quantity > ZERO
        limit_leg = spec.order_type is OrderType.LIMIT or (
            spec.order_type is OrderType.STOP_LIMIT and order.triggered)
        hs = self.half_spread(spec.symbol, bar.ts)
        if limit_leg:
            exec_price = apply_spread(raw, is_buy, hs, ZERO)
            if spec.limit_price is not None:
                exec_price = min(exec_price, spec.limit_price) if is_buy \
                    else max(exec_price, spec.limit_price)
        else:
            exec_price = apply_spread(raw, is_buy, hs, self.cost_cfg.slippage_bps)

        qty = order.remaining_qty
        cap = self.faults.volume_cap_shares(bar)
        used = self._bar_volume_used.get(spec.symbol, ZERO)
        if cap is not None:
            # The participation cap binds per SYMBOL per bar across every
            # order, so concurrent orders share one budget.
            capped = min(abs(qty), cap - used).quantize(_QTY_STEP,
                                                        rounding=ROUND_DOWN)
            if capped <= ZERO:
                return None
            qty = capped if is_buy else -capped

        fx_mid = None
        if bar.quote_ccy == "USD":
            fx_mid = self.fx.rate_at(self.fx_query_ts(key))
        cash_delta, principal_gbp, costs = fill_cash_and_costs(
            qty, exec_price, bar.quote_ccy, fx_mid,
            security_kind(spec.symbol), spec.symbol.endswith(".L"),
            self.cost_cfg,
            prior_order_principal_gbp=order.filled_principal_gbp,
            ptm_already_charged=order.ptm_charged)
        # Funds gate at execution: this fill may draw on settled cash MINUS
        # what is frozen for OTHER pending orders (its own reservation is
        # naturally available to it). Comparing against total cash would let
        # one order spend another order's reservedForOrders.
        if is_buy and -cash_delta > ledger.cash_gbp - (ledger.reserved_gbp
                                                       - order.reserved_gbp):
            self._close(order, OrderStatus.CANCELLED,
                        "insufficient_free_funds_at_execution", ledger)
            return None

        fill = Fill(order_id=order.order_id, symbol=spec.symbol, ts=bar.ts,
                    step=step, quantity=qty, price=exec_price,
                    quote_ccy=bar.quote_ccy, fx_mid=fx_mid,
                    cash_delta_gbp=cash_delta, costs_gbp=costs)
        ledger.apply_fill(fill)
        order.filled_qty += qty
        order.filled_principal_gbp += principal_gbp
        if "ptm_levy" in costs:
            order.ptm_charged = True
        if cap is not None:
            self._bar_volume_used[spec.symbol] = used + abs(qty)
        self._last_fill[spec.symbol] = (key, order.order_id)
        if is_buy:
            spent = -cash_delta
            remaining_res = max(ZERO, order.reserved_gbp - spent)
            ledger.release(order.order_id)
            if order.remaining_qty != ZERO and remaining_res > ZERO:
                ledger.reserve(order.order_id, remaining_res)
            order.reserved_gbp = remaining_res if order.remaining_qty != ZERO else ZERO
        if order.remaining_qty == ZERO:
            order.status = OrderStatus.FILLED
            ledger.release(order.order_id)
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
            if order.cancel_requested:
                self._close(order, OrderStatus.CANCELLED,
                            "cancel_after_partial", ledger)
        return fill

    def _close(self, order: Order, status: OrderStatus, reason: str,
               ledger: Ledger) -> None:
        order.status, order.reason = status, reason
        order.reserved_gbp = ZERO
        ledger.release(order.order_id)
