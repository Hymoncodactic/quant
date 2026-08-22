"""Same-close execution: fill a market order at the decision bar's close.

Responsibility: the "last minute before the close" convention
(EngineConfig.fill_timing == "same_close", user ruling 2026-08-22 recorded in
research/decisions/20260822_close_execution_timing.md). A strategy computed
on the close places its market order about one minute before the session
ends; the fill is modeled at the official close, charged adversely with the
calibrated close-proximity gap (CostConfig.close_gap_bps) on top of spread
and slippage, and only succeeds when the latency draw fits inside
CostConfig.close_window_sec -- otherwise the order keeps its next-open
eligibility and spills to the following session like any queued order.
Not responsible for: next-open matching (broker_sim.py) or fill booking
(fills.py).

This is a declared deviation from backtest-discipline hard list items 1-2;
the result file name carries the mode so it can never be mistaken for the
conservative default.

Public functions:
    try_same_close_fill(broker, order, key, ledger, latency_sec)
"""

from __future__ import annotations

__all__ = ["try_same_close_fill"]

from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd

from backtest.engine.ledger import Ledger
from backtest.engine.types import Fill, Order, OrderType
from backtest.t212.fills import fill_order

if TYPE_CHECKING:
    from backtest.t212.broker_sim import T212BrokerSim


def try_same_close_fill(broker: "T212BrokerSim", order: Order,
                        key: pd.Timestamp, ledger: Ledger,
                        latency_sec: float) -> Fill | None:
    """Attempt the close fill; None leaves the order on the next-open path.

    Preconditions, each a real-world reason the close fill would not happen:
    market order only; latency inside the close window; the symbol actually
    traded on this key (market open); cooldown clear. Volume budget, funds
    gate and cost stack are applied by fill_order like any other execution.
    """
    spec = order.spec
    if spec.order_type is not OrderType.MARKET or not order.is_open:
        return None
    if latency_sec > broker.cost_cfg.close_window_sec:
        return None
    if spec.symbol not in broker.bars_this_step:
        return None
    bar = broker.last_bar(spec.symbol)
    if bar is None or not broker.cooldown_ok(order, key):
        return None
    raw = Decimal(str(bar.close))
    extra = broker.cost_cfg.slippage_bps + broker.cost_cfg.close_gap_bps
    return fill_order(broker, order, bar, raw, broker.current_step, key,
                      ledger, extra_bps=extra, at_close=True)
