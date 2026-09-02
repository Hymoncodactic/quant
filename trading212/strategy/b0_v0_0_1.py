"""B0: A0 and A1 sharing one Trading 212 account, A0 first, A1 absorbing the rest.

Responsibility: hold THE single copy of the capital-sharing rule of
trading212/strategy/b0_spec.md. B0 introduces no signal of its own. It asks A0
which of its eighteen names it wants today, sizes those by A0's own slot rule
against WHOLE-account equity, and puts everything that is left into A1's
current twenty-name book. The account therefore sits near fully invested
instead of holding the roughly 45% cash A0 leaves idle.

Two facts make this more than a sum of the two modules:

    A0 cannot be asked for quantities.  A0 sizes slots from the equity IT can
        see -- cash plus its own eighteen names. In a shared account, once A1
        holds the capital that equity is near zero, a fresh slot floors to
        zero shares, and A0 reads as "does not want the name" when it means
        "cannot afford it". B0 therefore reads only the SET of names A0 wants,
        through a portfolio view with synthetic cash, and sizes them itself
        (b0_spec.md section 3.1).
    A0's active set is a DAILY fact.  Live, the view is hourly, so counting
        253 bars on it would span about thirty-six sessions and admit names
        that have no business holding a slot. The live path counts the
        injected daily rows instead, through the same synthetic daily view
        A0's own signal is computed on.

Overlap between the two name lists is resolved by the priority parameter:
"a1" (the default and the backtested reading) sizes a shared name by the A1
rule, "a0" by the A0 slot rule. Both are implemented and the diagnostics
report the attribution that was actually applied, never a guess from list
membership.

Out of scope: the A0 signal and its gates (trading212/strategy/a0_v0_0_1.py),
    the hourly timing shim (a0_intraday_v0_0_1.py), A1's admission, ranking
    and buffer band (a1_v0_0_1.py), what a session is and what the previous
    book was (both arrive in the injection, built by
    trading212/execution/market_data.py), and order submission
    (trading212/execution/).

Public functions:
    make_strategy(injection)                     Bind an injection, return the
                                                 strategy callable (seam S7).
    compute_targets(view, portfolio, params)     Plugin entry point; requires
                                                 params["injection"].
    signal_diagnostics(view, portfolio, params, injection)
                                                 The whole diagnostics tree
                                                 (seam S6), structure frozen in
                                                 fixplans/t212/b0/
                                                 00_coordination.md 2.6.

Public constants:
    STRATEGY_NAME     str  "b0". Must match the file name.
    STRATEGY_VERSION  str  "0.0.1". Same check.

Parameters, from trading212/config/strategies/b0_v0_0_1.yaml unless noted:
    priority              str      "a1" or "a0"; who sizes an overlapping name.
    a1_band               float    0.10. An A1 leg is left alone while it sits
                                   within this fraction of its recomputed
                                   target, so daily drift alone does not churn.
    slot_headroom         float    0.99, the cost buffer on both legs.
    signal_view_cash_gbp  int      1000000. Cash of the synthetic view used to
                                   READ A0's signal set. It never sizes
                                   anything.
    sells_first           bool     True live. Reductions are emitted before
                                   purchases so a sell's proceeds are visible
                                   to the cash check by the time the buys are
                                   examined. False reproduces the reference
                                   implementation's order.
    fx_symbol             str      "GBPUSD=X".
    live_from             str      "YYYY-MM-DD"; nothing is emitted before it.
    a0_params             dict     A0's own parameter mapping, injected by the
                                   entry layer with live_from overridden to
                                   B0's start date.
    a1_params             dict     A1's own parameter mapping, same override on
                                   live_from and rebalance_anchor.

Injection keys (fixplans/t212/b0/00_coordination.md section 2.3):
    a0_mode      "rows" live, "view" in the backtest. Chooses where A0's active
                 set and daily history come from.
    a0_rows      symbol -> [(iso_date, o, h, l, c), ...], live only.
    sessions     The US session list from the rebalance anchor onward.
    a1_rank / panel, rank_as_of, a1_book, thin, a1_frozen
                 Passed straight through to the A1 leg; see a1_v0_0_1.py.

Inputs: none. Outputs: none. No argument is mutated; the returned mapping is
    the only effect. Its INSERTION ORDER is part of the contract: the execution
    layer and the engine both submit in that order.

Change log:
    2026-09-03  Created from b0_spec.md and fixplans/t212/b0/02_strategy_b0.md.
"""

from __future__ import annotations

__all__ = ["STRATEGY_NAME", "STRATEGY_VERSION", "make_strategy",
           "compute_targets", "signal_diagnostics"]

import dataclasses
import sys
from datetime import date
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from trading212.strategy import a0_v0_0_1 as _a0            # noqa: E402
from trading212.strategy import a0_intraday_v0_0_1 as _a0i  # noqa: E402
from trading212.strategy import a1_v0_0_1 as _a1            # noqa: E402

STRATEGY_NAME = "b0"
STRATEGY_VERSION = "0.0.1"

_SHARE_STEP = Decimal("0.0001")
_ZERO = Decimal("0")
_TZ_NEW_YORK = "America/New_York"


# ============================================================================
# [1] Small helpers
# ============================================================================

def _shares(gbp: Decimal, fx: Decimal, price: Decimal) -> Decimal:
    """Whole-pound budget to venue-quantized shares; zero when unbuyable."""
    if price <= 0 or gbp <= 0:
        return _ZERO
    return (gbp * fx / price).quantize(_SHARE_STEP, rounding=ROUND_DOWN)


def _as_date(value) -> date:
    if isinstance(value, date) and not hasattr(value, "tz_convert"):
        return value
    import pandas as pd
    return pd.Timestamp(value).date()


def _view_date(view) -> date:
    ts = view.now
    try:
        if ts.tzinfo is not None:
            ts = ts.tz_convert(_TZ_NEW_YORK)
    except (TypeError, AttributeError):
        pass
    return ts.date()


def _synthetic_view(portfolio, params):
    """A portfolio lookalike with large cash, for READING A0's signal set.

    Positions and pending quantities are passed through untouched so the
    no-churn branch still reports the real held quantity; only the cash is
    synthetic, and it is never used to size anything (b0_spec.md 3.1). The
    real view is a frozen dataclass, so replace() copies rather than mutates
    and the caller's object cannot be polluted.
    """
    cash = Decimal(str(params.get("signal_view_cash_gbp", 1000000)))
    if dataclasses.is_dataclass(portfolio):
        return dataclasses.replace(portfolio, cash_gbp=cash,
                                   available_cash_gbp=cash)
    return _SignalView(cash, portfolio)


class _SignalView:
    """Fallback lookalike for a portfolio object that is not a dataclass."""

    def __init__(self, cash: Decimal, portfolio) -> None:
        self.cash_gbp = cash
        self.available_cash_gbp = cash
        self.positions = portfolio.positions
        self.pending_signed_qty = portfolio.pending_signed_qty


# ============================================================================
# [2] The two legs
# ============================================================================

def _a0_signal_set(view, portfolio, params, injection) -> set:
    """The names A0 wants today: its targets read through the synthetic view."""
    a0_params = params["a0_params"]
    synthetic = _synthetic_view(portfolio, params)
    if injection.get("a0_mode", "view") == "rows":
        shim = _a0i.daily_view(view, a0_params, injection["a0_rows"],
                               a0_params.get("exchange_tz", _TZ_NEW_YORK))
        targets = _a0.compute_targets(shim, synthetic, a0_params)
    else:
        targets = _a0.compute_targets(view, synthetic, a0_params)
    return {symbol for symbol, qty in targets.items() if qty > 0}


def _a0_active_and_prices(view, injection, params
                          ) -> tuple[list, dict[str, Decimal]]:
    """A0's active set and the decision prices for its names.

    Active means "has enough DAILY history to hold a slot", the same test
    a0_v0_0_1.compute_targets applies to the view it is given. Live that view
    is hourly, so the count comes from the injected daily rows strictly before
    today plus the session's own synthesized bar; in the backtest the view is
    already daily and is counted directly.

    Prices always come from the view, because the price that sizes an order is
    the one observable at the decision instant, not yesterday's close.
    """
    a0_params = params["a0_params"]
    names = list(a0_params["trade_symbols"])
    lookback = int(a0_params.get("tsmom_lookback", 252))
    trend_ma = int(a0_params.get("trend_ma", 200))

    if injection.get("a0_mode", "view") == "rows":
        today_iso = _view_date(view).isoformat()
        rows = injection.get("a0_rows") or {}
        active = [s for s in names
                  if len([r for r in (rows.get(s) or []) if r[0] < today_iso])
                  >= lookback]
    else:
        active = [s for s in names
                  if len(view.bars(s, max(lookback, trend_ma) + 1))
                  >= lookback + 1]

    prices: dict[str, Decimal] = {}
    for symbol in names:
        bar = view.bar(symbol)
        if bar is not None and bar.close > 0:
            prices[symbol] = Decimal(str(bar.close))
    return active, prices


def _split(s0: set, book: set, priority: str) -> tuple[set, set]:
    """(names sized by A0, names sized by A1) -- b0_spec.md section 3.2."""
    if priority == "a1":
        return s0 - book, set(book)
    return set(s0), book - s0


# ============================================================================
# [3] Equity and sizing
# ============================================================================

def _equity(view, portfolio, fx: Decimal) -> tuple[Decimal, dict[str, Decimal]]:
    """(whole-account equity in GBP, price cache) at the view's own prices.

    A holding whose symbol has no usable bar contributes zero. That is the
    conservative reading: an unpriceable holding cannot be sold at a
    remembered price either, and carrying a stale price into equity would
    inflate every slot in the same step.
    """
    prices: dict[str, Decimal] = {}
    equity = portfolio.cash_gbp
    for symbol, qty in portfolio.positions.items():
        bar = view.bar(symbol)
        if bar is not None and bar.close > 0:
            prices[symbol] = Decimal(str(bar.close))
            if qty:
                equity += qty * prices[symbol] / fx
    return equity, prices


def _size_a0(names: list, slot: Decimal, fx: Decimal, price_of, held_of
             ) -> tuple[dict[str, Decimal], Decimal]:
    """A0's slot rule against whole-account equity, plus the value it occupies.

    The no-churn branch is A0's own: a name already held keeps its quantity
    while its signal is on, so a rising position is not trimmed back to the
    slot every session.
    """
    targets: dict[str, Decimal] = {}
    occupied = _ZERO
    for symbol in names:
        price = price_of(symbol)
        held = held_of(symbol)
        quantity = held if held > 0 else _shares(slot, fx, price)
        targets[symbol] = quantity
        if price > 0:
            occupied += quantity * price / fx
    return targets, occupied


def _size_a1(names: list, frozen_names: set, capital: Decimal, fx: Decimal,
             price_of, held_of, band: float) -> dict[str, Decimal]:
    """Equal split of the capital A0 left, inside the no-churn band.

    Three separate outcomes, and conflating any two of them is a real defect:

      frozen   The name is in injection["thin"] (no price at this session's
               decision key) or the whole A1 leg is frozen because the ranking
               table is stale. Target equals the held quantity: without a
               current price there is no defensible quantity to move to, and
               selling on a data hole is a decision the data does not support.
      zero     There is no capital left (A0 occupies the whole 99%), or the
               name has no price at all. b0_spec.md section 3.4 is explicit
               that this SELLS the position rather than keeping it, and the
               guard exists so the band below is never evaluated against a
               zero or negative target.
      banded   Everything else: recompute the target and move only when the
               held quantity is more than `band` away from it.
    """
    targets: dict[str, Decimal] = {}
    sizable = [s for s in names if s not in frozen_names]
    per = capital / Decimal(len(sizable)) if sizable and capital > 0 else _ZERO
    for symbol in names:
        held = held_of(symbol)
        if symbol in frozen_names:
            targets[symbol] = held if held > 0 else _ZERO
            continue
        price = price_of(symbol)
        target = _shares(per, fx, price) if per > 0 else _ZERO
        if held > 0 and target > 0 and price > 0 \
                and abs(float(held - target)) / float(target) < band:
            targets[symbol] = held
        else:
            targets[symbol] = target
    return targets


def _ordered(targets: dict[str, Decimal], held_of, a0_names: list,
             a1_names: list, sells_first: bool) -> dict[str, Decimal]:
    """Reductions first, then A0 purchases, then A1 purchases.

    The execution layer and the engine both submit in the mapping's insertion
    order, and a sell that settles first funds the buys that follow it. With
    the reference implementation's order (A0 names, then A1 names, then the
    clearing zeros) the same-session buys were examined while the cash from
    that session's sells had not been credited yet, which is where roughly
    2,000 of the recorded 2,901 rejections came from (b0_spec.md section 5).

    sells_first=False keeps the given order untouched, which is what the
    reproduction arm needs.
    """
    if not sells_first:
        return targets
    a0_set, a1_set = set(a0_names), set(a1_names)
    reductions: dict[str, Decimal] = {}
    a0_buys: dict[str, Decimal] = {}
    a1_buys: dict[str, Decimal] = {}
    other: dict[str, Decimal] = {}
    for symbol, target in targets.items():
        if target <= held_of(symbol):
            reductions[symbol] = target
        elif symbol in a1_set:
            a1_buys[symbol] = target
        elif symbol in a0_set:
            a0_buys[symbol] = target
        else:
            other[symbol] = target
    ordered = dict(reductions)
    ordered.update(a0_buys)
    ordered.update(a1_buys)
    ordered.update(other)
    return ordered


# ============================================================================
# [4] Strategy assembly
# ============================================================================

def _decide(view, portfolio, params, injection, a1_leg) -> dict[str, Decimal]:
    """One session's targets. Pure; see the module header for the rule."""
    as_of = _view_date(view)
    sessions = {_as_date(s) for s in injection["sessions"]}
    if as_of < _as_date(params["live_from"]):
        return {}
    if as_of not in sessions:
        # GBPUSD=X keeps trading on US holidays and supplies timeline keys
        # that are not sessions; deciding on one would rebuy the whole book
        # against a stale price set.
        return {}
    fx_bar = view.bar(params["fx_symbol"])
    if fx_bar is None or fx_bar.close <= 0:
        return {}
    fx = Decimal(str(fx_bar.close))

    a1_params = params["a1_params"]
    a1_leg(view, portfolio, a1_params)
    book = list(a1_leg.book)

    s0 = _a0_signal_set(view, portfolio, params, injection)
    active, a0_prices = _a0_active_and_prices(view, injection, params)
    equity, prices = _equity(view, portfolio, fx)
    prices = {**a0_prices, **prices}
    if not book and not s0:
        return {}

    def price_of(symbol) -> Decimal:
        if symbol in prices:
            return prices[symbol]
        bar = view.bar(symbol)
        return Decimal(str(bar.close)) \
            if bar is not None and bar.close > 0 else _ZERO

    def held_of(symbol) -> Decimal:
        return portfolio.positions.get(symbol, _ZERO) \
            + portfolio.pending_signed_qty.get(symbol, _ZERO)

    headroom = Decimal(str(params.get("slot_headroom", 0.99)))
    priority = str(params.get("priority", "a1"))
    a0_sized, a1_sized = _split(s0, set(book), priority)
    slot = equity / Decimal(max(len(active), 1)) * headroom

    targets, a0_value = _size_a0(sorted(a0_sized), slot, fx, price_of, held_of)

    frozen_names = set()
    if injection.get("a1_frozen"):
        frozen_names = set(a1_sized)
    else:
        frozen_names = {s for s in a1_sized if s in set(injection.get("thin")
                                                       or [])}
    # A frozen leg keeps its position, so the capital it occupies is no longer
    # available to the names that ARE being sized; counting it as free would
    # over-allocate the account by exactly that amount.
    frozen_value = _ZERO
    for symbol in frozen_names:
        price = price_of(symbol)
        held = held_of(symbol)
        if price > 0 and held > 0:
            frozen_value += held * price / fx

    capital = equity * headroom - a0_value - frozen_value
    targets.update(_size_a1(sorted(a1_sized), frozen_names, capital, fx,
                            price_of, held_of,
                            float(params.get("a1_band", 0.10))))

    for symbol in list(portfolio.positions) + list(portfolio.pending_signed_qty):
        if symbol in targets:
            continue
        if portfolio.positions.get(symbol, _ZERO) > 0 \
                or portfolio.pending_signed_qty.get(symbol, _ZERO) != 0:
            targets[symbol] = _ZERO
    for symbol in list(params["a0_params"]["trade_symbols"]):
        targets.setdefault(symbol, _ZERO)

    return _ordered(targets, held_of,
                    list(params["a0_params"]["trade_symbols"]), book,
                    bool(params.get("sells_first", True)))


def make_strategy(injection: dict):
    """Bind an injection and return the strategy callable (seam S7).

    The A1 leg is built here, once, so its running book survives across the
    sessions of a backtest exactly as it survives across the process of a
    live decision.
    """
    a1_leg = _a1.make_strategy(injection)

    def strategy(view, portfolio, params) -> dict[str, Decimal]:
        return _decide(view, portfolio, params, injection, a1_leg)

    strategy.a1_leg = a1_leg
    return strategy


def compute_targets(view, portfolio, params) -> dict[str, Decimal]:
    """Plugin-contract entry point; the injection arrives inside params."""
    injection = params.get("injection")
    if injection is None:
        raise ValueError(
            "b0 needs params['injection'] (see the module header); the entry "
            "layer normally calls make_strategy() instead, to keep the daily "
            "history and the ranking table out of the run metadata")
    return make_strategy(injection)(view, portfolio, params)


# ============================================================================
# [5] Diagnostics
# ============================================================================

def signal_diagnostics(view, portfolio, params, injection: dict) -> dict:
    """Seam S6: everything the dashboard shows about one decision.

    Called BEFORE submission. After submission the book already carries this
    session's pending quantities, so held would have moved and every status,
    every added/dropped set and the attribution would describe a state the
    decision never saw.
    """
    as_of = _view_date(view)
    fx_bar = view.bar(params["fx_symbol"])
    fx = Decimal(str(fx_bar.close)) if fx_bar is not None \
        and fx_bar.close > 0 else _ZERO

    a0_params = params["a0_params"]
    a1_params = params["a1_params"]
    priority = str(params.get("priority", "a1"))

    if injection.get("a0_mode", "view") == "rows":
        a0_view = _a0i.daily_view(view, a0_params, injection.get("a0_rows") or {},
                                  a0_params.get("exchange_tz", _TZ_NEW_YORK))
    else:
        a0_view = view
    a0_tree = _a0.signal_diagnostics(a0_view, a0_params)
    a1_tree = _a1.signal_diagnostics(view, portfolio, a1_params, injection)

    s0 = _a0_signal_set(view, portfolio, params, injection)
    book = [row["symbol"] for row in a1_tree["book"]
            if row["status"] != "exiting"]
    a0_sized, a1_sized = _split(s0, set(book), priority)

    equity, prices = (_equity(view, portfolio, fx) if fx > 0
                      else (portfolio.cash_gbp, {}))
    headroom = Decimal(str(params.get("slot_headroom", 0.99)))

    def price_of(symbol) -> Decimal:
        if symbol in prices:
            return prices[symbol]
        bar = view.bar(symbol)
        return Decimal(str(bar.close)) \
            if bar is not None and bar.close > 0 else _ZERO

    # Attribution follows the split that was actually applied, not membership
    # of a name list: with priority "a1" an overlapping name is sized by A1
    # and its value belongs to A1, and reporting it as A0 would make the
    # occupancy panel disagree with the orders.
    attribution: dict[str, str] = {}
    a0_value = a1_value = _ZERO
    for symbol, qty in portfolio.positions.items():
        if qty <= 0:
            continue
        if symbol in a0_sized:
            attribution[symbol] = "a0"
        elif symbol in a1_sized:
            attribution[symbol] = "a1"
        else:
            attribution[symbol] = "other"
        price = price_of(symbol)
        if price > 0 and fx > 0:
            value = qty * price / fx
            if attribution[symbol] == "a0":
                a0_value += value
            elif attribution[symbol] == "a1":
                a1_value += value

    active, _ = _a0_active_and_prices(view, injection, params)
    slot = equity / Decimal(max(len(active), 1)) * headroom
    a0_target = sum((slot for _ in a0_sized), _ZERO)
    a1_target = equity * headroom - a0_target

    return {
        "as_of": as_of.isoformat(),
        "strategy": STRATEGY_NAME,
        "priority": priority,
        "a0": a0_tree,
        "a1": a1_tree,
        "allocation": {
            "equity_gbp": float(equity),
            "headroom": float(headroom),
            "a0_names": sorted(a0_sized),
            "a1_names": sorted(a1_sized),
            "overlap": sorted(s0 & set(book)),
            "a0_target_gbp": float(a0_target),
            "a1_target_gbp": float(max(a1_target, _ZERO)),
            "cash_target_gbp": float(equity - headroom * equity),
        },
        "attribution": {
            "positions": attribution,
            "a0_value_gbp": float(a0_value),
            "a1_value_gbp": float(a1_value),
            "cash_gbp": float(portfolio.cash_gbp),
        },
    }
