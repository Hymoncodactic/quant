"""Ledger: GBP cash, positions at average cost, and the capital-occupancy
series that defines the performance denominator.

Responsibility: exact cash and position accounting in Decimal, covering cash
reservations for pending buys, booking of fills, average-cost release of basis
on sells, mark-to-market valuation, and the per-step equity and occupancy
record that the metrics layer consumes. Cash is a single GBP balance by design:
the venue API settles every order in the primary account currency
(docs/backtest/framework/01_architecture.md section 4, official source cited there),
so no foreign-currency balance can exist in an API-driven account. The book is
long-only because the Invest account is a cash account; the broker rejects
sells beyond holdings and the ledger enforces the same invariant defensively.
Money discipline: every amount is Decimal (quant-code-standards section 5.1),
and floats appear only in the mark-to-market records consumed by metrics and
plots.

Out of scope: deciding which orders fill and what they cost, which belongs to
the broker simulator (backtest/t212/broker_sim.py); computing performance
statistics, which belongs to metrics.py; writing the record to disk, which
belongs to results.py.

Public classes:
    Position   One open position: quantity in shares plus the total GBP outlay
               attributed to it under average-cost accounting, which is what
               capital occupied by this position means in the performance
               denominator (docs/backtest/framework/05_metrics_reporting.md
               section 1).
    Ledger     Cash, reservations, fill booking, occupancy and the equity
               record. mark() appends one row per step, holding both the mid
               mark and the liquidation mark; records_frame() returns the rows
               as a DataFrame.

Constants:
    ZERO   Decimal   Decimal("0"), the module's single zero literal, so no
           float zero can leak into money arithmetic.

Inputs: None.
Outputs: None. The equity record is returned in memory by records_frame().

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["Position", "Ledger"]

from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from backtest.engine.types import Fill

ZERO = Decimal("0")


@dataclass
class Position:
    """One open position: signed-free (long-only) quantity and its GBP cost.

    cost_basis_gbp is the total GBP outlay (fees included) attributed to the
    open quantity under average-cost accounting; it is what "capital occupied
    by this position" means in the performance denominator
    (docs/backtest/framework/05_metrics_reporting.md section 1).
    """
    qty: Decimal = ZERO
    cost_basis_gbp: Decimal = ZERO


@dataclass
class Ledger:
    initial_cash_gbp: Decimal
    cash_gbp: Decimal = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    _reserved: dict[int, Decimal] = field(default_factory=dict)
    buy_outlays_gbp: list[Decimal] = field(default_factory=list)
    _records: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.initial_cash_gbp <= 0:
            raise ValueError("initial cash must be positive")
        self.cash_gbp = self.initial_cash_gbp

    # ------------------------------------------------------------------
    # [1] Reservations (pending buy orders freeze cash: AccountSummary.Cash
    #     .reservedForOrders in the venue API)
    # ------------------------------------------------------------------

    def reserve(self, order_id: int, amount_gbp: Decimal) -> None:
        if amount_gbp < ZERO:
            raise ValueError("reservation must be non-negative")
        self._reserved[order_id] = self._reserved.get(order_id, ZERO) + amount_gbp

    def release(self, order_id: int) -> None:
        self._reserved.pop(order_id, None)

    @property
    def reserved_gbp(self) -> Decimal:
        return sum(self._reserved.values(), ZERO)

    @property
    def available_cash_gbp(self) -> Decimal:
        """Cash usable for NEW orders: settled cash minus reservations."""
        return self.cash_gbp - self.reserved_gbp

    # ------------------------------------------------------------------
    # [2] Fills
    # ------------------------------------------------------------------

    def apply_fill(self, fill: Fill) -> None:
        """Book one execution: move cash, update position and cost basis."""
        pos = self.positions.setdefault(fill.symbol, Position())
        if fill.quantity > ZERO:
            outlay = -fill.cash_delta_gbp
            if outlay <= ZERO:
                raise ValueError("buy fill must consume cash")
            pos.cost_basis_gbp += outlay
            self.buy_outlays_gbp.append(outlay)
        else:
            sold = -fill.quantity
            if sold > pos.qty:
                raise ValueError(
                    f"{fill.symbol}: sell {sold} exceeds held {pos.qty}")
            # Average-cost release: the sold fraction carries its share of basis.
            pos.cost_basis_gbp -= pos.cost_basis_gbp * sold / pos.qty
        pos.qty += fill.quantity
        self.cash_gbp += fill.cash_delta_gbp
        if self.cash_gbp < ZERO:
            raise ValueError("cash went negative; broker admission check failed")
        if pos.qty == ZERO:
            pos.cost_basis_gbp = ZERO

    def position_qty(self, symbol: str) -> Decimal:
        pos = self.positions.get(symbol)
        return pos.qty if pos else ZERO

    # ------------------------------------------------------------------
    # [3] Mark-to-market and occupancy
    # ------------------------------------------------------------------

    @property
    def invested_cost_gbp(self) -> Decimal:
        return sum((p.cost_basis_gbp for p in self.positions.values()), ZERO)

    @property
    def occupied_gbp(self) -> Decimal:
        """Capital in use now: open-position cost basis plus frozen cash."""
        return self.invested_cost_gbp + self.reserved_gbp

    def equity_gbp(self, prices_gbp: dict[str, Decimal]) -> Decimal:
        """Cash plus positions at the given GBP mid prices.

        Raises when a held symbol lacks a price: valuing a position at zero or
        at a stale unknown would silently corrupt the equity curve.
        """
        total = self.cash_gbp
        for symbol, pos in self.positions.items():
            if pos.qty == ZERO:
                continue
            if symbol not in prices_gbp:
                raise ValueError(f"no valuation price for held symbol {symbol}")
            total += pos.qty * prices_gbp[symbol]
        return total

    def mark(self, step: int, key: pd.Timestamp,
             prices_gbp: dict[str, Decimal],
             prices_liq_gbp: dict[str, Decimal] | None = None) -> None:
        """Append one row to the equity/occupancy record.

        prices_liq_gbp values positions at what a market EXIT would fetch
        (bid side, conversion fee, sell taxes -- the venue adapter builds
        them). The mid mark stays the diagnostic column; drawdown and final
        equity in the authoritative tier read the liquidation column
        (docs/backtest/framework/05_metrics_reporting.md). Without a liquidation
        valuer the column equals the mid mark.
        """
        equity_mid = float(self.equity_gbp(prices_gbp))
        equity_liq = float(self.equity_gbp(prices_liq_gbp)) \
            if prices_liq_gbp is not None else equity_mid
        self._records.append({
            "step": step,
            "ts": key,
            "cash_gbp": float(self.cash_gbp),
            "reserved_gbp": float(self.reserved_gbp),
            "invested_cost_gbp": float(self.invested_cost_gbp),
            "occupied_gbp": float(self.occupied_gbp),
            "equity_gbp": equity_mid,
            "equity_liq_gbp": equity_liq,
        })

    def records_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self._records)
