"""Venue-neutral matching rules: does an order trade against this bar, and at
what raw price, before any venue cost adjustment.

Responsibility: the pure trigger/price logic of
fixplans/framework/03_order_lifecycle.md section 2.1.
Not responsible for: spread, slippage, fees, volume caps, latency (broker
simulator applies those on top of the raw price returned here).

Conventions:
    - Intra-bar sequence is O -> H -> L -> C (NautilusTrader convention,
      fixplans/framework/01_architecture.md section 3.3).
    - An ambiguous bar resolves against the strategy.
    - Prices returned as Decimal in the bar's quote currency.

Public functions:
    match_market(bar)                       Raw fill price of a market order
    match_limit(is_buy, limit, bar)         Trigger and raw price of a limit order
    match_stop(is_buy, stop, bar)           Trigger and raw price of a stop order
    match_stop_limit(is_buy, stop, limit, bar)  Two-step stop-limit evaluation
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

    Buy: fills when the bar trades at or below the limit. If the bar OPENS
    below the limit the fill is the (better) open; otherwise, if the low
    reaches the limit, the fill is exactly the limit -- never better, which is
    the conservative reading of an OHLC bar. Sell is the mirror image.
    """
    if is_buy:
        if _d(bar.open) <= limit:
            return _d(bar.open)
        if _d(bar.low) <= limit:
            return limit
        return None
    if _d(bar.open) >= limit:
        return _d(bar.open)
    if _d(bar.high) >= limit:
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
    needs the bar to come back through the limit AFTER the trigger. Under
    O-H-L-C, for a buy the trigger is on the way up (high) and the low prints
    after it, so low <= limit is valid post-trigger evidence; for a sell the
    trigger is on the way down (low) and the only post-trigger print is the
    close, so the high must NOT be used -- the fill requires close >= limit.
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
        return True, (limit if _d(bar.low) <= limit else None)
    opened_through = _d(bar.open) <= stop
    triggered = opened_through or _d(bar.low) <= stop
    if not triggered:
        return False, None
    if opened_through:
        return True, match_limit(False, limit, bar)
    if limit <= stop:
        return True, stop
    return True, (limit if _d(bar.close) >= limit else None)
