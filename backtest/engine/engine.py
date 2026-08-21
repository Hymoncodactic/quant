"""Backtest main loop: settle fills, guard the timeline, call the strategy,
submit the position diff as orders, then mark to market.

Responsibility: sequencing and the online no-lookahead assertions of
fixplans/validation/01_no_lookahead.md. The per-step order is fixed at four
steps and the order itself is a ruling, not an implementation detail:
    1. broker.process_bar() settles orders submitted at earlier steps.
    2. strategy(view, portfolio, params) sees history up to and including now.
    3. The diff between targets and held plus pending becomes market orders,
       whose earliest fill is one full interval later.
    4. ledger.mark() samples valuation and occupancy AFTER the submissions, so
       cash frozen at this step enters the capital peak; marking earlier would
       let a reservation that resolves next bar escape the denominator
       (fixplans/framework/05_metrics_reporting.md section 1.1).
Two assertions run always: a fill in the same step as its submission is a
lookahead bug, and on intraday data a fill bar must not open before the
submission bar's close exists. A held symbol whose feed stops producing bars
for longer than the configured wall-time threshold aborts the run, because a
position marked forever at its last pre-suspension price is survivorship bias
in valuation form. The strategy is a pure function per ARCHITECTURE.md
section 2.0: strategy(view, portfolio, params) returns target shares per
symbol.

Out of scope: matching rules (matching.py), costs, taxes and fault injection
(backtest/t212/), cash and position accounting (ledger.py), performance
statistics (metrics.py), persistence (results.py). Currency conversion and
liquidation valuation are injected by the venue runner as to_base_ccy and
to_liquidation, so the engine holds no venue currency rules.

Public classes and functions:
    PortfolioView      Read-only account state handed to the strategy: never
                       the ledger object itself.
    RunResult          Trades, equity and orders frames plus run metadata of
                       one finished run.
    BacktestEngine     run() drives the whole loop; construct one per run, it
                       is not reusable. last_bars exposes the latest bar per
                       symbol so the venue runner can value end-of-run
                       positions.
    BaseCcyConverter   Type alias, not in __all__: (price, quote_ccy, fx_mid)
                       returns the price in the account currency.
    LiquidationValuer  Type alias, not in __all__: (symbol, bar, key) returns
                       the per-share exit value in the account currency, on
                       the bid side and after conversion fee and sell taxes.
                       Optional and venue-supplied.
    StrategyFn         Type alias, not in __all__: the strategy signature;
                       imported by strategy_loader.py.

Constants:
    ZERO         Decimal   Decimal("0"), the module's single zero literal for
                 quantity comparisons.
    _QTY_STEP    Decimal   Decimal("0.00000001"), that is eight decimal
                 places. Fallback order-quantity step used only when the
                 broker exposes no order_quantity_step(); when it does, the
                 venue value wins, for example four decimal places under the
                 T212 precision fault. Source unknown, needs verification.

Inputs: None. Bars arrive through the injected BarFeed and FX rates through
    the injected FxSeries; this module opens no file and makes no network call.
Outputs: None. Frames are returned in memory as RunResult.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["PortfolioView", "RunResult", "BacktestEngine"]

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable

import pandas as pd

from backtest.engine.feed import BarFeed, FxSeries, MarketView
from backtest.engine.ledger import Ledger
from backtest.engine.metrics import naive_utc
from backtest.engine.types import (INTERVAL_SECONDS, Bar, EngineConfig, Fill,
                                   OrderSpec, OrderStatus)

ZERO = Decimal("0")
_QTY_STEP = Decimal("0.00000001")

# to_base_ccy(price, quote_ccy, fx_mid) -> price in the account currency.
# Injected by the venue runner so the engine holds no venue currency rules.
BaseCcyConverter = Callable[[Decimal, str, Decimal | None], Decimal]
# to_liquidation(symbol, bar, key) -> per-share EXIT value in the account
# currency (bid side, conversion fee, sell taxes). Optional; venue-supplied.
LiquidationValuer = Callable[[str, Bar, pd.Timestamp], Decimal]
StrategyFn = Callable[[MarketView, "PortfolioView", dict], dict[str, Decimal]]


@dataclass(frozen=True)
class PortfolioView:
    """Account state a strategy may read: never the ledger object itself."""
    cash_gbp: Decimal
    available_cash_gbp: Decimal
    positions: dict[str, Decimal]
    pending_signed_qty: dict[str, Decimal]


@dataclass
class RunResult:
    trades: pd.DataFrame
    equity: pd.DataFrame
    orders: pd.DataFrame
    meta: dict[str, Any] = field(default_factory=dict)


class BacktestEngine:
    """One backtest run. Construct fresh per run; not reusable."""

    def __init__(self, config: EngineConfig, feed: BarFeed, fx: FxSeries,
                 broker, strategy: StrategyFn,
                 to_base_ccy: BaseCcyConverter,
                 to_liquidation: LiquidationValuer | None = None) -> None:
        self.config = config
        self.feed = feed
        self.fx = fx
        self.broker = broker
        self.strategy = strategy
        self.to_base = to_base_ccy
        self.to_liquidation = to_liquidation
        self.ledger = Ledger(initial_cash_gbp=config.initial_cash_gbp)
        self.fills: list[Fill] = []
        self._last_bar: dict[str, Bar] = {}
        self._last_seen_key: dict[str, pd.Timestamp] = {}

    # ------------------------------------------------------------------
    # [1] Main loop
    # ------------------------------------------------------------------

    def run(self) -> RunResult:
        for step, key, bars in self.feed:
            fills = self.broker.process_bar(step, key, bars, self.ledger)
            self._assert_fill_timing(fills)
            self.fills.extend(fills)
            self._last_bar.update(bars)
            self._guard_stale_positions(key, bars)
            view = self._market_view(key, bars)
            targets = self.strategy(view, self._portfolio_view(), self.config.params)
            for spec in self._diff_to_specs(targets):
                self.broker.submit(spec, key, step, self.ledger)
            # Mark AFTER submissions so cash frozen for orders placed this
            # step enters the occupancy series; marking before would let a
            # reservation that resolves next bar escape the capital peak
            # (fixplans/framework/05_metrics_reporting.md section 1.1).
            self.ledger.mark(step, key, self._valuation_prices(key),
                             self._liquidation_prices(key))
        return self._collect()

    @property
    def last_bars(self) -> dict[str, Bar]:
        """Latest bar seen per symbol; the venue runner values end-of-run
        positions (liquidation estimate) from these."""
        return dict(self._last_bar)

    # ------------------------------------------------------------------
    # [2] Per-step helpers
    # ------------------------------------------------------------------

    def _assert_fill_timing(self, fills: list[Fill]) -> None:
        """Online lookahead guards, always on (fixplans/validation/
        01_no_lookahead.md section 1.2).

        Step guard: a fill in the same step its order was submitted is a
        lookahead bug, full stop (backtest-discipline section 2.1).
        Time guard (intraday): the decision at the submission key used that
        bar's close, which only exists one full interval later; a fill bar
        must not OPEN before that instant. Steps alone cannot express this on
        a mixed-exchange timeline whose consecutive keys interleave closer
        than one interval.
        """
        interval = pd.Timedelta(seconds=INTERVAL_SECONDS[self.config.interval])
        daily = self.config.interval == "1d"
        for fill in fills:
            order = self.broker.orders[fill.order_id]
            if fill.step <= order.submitted_step:
                raise AssertionError(
                    f"same-bar fill: order {order.order_id} submitted at step "
                    f"{order.submitted_step}, filled at step {fill.step}")
            if not daily and fill.ts < order.submitted_ts + interval:
                raise AssertionError(
                    f"fill before information exists: order {order.order_id} "
                    f"decided at {order.submitted_ts} (close known "
                    f"{order.submitted_ts + interval}), fill bar opens "
                    f"{fill.ts}")

    def _valuation_prices(self, key: pd.Timestamp) -> dict[str, Decimal]:
        """GBP mid prices of every symbol seen so far, at the latest close."""
        query = key.tz_localize("UTC") if key.tzinfo is None else key
        prices: dict[str, Decimal] = {}
        for symbol, bar in self._last_bar.items():
            fx_mid = self.fx.rate_at(query) if bar.quote_ccy == "USD" else None
            prices[symbol] = self.to_base(Decimal(str(bar.close)),
                                          bar.quote_ccy, fx_mid)
        return prices

    def _liquidation_prices(self, key: pd.Timestamp) -> dict[str, Decimal] | None:
        """Per-share exit values from the venue's liquidation valuer."""
        if self.to_liquidation is None:
            return None
        return {symbol: self.to_liquidation(symbol, bar, key)
                for symbol, bar in self._last_bar.items()}

    def _guard_stale_positions(self, key: pd.Timestamp,
                               bars: dict[str, Bar]) -> None:
        """Abort when a held symbol's feed has died (wall-time threshold).

        A position marked forever at its last pre-suspension price is
        survivorship bias in valuation form; the run must fail loudly and be
        handled explicitly, never priced silently. Weekends and cross-venue
        closed hours stay far below the day threshold by construction.
        """
        now = naive_utc(key)
        limit = pd.Timedelta(days=self.config.max_stale_days_with_position)
        for symbol, pos in self.ledger.positions.items():
            if pos.qty == ZERO:
                self._last_seen_key.pop(symbol, None)
                continue
            if symbol in bars:
                self._last_seen_key[symbol] = now
                continue
            last_seen = self._last_seen_key.get(symbol)
            if last_seen is not None and now - last_seen > limit:
                raise RuntimeError(
                    f"held symbol {symbol} produced no bar since {last_seen} "
                    f"(now {now}, threshold {limit}); feed died while a "
                    f"position is open -- handle the suspension explicitly")

    def _market_view(self, key: pd.Timestamp,
                     bars: dict[str, Bar]) -> MarketView:
        probe = None
        if self.config.lookahead_probe:
            probe = {s: b for s in bars
                     if (b := self.feed.peek(s)) is not None}
        return MarketView(self.feed.history, key, probe)

    def _portfolio_view(self) -> PortfolioView:
        positions = {s: p.qty for s, p in self.ledger.positions.items()
                     if p.qty != ZERO}
        pending = {s: q for s in set(list(positions) + list(self._last_bar))
                   if (q := self.broker.pending_signed_qty(s)) != ZERO}
        return PortfolioView(cash_gbp=self.ledger.cash_gbp,
                             available_cash_gbp=self.ledger.available_cash_gbp,
                             positions=positions, pending_signed_qty=pending)

    def _diff_to_specs(self, targets: dict[str, Decimal]) -> list[OrderSpec]:
        """Target shares minus (held + pending) becomes market orders.

        Deltas are floored to the VENUE's order-quantity step (the broker
        knows it; e.g. 4 dp under the T212 precision fault) so a fractional
        target is dust-truncated once instead of being resubmitted and
        rejected on every bar. Sub-step dust is ignored rather than churned.
        """
        step_fn = getattr(self.broker, "order_quantity_step", None)
        qty_step = step_fn() if step_fn else _QTY_STEP
        specs: list[OrderSpec] = []
        for symbol, target in targets.items():
            current = self.ledger.position_qty(symbol) \
                + self.broker.pending_signed_qty(symbol)
            delta = target - current
            sign = 1 if delta > ZERO else -1
            magnitude = abs(delta).quantize(qty_step, rounding=ROUND_DOWN)
            if magnitude == ZERO:
                continue
            specs.append(OrderSpec(symbol=symbol, quantity=sign * magnitude))
        return specs

    # ------------------------------------------------------------------
    # [3] Collection
    # ------------------------------------------------------------------

    def _collect(self) -> RunResult:
        # Each trade row is self-describing (fixplans/framework/
        # 05_metrics_reporting.md section 4.2): submission time, side and the
        # order's terminal state ride along with the fill.
        trades = pd.DataFrame([{
            "order_id": f.order_id, "symbol": f.symbol, "ts": f.ts,
            "step": f.step, "quantity": float(f.quantity),
            "side": "BUY" if f.quantity > ZERO else "SELL",
            "submitted_ts": self.broker.orders[f.order_id].submitted_ts,
            "order_status": self.broker.orders[f.order_id].status.value,
            "order_reason": self.broker.orders[f.order_id].reason,
            "price": float(f.price), "quote_ccy": f.quote_ccy,
            "fx_mid": float(f.fx_mid) if f.fx_mid is not None else None,
            "cash_delta_gbp": float(f.cash_delta_gbp),
            **{f"cost_{k}": float(v) for k, v in f.costs_gbp.items()},
        } for f in self.fills])
        orders = pd.DataFrame([{
            "order_id": o.order_id, "symbol": o.spec.symbol,
            "quantity": float(o.spec.quantity),
            "type": o.spec.order_type.value, "status": o.status.value,
            "reason": o.reason, "submitted_step": o.submitted_step,
            "eligible_ts": str(o.eligible_ts),
            "filled_qty": float(o.filled_qty),
        } for o in self.broker.orders.values()])
        equity = self.ledger.records_frame()
        rejected = int((orders["status"] == OrderStatus.REJECTED.value).sum()) \
            if not orders.empty else 0
        meta = {
            "fills": len(self.fills),
            "orders_total": len(orders),
            "orders_rejected": rejected,
            "buy_outlays_gbp": [str(x) for x in self.ledger.buy_outlays_gbp],
            "final_cash_gbp": str(self.ledger.cash_gbp),
        }
        return RunResult(trades=trades, equity=equity, orders=orders, meta=meta)
