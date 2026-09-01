"""Fill booking for the T212 broker simulator.

Responsibility: turn a raw matched price into an executed fill -- spread and
slippage, the per-symbol volume budget, the cost stack, ledger booking,
reservation arithmetic and order state -- and close orders cleanly with a
reason string for the audit trail.

Out of scope: deciding whether an order matches, which belongs to
backtest/t212/broker_sim.py and backtest/engine/matching.py; admission
checks, which belong to backtest/t212/admission.py; the same-close attempt,
which belongs to backtest/t212/same_close.py; the cost arithmetic itself,
which belongs to backtest/t212/costs.py.

Public functions:
    fill_order(broker, order, bar, raw, step, key, ledger, extra_bps, at_close)
        Price the raw match (half spread plus slippage, or the caller's
        extra_bps for a same-close fill), cap it by the symbol's remaining
        bar volume budget, run the cost stack, gate on free cash net of other
        orders' reservations, book the fill into the ledger, and advance the
        order state. Returns the Fill, or None when nothing could execute.
    close_order(broker, order, status, reason, ledger)
        Terminal transition: set status and reason, drop the reservation.

Constants:
    ZERO
        Decimal("0"), so Decimal comparisons never mix in a float.
    _QTY_STEP
        Decimal("0.00000001"): the 8 dp holding grid (largest fractional
        precision observed in real positions, fault catalog F8).

Inputs: None. Pure computation over the broker, ledger and bar objects.
Outputs: None directly; fills are booked into the ledger passed in.

Change log:
    2026-08-22  Extracted from broker_sim.py (fill_order was _fill, close_order
                was _close) to respect the 400-line module cap once the
                same-close execution path was added.
"""


from __future__ import annotations

__all__ = ["fill_order", "close_order"]

from decimal import Decimal, ROUND_DOWN
from typing import TYPE_CHECKING

import pandas as pd

from backtest.engine.ledger import Ledger
from backtest.engine.types import Bar, Fill, Order, OrderStatus, OrderType
from backtest.t212.costs import apply_spread, fill_cash_and_costs
from backtest.t212.instruments import security_kind

if TYPE_CHECKING:
    from backtest.t212.broker_sim import T212BrokerSim

ZERO = Decimal("0")
_QTY_STEP = Decimal("0.00000001")  # 8 dp: max observed holding precision


def fill_order(broker: "T212BrokerSim", order: Order, bar: Bar, raw: Decimal,
           step: int, key: pd.Timestamp, ledger: Ledger,
           extra_bps: Decimal | None = None,
           at_close: bool = False) -> Fill | None:
    """Price, cap, cost and book one execution against this bar.

    extra_bps overrides the market-leg slippage (same-close fills add the
    close-proximity gap); at_close stamps the fill for the engine's
    timing guard.
    """
    spec = order.spec
    is_buy = spec.quantity > ZERO
    limit_leg = spec.order_type is OrderType.LIMIT or (
        spec.order_type is OrderType.STOP_LIMIT and order.triggered)
    hs = broker.half_spread(spec.symbol, bar.ts)
    if limit_leg:
        exec_price = apply_spread(raw, is_buy, hs, ZERO)
        if spec.limit_price is not None:
            exec_price = min(exec_price, spec.limit_price) if is_buy \
                else max(exec_price, spec.limit_price)
    else:
        slip = broker.cost_cfg.slippage_for(spec.symbol) \
            if extra_bps is None else extra_bps
        exec_price = apply_spread(raw, is_buy, hs, slip)

    qty = order.remaining_qty
    cap = broker.faults.volume_cap_shares(bar)
    used = broker.bar_volume_used.get(spec.symbol, ZERO)
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
        fx_mid = broker.fx.rate_at(broker.fx_query_ts(key))
    cash_delta, principal_gbp, costs = fill_cash_and_costs(
        qty, exec_price, bar.quote_ccy, fx_mid,
        security_kind(spec.symbol), spec.symbol.endswith(".L"),
        broker.cost_cfg,
        prior_order_principal_gbp=order.filled_principal_gbp,
        ptm_already_charged=order.ptm_charged)
    # Funds gate at execution: this fill may draw on settled cash MINUS
    # what is frozen for OTHER pending orders (its own reservation is
    # naturally available to it). Comparing against total cash would let
    # one order spend another order's reservedForOrders.
    if is_buy and -cash_delta > ledger.cash_gbp - (ledger.reserved_gbp
                                                   - order.reserved_gbp):
        close_order(broker, order, OrderStatus.CANCELLED,
                    "insufficient_free_funds_at_execution", ledger)
        return None

    fill = Fill(order_id=order.order_id, symbol=spec.symbol, ts=bar.ts,
                step=step, quantity=qty, price=exec_price,
                quote_ccy=bar.quote_ccy, fx_mid=fx_mid,
                cash_delta_gbp=cash_delta, costs_gbp=costs,
                at_close=at_close)
    ledger.apply_fill(fill)
    order.filled_qty += qty
    order.filled_principal_gbp += principal_gbp
    if "ptm_levy" in costs:
        order.ptm_charged = True
    if cap is not None:
        broker.bar_volume_used[spec.symbol] = used + abs(qty)
    broker.last_fill[spec.symbol] = (key, order.order_id)
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
            close_order(broker, order, OrderStatus.CANCELLED,
                        "cancel_after_partial", ledger)
    return fill


def close_order(broker: "T212BrokerSim", order: Order, status: OrderStatus,
            reason: str, ledger: Ledger) -> None:
    order.status, order.reason = status, reason
    order.reserved_gbp = ZERO
    ledger.release(order.order_id)
