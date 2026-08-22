"""Broker simulator protocol: the one interface every venue adapter and the
future live execution adapter implement.

Responsibility: the type contract only, expressed as a runtime-checkable
typing.Protocol, so the engine talks to any broker through a single surface
(docs/backtest/framework/01_architecture.md section 2, where the same-interface
principle is recorded as the lesson taken from the lumibot survey). No behavior
lives here. Retention note: no module imports this Protocol at runtime today
because backtest/t212/broker_sim.py duck-types it rather than inheriting it;
the file is kept because it fixes the design ruling in a form a type checker
and the future live adapter can both use.

Out of scope: every behavior behind the interface. Admission checks live in
backtest/t212/admission.py, latency and platform faults in
backtest/t212/faults.py, order lifecycle and cost application in
backtest/t212/broker_sim.py, and the pure trigger and price rules in
backtest/engine/matching.py.

Public classes:
    BrokerSim   Protocol requiring one attribute, orders, and six methods:
                submit, cancel, process_bar, open_orders, pending_signed_qty
                and order_quantity_step.

Constants: None.

Inputs: None.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
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
