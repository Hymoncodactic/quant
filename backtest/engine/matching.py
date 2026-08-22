"""Venue-neutral matching rules: whether an order trades against this bar, and
at what raw price before any venue cost adjustment.

Responsibility: the pure trigger and price logic of
docs/backtest/framework/03_order_lifecycle.md section 2.1. Three conventions hold
throughout: the intra-bar sequence is open, high, low, close, which is the
NautilusTrader convention adopted in
docs/backtest/framework/01_architecture.md section 3.3; an ambiguous bar resolves
against the strategy; prices are returned as Decimal in the bar's quote
currency. Two conservative rules follow from that. A resting limit order fills
only when the bar trades strictly through the limit, because a bare touch
rarely clears a queue and granting the fill would systematically harvest bar
extremes. A stop order that gaps fills at the open, which is the side worse for
the strategy.

Out of scope: spread, slippage, fees, taxes, volume caps, latency and fault
injection. The broker simulator applies all of those on top of the raw price
returned here (backtest/t212/broker_sim.py, backtest/t212/costs.py,
backtest/t212/faults.py).

Public functions:
    match_market(bar)                           Raw fill price of a market
                                                order eligible on this bar: the
                                                bar open.
    match_limit(is_buy, limit, bar)             Raw fill price of a limit
                                                order, or None when it does not
                                                fill.
    match_stop(is_buy, stop, bar)               Raw fill price of a stop order
                                                after a last-traded-price
                                                trigger, or None.
    match_stop_limit(is_buy, stop, limit, bar)  Two-step stop-limit
                                                evaluation; returns
                                                (triggered, raw price or None).

Constants: None.

Inputs: None.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["match_market", "match_limit", "match_stop", "match_stop_limit"]

from decimal import Decimal

from backtest.engine.types import Bar


def _d(value: float) -> Decimal:
    """Float price -> Decimal via its shortest string representation."""
    return Decimal(str(value))


def match_market(bar: Bar) -> Decimal:
    """A market order eligible on this bar trades at the bar open."""
    return _d(bar.open)


def match_limit(is_buy: bool, limit: Decimal, bar: Bar) -> Decimal | None:
    """Limit order evaluation. Returns the raw fill price or None.

    Buy: a gap open at or through the limit fills at the (better) open.
    Otherwise the bar must trade STRICTLY through the limit (low < limit) to
    fill, and then exactly at the limit -- never better. A bare touch
    (low == limit) does NOT fill: a resting order at the bar's exact extreme
    sits at the back of a queue that a one-tick touch rarely clears, so
    granting the fill would systematically harvest bar extremes
    (docs/backtest/framework/03_order_lifecycle.md section 2.1). Sell mirrors.
    """
    if is_buy:
        if _d(bar.open) <= limit:
            return _d(bar.open)
        if _d(bar.low) < limit:
            return limit
        return None
    if _d(bar.open) >= limit:
        return _d(bar.open)
    if _d(bar.high) > limit:
        return limit
    return None


def match_stop(is_buy: bool, stop: Decimal, bar: Bar) -> Decimal | None:
    """Stop order: triggers on last-traded-price touching the stop, then
    trades as a market order (T212 semantics: trigger is the LTP, OpenAPI v0
    StopRequest description).

    Buy stop triggers when price rises to the stop: if the bar opens at or
    above the stop the raw price is the open (gap through the stop trades at
    the worse open), otherwise if the high reaches it the raw price is the
    stop itself. Sell is the mirror. Conservative: gaps always fill at the
    open, i.e. the side worse for the strategy.
    """
    if is_buy:
        if _d(bar.open) >= stop:
            return _d(bar.open)
        if _d(bar.high) >= stop:
            return stop
        return None
    if _d(bar.open) <= stop:
        return _d(bar.open)
    if _d(bar.low) <= stop:
        return stop
    return None


def match_stop_limit(is_buy: bool, stop: Decimal, limit: Decimal,
                     bar: Bar) -> tuple[bool, Decimal | None]:
    """Stop-limit: once the stop triggers, a limit order works the same bar.

    Returns (triggered, raw_price_or_None). Two cases per side, both bounded
    by the O-H-L-C sequence so no pre-trigger print can justify a fill:

    Marketable leg (buy: limit >= stop; sell: limit <= stop -- the standard
    slippage-cap setup): the limit is immediately executable at the trigger,
    so a mid-bar trigger fills AT THE STOP TOUCH itself, never at a price the
    bar did not trade (the old behavior granted `limit` even outside the
    bar's range). An open through the stop is a gap: the limit leg then sees
    the whole bar via match_limit.

    Non-marketable leg (buy: limit < stop; sell: limit > stop): the fill
    needs the bar to come back STRICTLY through the limit AFTER the trigger
    (a bare touch does not clear a resting queue, same rule as match_limit).
    Under O-H-L-C, for a buy the trigger is on the way up (high) and the low
    prints after it, so low < limit is valid post-trigger evidence; for a
    sell the trigger is on the way down (low) and the only post-trigger print
    is the close, so the high must NOT be used -- the fill requires
    close > limit.
    """
    if is_buy:
        opened_through = _d(bar.open) >= stop
        triggered = opened_through or _d(bar.high) >= stop
        if not triggered:
            return False, None
        if opened_through:
            return True, match_limit(True, limit, bar)
        if limit >= stop:
            return True, stop
        return True, (limit if _d(bar.low) < limit else None)
    opened_through = _d(bar.open) <= stop
    triggered = opened_through or _d(bar.low) <= stop
    if not triggered:
        return False, None
    if opened_through:
        return True, match_limit(False, limit, bar)
    if limit <= stop:
        return True, stop
    return True, (limit if _d(bar.close) > limit else None)
