"""Shared fixtures for the execution-layer tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading212.execution.shadow_ledger import ShadowLedger

STRATEGY_ID = "a0_v0_0_1"


@pytest.fixture()
def ledger(tmp_path):
    """A fresh book with a 1000 GBP allocation in a temporary directory."""
    return ShadowLedger.init_fresh(tmp_path, STRATEGY_ID, Decimal("1000"))


@pytest.fixture()
def funded_ledger(ledger):
    """A book holding 2 NVDA shares bought for 260 GBP, no open orders."""
    ledger.record_intent("i1", "NVDA", "NVDA_US_EQ", Decimal("2"),
                         Decimal("175"), Decimal("1.35"), dry_run=False)
    ledger.record_submitted("i1", 111, "NVDA", "NVDA_US_EQ", Decimal("2"),
                            Decimal("259.26"), "NEW")
    ledger.record_fill(111, 9001, Decimal("2"), Decimal("175.50"),
                       Decimal("-260.00"), [], "2026-08-20T13:30:01Z")
    ledger.record_order_terminal(111, "FILLED", Decimal("2"))
    return ledger
