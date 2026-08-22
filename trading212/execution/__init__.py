"""Trading 212 execution layer package.

Responsibility: declare the execution layer as a regular Python package. The
layer is registered in ARCHITECTURE.md section 2 as the place for order
submission, cancelation, the order state machine and position reconciliation.
No implementation module exists in this package yet, so the declaration serves
only to keep the package structure identical to ingest and strategy: a module
added later needs no separate package declaration step.

Out of scope: signal computation, which belongs to trading212/strategy/ and
must be imported from there rather than reimplemented here; fill simulation,
which belongs to backtest/t212/broker_sim.py; bar download and storage, which
belongs to trading212/ingest/.

Public functions: None. This module defines no symbol and imports nothing.

Constants: None.

Inputs: None.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""
