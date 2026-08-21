"""Order admission checks for the T212 broker simulator.

Responsibility: the fixed-order submission checks of
fixplans/framework/03_order_lifecycle.md section 2.2, plus the worst-case
buy-cost estimate that those checks and the cash reservation logic share. The
checks run in one place and in one order: fault windows and universe, zero
quantity, quantity precision, market data presence, minimum order value, then
the sell branch (owned quantity net of pending sells, residual above minimum)
or the buy branch (estimated cost against effective buying power), then the
pending-order cap and the per-bar submission pacing.

Out of scope: order lifecycle, matching and fills, which belong to
backtest/t212/broker_sim.py; the cost arithmetic itself, which belongs to
backtest/t212/costs.py; fault parameters and their evaluation, which belong to
backtest/t212/faults.py; cash and position accounting, which belongs to
backtest/engine/ledger.py.

Public functions:
    admission_reason(broker, spec, key, step, ledger)
        The first failing check as a reason string, or None when the order is
        admitted. Reason strings follow the venue's observed error vocabulary.
    estimated_buy_cost(broker, spec, bar, key)
        Worst-case GBP cost of a buy, used for admission and for the cash
        reservation. The reference price depends on order type: a limit or
        stop-limit caps the spend at its limit; a buy STOP can only execute at
        or above its stop, so the estimate takes max(stop, last close); a
        market order takes the last close.
    mid_gbp(broker, bar, key)
        The bar's close converted to GBP at mid, for valuation only, no FX fee.

Constants:
    ZERO
        Decimal("0"). Not a tunable parameter; it exists so Decimal
        comparisons never mix in a float.

Inputs: None. Pure computation over the broker, ledger and bar objects passed
    in; no file or network access.
Outputs: None.

Change log:
    2026-08-20  Split out of broker_sim.py to respect the 400-line module cap
                (quant-code-standards section 4.2). The functions take the
                broker as an explicit collaborator rather than duplicating its
                state.
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["admission_reason", "estimated_buy_cost", "mid_gbp"]

from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd

from backtest.engine.ledger import Ledger
from backtest.engine.types import Bar, OrderSpec, OrderType
from backtest.t212.costs import apply_spread, fill_cash_and_costs, price_to_gbp
from backtest.t212.instruments import security_kind

if TYPE_CHECKING:
    from backtest.t212.broker_sim import T212BrokerSim

ZERO = Decimal("0")


def mid_gbp(broker: "T212BrokerSim", bar: Bar, key: pd.Timestamp) -> Decimal:
    """The bar's close converted to GBP at mid (valuation, no FX fee)."""
    fx_mid = None
    if bar.quote_ccy == "USD":
        fx_mid = broker.fx.rate_at(broker.fx_query_ts(key))
    return price_to_gbp(Decimal(str(bar.close)), bar.quote_ccy, fx_mid)


def estimated_buy_cost(broker: "T212BrokerSim", spec: OrderSpec,
                       bar: Bar | None, key: pd.Timestamp) -> Decimal:
    """Worst-case GBP cost estimate used for admission and reservation.

    Reference price by type: a limit (or stop-limit) caps the spend at its
    limit; a buy STOP can only ever execute at or above its stop, so the
    estimate takes max(stop, last close); a market order takes the last
    close.
    """
    if bar is None:
        return ZERO
    if spec.limit_price is not None:
        ref = spec.limit_price
    elif spec.order_type is OrderType.STOP and spec.stop_price is not None:
        ref = max(spec.stop_price, Decimal(str(bar.close)))
    else:
        ref = Decimal(str(bar.close))
    exec_est = apply_spread(ref, True, broker.half_spread(spec.symbol, key),
                            broker.cost_cfg.slippage_bps)
    fx_mid = None
    if bar.quote_ccy == "USD":
        fx_mid = broker.fx.rate_at(broker.fx_query_ts(key))
    cash_delta, _, _ = fill_cash_and_costs(
        spec.quantity, exec_est, bar.quote_ccy, fx_mid,
        security_kind(spec.symbol), spec.symbol.endswith(".L"),
        broker.cost_cfg)
    return -cash_delta


def admission_reason(broker: "T212BrokerSim", spec: OrderSpec,
                     key: pd.Timestamp, step: int,
                     ledger: Ledger) -> str | None:
    """First failing admission check, in the fixed order of
    fixplans/framework/03_order_lifecycle.md section 2.2; None = admitted."""
    symbol, qty = spec.symbol, spec.quantity
    blocked = broker.faults.submit_blocked(broker.fx_query_ts(key), symbol,
                                           is_buy=qty > ZERO)
    if blocked:
        return blocked
    if qty == ZERO:
        return "zero_quantity"
    decimals = broker.faults.qty_decimals()
    if decimals is not None and -qty.normalize().as_tuple().exponent > decimals:
        return "quantity_precision_mismatch"
    bar = broker.last_bar(symbol)
    if bar is None:
        return "no_market_data"
    value_gbp = abs(qty) * mid_gbp(broker, bar, key)
    if value_gbp < broker.cost_cfg.min_order_value_gbp:
        return "order_value_below_minimum"
    if qty < ZERO:
        held = ledger.position_qty(symbol)
        available = held - broker.pending_sell_qty(symbol) \
            if broker.faults.cfg.on("F10_sell_reservation") else held
        if -qty > available:
            return "selling_equity_not_owned"
        residual = (held + qty) * mid_gbp(broker, bar, key)
        if ZERO < residual < broker.cost_cfg.min_order_value_gbp:
            return "residual_below_minimum"
    else:
        est = estimated_buy_cost(broker, spec, bar, key)
        power = broker.faults.effective_buying_power(ledger.available_cash_gbp)
        if est > power:
            return "insufficient_free_for_stocks_buy"
    if len(broker.open_orders(symbol)) >= broker.faults.cfg.max_pending_per_ticker:
        return "max_pending_orders_per_ticker"
    market_cap, pending_cap = broker.faults.submit_caps_per_bar()
    if spec.order_type is OrderType.MARKET:
        if broker.market_submits >= market_cap:
            return "pacing_deferred"
    elif broker.pending_submits >= pending_cap:
        return "pacing_deferred"
    return None
