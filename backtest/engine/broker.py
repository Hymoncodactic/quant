"""Broker simulator protocol: the one interface every venue adapter and the
future live execution adapter implement (fixplans/framework/01_architecture.md
section 2; the same-interface principle is the survey's lumibot lesson).

Responsibility: the type contract only. No behavior lives here.

Public classes:
    BrokerSim   typing.Protocol implemented by backtest/t212/broker_sim.py
"""

from __future__ import annotations

__all__ = ["BrokerSim"]

from decimal import Decimal
from typing import Protocol, runtime_checkable

import pandas as pd

from backtest.engine.ledger import Ledger
from backtest.engine.types import Bar, Fill, Order, OrderSpec


@runtime_checkable
class BrokerSim(Protocol):
    """What the engine requires of a broker simulator."""

    orders: dict[int, Order]

    def submit(self, spec: OrderSpec, key: pd.Timestamp, step: int,
               ledger: Ledger) -> Order:
        """Admission-check one request; return the NEW or REJECTED order."""

    def cancel(self, order_id: int) -> bool:
        """Request cancellation; True means the request was accepted."""

    def process_bar(self, step: int, key: pd.Timestamp, bars: dict[str, Bar],
                    ledger: Ledger) -> list[Fill]:
        """Advance one bar: expiries, cancels, matching. Returns fills."""

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        """Currently open orders, optionally for one symbol."""

    def pending_signed_qty(self, symbol: str) -> Decimal:
        """Net unfilled signed quantity across open orders of one symbol."""

    def order_quantity_step(self) -> Decimal:
        """Smallest order-quantity increment the venue accepts right now."""
