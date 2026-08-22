"""Core data structures for the event-driven backtest engine.

Responsibility: the enums and records shared by every engine module and every
venue adapter, namely the order type, order status and time-validity enums, one
OHLCV bar, a strategy-side order request, broker-side order state, one
execution with its itemized costs, and the complete run configuration. Order
semantics mirror the Trading 212 Public API v0 contract (a sell is a negative
quantity, validity is DAY or GOOD_TILL_CANCEL, orders are quantity-only),
verified 2026-08-20 from data/reference/t212_openapi_v0_20260820.yaml. Design
source: docs/backtest/framework/01_architecture.md section 2.

Out of scope: all behavior. Matching belongs to matching.py, cash and position
accounting to ledger.py, sequencing to engine.py, and venue fee and tax rules
to backtest/t212/costs.py.

Public classes:
    OrderType      Order types offered by the venue: MARKET, LIMIT, STOP,
                   STOP_LIMIT.
    OrderStatus    Simulator lifecycle states; the venue's eleven states are
                   reduced to five because the pre-activation ones are not
                   distinguishable at bar granularity.
    TimeInForce    DAY, which expires at exchange-local midnight, or
                   GOOD_TILL_CANCEL.
    Bar            One OHLCV bar; prices are floats in the source quote
                   currency and ts is the bar open time.
    OrderSpec      Strategy-side order request; quantity is signed, in shares.
    Order          Broker-side order state, mutated only by the broker
                   simulator; exposes remaining_qty and is_open.
    Fill           One execution, with the signed GBP cash movement and the
                   itemized cost breakdown keyed by the venue's tax names.
    EngineConfig   Complete configuration of one run, serialized in full into
                   the result metadata.

Constants:
    INTERVAL_SECONDS   dict[str, int]   Seconds per bar for every stored
                       interval: 1m, 2m, 5m, 1h, 1d. Source:
                       docs/data/t212/DATA_SPEC.md section 5. Order eligibility
                       is measured in time rather than in merged-timeline
                       steps, because mixed-exchange intraday grids interleave
                       (US 1h bars on the half hour, LSE on the hour) and one
                       step is therefore not one interval.

Inputs: None.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
    2026-08-22  EngineConfig.fill_timing (next_open | same_close) and
                Fill.at_close added for the last-minute-before-close
                execution convention (user ruling 2026-08-22).
"""

from __future__ import annotations

__all__ = ["OrderType", "OrderStatus", "TimeInForce", "Bar", "OrderSpec",
           "Order", "Fill", "EngineConfig", "INTERVAL_SECONDS"]

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

import pandas as pd

# Seconds per bar for every stored interval (docs/data/t212/DATA_SPEC.md
# section 5). Order eligibility is measured in TIME, not merged-timeline
# steps: mixed-exchange intraday grids interleave (US 1h bars on :30, LSE on
# :00), so a step is not an interval.
INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60, "2m": 120, "5m": 300, "1h": 3600, "1d": 86400,
}


# ============================================================================
# [1] Enums
# ============================================================================

class OrderType(Enum):
    """Order types offered by the T212 public API (OpenAPI v0 paths)."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(Enum):
    """Simulator lifecycle states.

    The real venue exposes 11 states (see docs/backtest/framework/03_order_lifecycle.md
    section 1.3). The pre-activation states LOCAL/UNCONFIRMED/CONFIRMED are not
    distinguishable at bar granularity and are represented by the latency model
    instead; REPLACING/REPLACED have no API endpoint and are not simulated.
    """
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class TimeInForce(Enum):
    """DAY expires at midnight in the exchange's time zone (OpenAPI v0,
    components.schemas.TimeValidity); GTC stays until filled or canceled."""
    DAY = "DAY"
    GOOD_TILL_CANCEL = "GOOD_TILL_CANCEL"


# ============================================================================
# [2] Market data
# ============================================================================

@dataclass(frozen=True)
class Bar:
    """One OHLCV bar.

    Prices are floats in the SOURCE quote currency (USD, GBP or GBp pence);
    conversion to GBP happens only in the ledger and cost layer. ts is the bar
    OPEN time in UTC for intraday data and the exchange-local midnight for
    daily data (verified 2026-08-20, docs/backtest/framework/02_data_layer.md
    section 3).
    """
    ts: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_ccy: str


# ============================================================================
# [3] Orders and fills
# ============================================================================

@dataclass(frozen=True)
class OrderSpec:
    """Strategy-side order request.

    quantity is SIGNED per the venue convention: negative sells, positive buys
    (OpenAPI v0, Key Concepts > Selling Orders). Unit: shares.
    """
    symbol: str
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: TimeInForce = TimeInForce.DAY


@dataclass
class Order:
    """Broker-side order state. Mutated only by the broker simulator."""
    order_id: int
    spec: OrderSpec
    submitted_ts: pd.Timestamp
    submitted_step: int
    # First timeline KEY (a time, never a step count) at which the order may
    # match. Steps are not intervals on a mixed-exchange timeline.
    eligible_ts: pd.Timestamp
    status: OrderStatus = OrderStatus.NEW
    filled_qty: Decimal = Decimal("0")   # signed, same sign as spec.quantity
    filled_principal_gbp: Decimal = Decimal("0")  # cumulative |principal|, GBP
    reserved_gbp: Decimal = Decimal("0")  # cash frozen for a pending buy
    cancel_requested: bool = False
    reason: str = ""                # reject / expiry / cancel detail for audit
    triggered: bool = False         # STOP / STOP_LIMIT armed into their leg
    ptm_charged: bool = False       # PTM levy is once per order, not per fill
    submitted_local_date: str = ""  # exchange-local date at submission (DAY expiry)

    @property
    def remaining_qty(self) -> Decimal:
        """Unfilled signed quantity, shares."""
        return self.spec.quantity - self.filled_qty

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)


@dataclass(frozen=True)
class Fill:
    """One execution.

    price is in the instrument's quote currency; cash_delta_gbp is the signed
    GBP cash movement including every itemized cost; costs_gbp keys follow the
    venue's Tax.name enum in lower case (currency_conversion_fee,
    stamp_duty_reserve_tax, ptm_levy, finra_fee, transaction_fee) so a backtest
    cost column can be reconciled line-by-line against the real account's
    GET /equity/history/orders walletImpact.taxes.
    """
    order_id: int
    symbol: str
    ts: pd.Timestamp
    step: int
    quantity: Decimal               # signed, shares
    price: Decimal                  # quote currency per share, spread included
    quote_ccy: str
    fx_mid: Decimal | None          # GBPUSD mid used, None for GBP/GBp fills
    cash_delta_gbp: Decimal         # signed GBP cash movement, costs included
    costs_gbp: dict[str, Decimal] = field(default_factory=dict)
    # True when executed at the decision bar's CLOSE under fill_timing ==
    # "same_close" (the last-minute-before-close convention); such a fill
    # legitimately shares its step with the submission.
    at_close: bool = False


# ============================================================================
# [4] Run configuration
# ============================================================================

@dataclass
class EngineConfig:
    """Complete configuration of one backtest run.

    Everything here is serialized into the result metadata; a result file
    without its full configuration is unusable by decree
    (docs/backtest/framework/05_metrics_reporting.md section 3.3).
    """
    symbols: list[str]
    interval: str                   # "1d", "1h", "5m", "2m", "1m"
    start: str                      # inclusive, "YYYY-MM-DD"
    end: str                        # inclusive, "YYYY-MM-DD"
    initial_cash_gbp: Decimal
    arm: str                        # arm label, goes into the result file name
    fee_tier: str = "worst"         # "worst" | "actual" (fixplans 04 section 6)
    seed: int = 20260820
    lookahead_probe: bool = False   # diagnostic only; results are stamped PROBE
    # Execution timing of MARKET orders generated from a decision at bar t:
    #   "next_open"  fill at the next bar's open (conservative default,
    #                backtest-discipline hard list items 1-2);
    #   "same_close" fill at bar t's close -- the order is placed in the last
    #                minute of the session and the signal is computed on the
    #                close (user ruling 2026-08-22, research/decisions/
    #                20260822_close_execution_timing.md). A deviation from
    #                the hard list that must be declared in the strategy's
    #                prereg; it pays the calibrated close-proximity gap
    #                (CostConfig.close_gap_bps) and only succeeds when the
    #                latency draw fits in CostConfig.close_window_sec,
    #                otherwise the order falls back to the next open.
    fill_timing: str = "next_open"
    # Hard guard against zombie holdings: a HELD symbol whose feed produces
    # no bar for more than this many calendar days (while the timeline keeps
    # advancing) aborts the run instead of marking the position at its last
    # price forever -- a suspended or delisted holding never losing value is
    # survivorship bias in valuation form (backtest-discipline hard list
    # item 8). Wall-time, not key counts: cross-exchange intraday timelines
    # legitimately skip hundreds of keys while one venue is closed.
    max_stale_days_with_position: int = 5
    strategy_name: str = "unnamed"
    strategy_version: str = "0.0.1"
    params: dict[str, Any] = field(default_factory=dict)
