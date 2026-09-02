"""A1: wide-universe cross-sectional momentum, 20 equal-weight names, 21-session rotation.

Responsibility: hold THE single copy of the A1 signal (ARCHITECTURE.md section
2.0), exactly as trading212/strategy/a1_spec.md defines it. Roughly 1,500 US
names are screened by a causal liquidity admission, ranked by 12-1 momentum,
and the top 20 are held equal weight; a held name survives while it stays
inside the top 40 (the buffer band). No market gate, no stop, no sizing change
between rebalances. The backtest and the live execution layer both load THIS
file, so the ranking exists once.

The module is pure: it opens no file, makes no network call and reads no
configuration. Everything it needs arrives through the injection object built
by the entry layer (fixplans/t212/b0/00_coordination.md section 2.3) and
through params.

Two injection shapes are accepted and exactly one must be present:

    live      injection["a1_rank"] is the pre-market ranking table computed by
              trading212/ingest/a1_rank.py for session injection["rank_as_of"],
              which is normally the session before the decision day. The
              decision window is far too short to load a 1,500-name panel, so
              the ranking is computed after the previous close instead.
    research  injection["panel"] carries {"closes": DataFrame, "volumes":
              DataFrame} and the module ranks the panel itself, truncated at
              the decision day. This is the reproduction path; it is the only
              path where rank_as_of equals the decision day.

Out of scope: what a session is, which arrives as injection["sessions"] and is
    built from local SPY daily bars by trading212/execution/market_data.py
    us_sessions(); the venue ticker map, built by
    scripts/20260903_build_universe_ticker_map.py and reaching this module as
    params["verified_tickers"]; capital sharing with A0, which belongs to
    trading212/strategy/b0_v0_0_1.py; order submission, which belongs to
    trading212/execution/.

Public functions:
    rank_table(closes, volumes, as_of, params)   The admission and ranking
                                                 table for one session. The
                                                 ONLY implementation of the
                                                 admission rules and the score.
    select(rank_df, book, params)                Buffer-band selection.
    size(pick, equity_gbp, fx, prices, params)   Equal-weight target shares.
    make_strategy(injection)                     Bind an injection, return the
                                                 strategy callable.
    compute_targets(view, portfolio, params)     Plugin entry point; requires
                                                 params["injection"].
    signal_diagnostics(view, portfolio, params, injection)
                                                 The "a1" diagnostics subtree.

Public constants:
    STRATEGY_NAME     str  "a1". Must match the file name or the loaders
                           refuse the module.
    STRATEGY_VERSION  str  "0.0.1". Same check.
    ELIG_REASONS      tuple  The frozen elig_reason enumeration, which the
                           ranking parquet and the dashboard both key on.
    RANK_COLUMNS      tuple  The frozen column order of rank_table's output.

Parameters, all read from params; baseline values are quoted from
trading212/config/strategies/a1_v0_0_1.yaml, whose own source is
trading212/strategy/a1_spec.md sections 3 to 7:
    universe_file                 str    Frozen candidate pool, read by the
                                         entry layer, never by this module.
    n_hold                        int    20, the book size.
    band_multiple                 int    2, so the band is the top 40.
    rebalance_every               int    21 sessions.
    mom_long / mom_skip           int    252 / 21 session offsets of the score.
    liq_window                    int    252, the admission window.
    min_dollar_volume_usd         float  1e6, admission E1.
    max_zero_volume_share         float  0.01, admission E2.
    min_history_bars              int    300, admission E3. Counted over the
                                         panel handed in, so the caller fixes
                                         the panel start (2010-01-04 in the
                                         reference arm).
    order_usd_for_participation   float  640, admission E4.
    require_verified_ticker       bool   Admission E5, live only. True demands
                                         params["verified_tickers"]; a name
                                         with no venue ticker cannot be
                                         ordered at all (instruments.
                                         order_ticker raises), so admitting it
                                         would abort the session.
    slot_headroom                 float  0.99, the cost buffer on every leg.
    fx_symbol                     str    "GBPUSD=X", USD per GBP.
    rebalance_anchor              str    "YYYY-MM-DD". Session 0 of the
                                         rotation; the entry layer overrides
                                         it with execution.b0_live_from.
    live_from                     str    Same date; no target before it.

Module constants:
    _SHARE_STEP  Decimal  0.0001, the venue fractional-share step, the same
                          constant a0_v0_0_1.py quantizes to.

Inputs: none. The injection, view, portfolio and params arguments carry
    everything; no path is opened here.
Outputs: none. The returned mapping is the only effect and no argument is
    mutated. make_strategy's closure carries the running book, because a
    buffer band is by definition a function of the previous book; live calls
    the closure once per process, so only the reproduction arm advances it.

Change log:
    2026-09-03  Created from a1_spec.md and fixplans/t212/b0/01_strategy_a1.md.
"""

from __future__ import annotations

__all__ = ["STRATEGY_NAME", "STRATEGY_VERSION", "ELIG_REASONS", "RANK_COLUMNS",
           "rank_table", "select", "size", "make_strategy", "compute_targets",
           "signal_diagnostics"]

from datetime import date
from decimal import Decimal, ROUND_DOWN

import numpy as np
import pandas as pd

STRATEGY_NAME = "a1"
STRATEGY_VERSION = "0.0.1"

_SHARE_STEP = Decimal("0.0001")

# Frozen enumeration. "ok" is reported only for a name that passes all
# admission conditions AND carries a score, which is exactly the set that
# receives a rank; an admitted name whose score is undefined reports
# "no_score" and no rank.
ELIG_REASONS = ("ok", "dollar_volume", "zero_volume", "history",
                "participation", "no_ticker", "no_score")

RANK_COLUMNS = ("symbol", "ticker", "close", "score", "eligible",
                "elig_reason", "rank")

_TZ_NEW_YORK = "America/New_York"


# ============================================================================
# [1] Admission and ranking
# ============================================================================

def _tail_admission(closes: pd.DataFrame, volumes: pd.DataFrame,
                    window: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Median dollar volume, zero-volume share and history count at the last row.

    Equivalent to the research layer's whole-panel rolling computation read at
    its final row (research/xsmom_wide/run_study.py eligibility), but evaluated
    on the tail only: a per-session call does not need the other four thousand
    rows, and the panel is 1,500 columns wide.

    min_periods equals the window there, so a window holding even one missing
    observation yields NaN rather than a median of what is present. That is
    reproduced here by masking the median where the count of valid
    observations falls short, NOT by letting median() skip the gaps -- the two
    differ precisely for the names a data hole would otherwise let in.
    """
    tail_c = closes.iloc[-window:]
    tail_v = volumes.iloc[-window:]
    obs = closes.notna().sum()
    if len(tail_c) < window:
        nan = pd.Series(np.nan, index=closes.columns)
        return nan, nan, obs
    dollar = tail_c * tail_v
    med = dollar.median().where(dollar.notna().sum() >= window)
    traded = ((tail_v > 0) & tail_c.notna()).astype(float)
    zero_share = 1.0 - traded.mean()
    return med, zero_share, obs


def _eligible_mask(closes: pd.DataFrame, volumes: pd.DataFrame,
                   params: dict) -> tuple[pd.Series, pd.Series]:
    """(eligible, reason) for every column at the panel's last row.

    Five conditions, evaluated on data up to and including the last row and
    nothing later (a1_spec.md section 3.2 plus E5 from
    fixplans/t212/b0/00_coordination.md decision A5). Reasons are reported in
    a fixed precedence so the same failure always names the same cause:
    history, then dollar volume, then zero-volume share, then participation,
    then the missing venue ticker.
    """
    window = int(params.get("liq_window", 252))
    med, zero_share, obs = _tail_admission(closes, volumes, window)

    min_dollar = float(params.get("min_dollar_volume_usd", 1e6))
    max_zero = float(params.get("max_zero_volume_share", 0.01))
    min_bars = int(params.get("min_history_bars", 300))
    order_usd = float(params.get("order_usd_for_participation", 640.0))

    e1 = (med >= min_dollar).fillna(False)
    e2 = (zero_share < max_zero).fillna(False)
    e3 = (obs >= min_bars).fillna(False)
    e4 = ((order_usd / med) < 0.001).fillna(False)
    if params.get("require_verified_ticker", False):
        tickers = params.get("verified_tickers")
        if tickers is None:
            raise ValueError(
                "require_verified_ticker is on but params['verified_tickers'] "
                "is absent; admitting an unmapped name would make "
                "instruments.order_ticker raise mid-session")
        e5 = pd.Series([bool(tickers.get(s)) for s in closes.columns],
                       index=closes.columns)
    else:
        e5 = pd.Series(True, index=closes.columns)

    eligible = e1 & e2 & e3 & e4 & e5
    reason = pd.Series("ok", index=closes.columns, dtype=object)
    reason = reason.mask(~e5, "no_ticker")
    reason = reason.mask(~e4, "participation")
    reason = reason.mask(~e2, "zero_volume")
    reason = reason.mask(~e1, "dollar_volume")
    reason = reason.mask(~e3, "history")
    return eligible, reason


def _score(closes: pd.DataFrame, params: dict) -> pd.Series:
    """12-1 momentum at the panel's last row: C[t - skip] / C[t - long] - 1."""
    long = int(params.get("mom_long", 252))
    skip = int(params.get("mom_skip", 21))
    if len(closes) < long + 1:
        return pd.Series(np.nan, index=closes.columns)
    return closes.iloc[-1 - skip] / closes.iloc[-1 - long] - 1.0


def rank_table(closes: pd.DataFrame, volumes: pd.DataFrame, as_of,
               params: dict) -> pd.DataFrame:
    """Admission, score and rank for one session. The only implementation.

    The panel is truncated at as_of before anything is read, so no later bar
    can reach the result. The pre-market ranking pass, the reproduction
    script and the tests all call THIS function; a second copy of the
    admission rules anywhere else is a defect.

    Args:
        closes: Date-indexed close panel, columns are disk-spelled symbols.
        volumes: Same shape, share volume.
        as_of: The session to rank. Must be present in the index.
        params: The A1 parameter mapping; see the module header.

    Returns:
        A frame with RANK_COLUMNS, one row per candidate, ordered by rank with
        the unranked names after them in panel column order. "ticker" is filled
        from params["verified_tickers"] when present and is None otherwise.
    """
    as_of = _as_date(as_of)
    index = list(closes.index)
    if as_of not in index:
        raise KeyError(f"{as_of} is not a session in the panel "
                       f"({index[0]}..{index[-1]})")
    panel_c = closes.loc[:as_of]
    panel_v = volumes.reindex(panel_c.index)[panel_c.columns]

    eligible, reason = _eligible_mask(panel_c, panel_v, params)
    score = _score(panel_c, params)
    close = panel_c.iloc[-1]

    ranked = score.where(eligible).dropna()
    # Stable sort: two identical float scores would otherwise order by an
    # implementation detail of quicksort and make the book irreproducible.
    ranked = ranked.sort_values(ascending=False, kind="mergesort")
    rank_of = {symbol: position for position, symbol
               in enumerate(ranked.index, start=1)}

    reason = reason.mask(eligible & score.isna(), "no_score")
    tickers = params.get("verified_tickers") or {}
    frame = pd.DataFrame({
        "symbol": list(panel_c.columns),
        "ticker": [tickers.get(s) for s in panel_c.columns],
        "close": [float(v) if pd.notna(v) else None for v in close],
        "score": [float(v) if pd.notna(v) else None for v in score],
        "eligible": [bool(v) for v in eligible],
        "elig_reason": list(reason),
        "rank": [rank_of.get(s) for s in panel_c.columns],
    })
    frame["rank"] = frame["rank"].astype("Int64")
    return frame.sort_values(["rank", "symbol"], na_position="last",
                             kind="mergesort").reset_index(drop=True)


# ============================================================================
# [2] Selection and sizing
# ============================================================================

def select(rank_df: pd.DataFrame, book, params: dict) -> list[str]:
    """The buffer band of a1_spec.md section 6.

    A name already in the book is kept while it stays inside the top
    band_multiple * n_hold, in the book's own order; the remaining slots go to
    the highest-ranked names that were not kept. The book is the PREVIOUS
    target list, never the current positions: a position can differ from the
    book after a rejected order, and deriving the band from positions would
    then silently drop a name the strategy still holds in its plan
    (fixplans/t212/b0/00_coordination.md decision A12).
    """
    n_hold = int(params.get("n_hold", 20))
    band = int(params.get("band_multiple", 2))
    ranked = [str(s) for s in
              rank_df.loc[rank_df["rank"].notna()]
              .sort_values("rank", kind="mergesort")["symbol"]]
    if not book:
        return ranked[:n_hold]
    in_band = set(ranked[:band * n_hold])
    keep = [s for s in book if s in in_band]
    fresh = [s for s in ranked if s not in keep]
    return keep + fresh[:max(0, n_hold - len(keep))]


def size(pick: list[str], equity_gbp: Decimal, fx: Decimal,
         prices: dict[str, Decimal], params: dict) -> dict[str, Decimal]:
    """Equal-weight target shares for the picked names.

    A picked name with no usable price yields NO entry and its weight is NOT
    redistributed over the others: the slot simply idles for the session. The
    alternative -- spreading the weight -- would change every other name's
    quantity and make the book depend on which data holes happened to exist
    that day, so the reproduction arm and the live arm could no longer be
    compared (a1_spec.md section 11.6).
    """
    out: dict[str, Decimal] = {}
    if not pick:
        return out
    headroom = Decimal(str(params.get("slot_headroom", 0.99)))
    weight = Decimal(str(1.0 / len(pick)))
    for symbol in pick:
        price = prices.get(symbol)
        if price is None or price <= 0:
            continue
        shares = (equity_gbp * weight * headroom * fx / price) \
            .quantize(_SHARE_STEP, rounding=ROUND_DOWN)
        if shares > 0:
            out[symbol] = shares
    return out


# ============================================================================
# [3] Session arithmetic
# ============================================================================

def _as_date(value) -> date:
    """Coerce a date-like to a plain date."""
    if isinstance(value, date) and not isinstance(value, pd.Timestamp):
        return value
    return pd.Timestamp(value).date()


def _rebalance_today(sessions: list, as_of, every: int) -> tuple[bool, int]:
    """(is a rebalance session, session index) for as_of.

    The index counts EVERY session since the anchor, half days included, and
    it is a pure function of the session list: a session the live cycle
    aborted still advances the rotation, exactly as the backtest's calendar
    does (fixplans/t212/b0/00_coordination.md decision A4).
    """
    as_of = _as_date(as_of)
    plain = [_as_date(s) for s in sessions]
    if as_of not in plain:
        return False, -1
    index = plain.index(as_of)
    return index % int(every) == 0, index


def _equity(view, portfolio, fx: Decimal) -> Decimal:
    """Whole-book equity in GBP at the view's own prices.

    A position whose symbol has no bar counts as zero rather than at a stale
    price. That is the reference implementation's reading and it is the
    conservative one: an unpriceable holding cannot be sold at a remembered
    price either.
    """
    equity = portfolio.cash_gbp
    for symbol, qty in portfolio.positions.items():
        bar = view.bar(symbol)
        if bar is not None and qty:
            equity += qty * Decimal(str(bar.close)) / fx
    return equity


# ============================================================================
# [4] Strategy assembly
# ============================================================================

def _view_date(view) -> date:
    """The current step's exchange-local date."""
    ts = view.now
    try:
        if ts.tzinfo is not None:
            ts = ts.tz_convert(_TZ_NEW_YORK)
    except (TypeError, AttributeError):
        pass
    return ts.date()


def _ranked_for(injection: dict, as_of: date, params: dict) -> pd.DataFrame:
    """The ranking table backing one decision, from whichever shape was injected."""
    panel = injection.get("panel")
    table = injection.get("a1_rank")
    if panel is None and table is None:
        raise ValueError(
            "the injection carries neither 'panel' (research) nor 'a1_rank' "
            "(live); with no ranking there is no book to rotate into. Live, "
            "this means the pre-market pass produced no table -- the caller "
            "should have set a1_frozen and skipped the rotation")
    if panel is not None and table is not None:
        raise ValueError("the injection must carry exactly one of "
                         "'panel' (research) and 'a1_rank' (live)")
    if panel is not None:
        return rank_table(panel["closes"], panel["volumes"], as_of, params)
    rank_as_of = _as_date(injection["rank_as_of"])
    if rank_as_of > as_of:
        raise ValueError(f"rank_as_of {rank_as_of} is after the decision day "
                         f"{as_of}; the ranking would carry future prices")
    return table


def make_strategy(injection: dict):
    """Bind an injection and return the strategy callable.

    The callable keeps the running book in its closure. That is state, and it
    is deliberate: the buffer band is defined against the previous book, so
    somewhere has to remember it. Live runs call the closure once per process
    and read the previous book out of the injection, which the execution layer
    fills from the last a1_plan record; only the reproduction arm, which walks
    thousands of sessions in one process, actually advances it.
    """
    sessions = [_as_date(s) for s in injection["sessions"]]
    session_set = set(sessions)

    def strategy(view, portfolio, params) -> dict[str, Decimal]:
        as_of = _view_date(view)
        if as_of < _as_date(params["live_from"]):
            return {}
        if as_of not in session_set:
            return {}
        every = int(params.get("rebalance_every", 21))
        is_rebalance, _index = _rebalance_today(sessions, as_of, every)
        if is_rebalance:
            strategy.pending = True
        if not strategy.pending:
            return {}
        if injection.get("a1_frozen"):
            # No usable ranking: the pre-market pass produced nothing, or what
            # it produced is too old to rotate on. Holding the existing book
            # is the conservative answer -- rotating on a stale ranking is a
            # different strategy from the tested one, and selling out is a
            # decision the missing data does not support. The rotation stays
            # pending and happens as soon as a fresh table exists.
            return {}

        fx_bar = view.bar(params["fx_symbol"])
        if fx_bar is None or fx_bar.close <= 0:
            # Leave the rebalance pending: without a rate nothing can be
            # sized, and skipping the rotation entirely would leave the book
            # 21 sessions stale.
            return {}
        fx = Decimal(str(fx_bar.close))

        ranked = _ranked_for(injection, as_of, params)
        pick = select(ranked, strategy.book, params)
        strategy.pending = False
        strategy.book = {s: 1.0 / len(pick) for s in pick} if pick else {}
        strategy.ranked = ranked

        equity = _equity(view, portfolio, fx)
        prices: dict[str, Decimal] = {}
        for symbol in pick:
            bar = view.bar(symbol)
            if bar is not None and bar.close > 0:
                prices[symbol] = Decimal(str(bar.close))
        # Every current holding is written first, at zero. A name that is
        # still picked overwrites its own entry below and keeps this early
        # position, which puts every reduction ahead of every purchase in the
        # mapping the execution layer iterates
        # (fixplans/t212/b0/00_coordination.md decision A6).
        targets: dict[str, Decimal] = {
            symbol: Decimal("0") for symbol, qty in portfolio.positions.items()
            if qty > 0}
        targets.update(size(pick, equity, fx, prices, params))
        return targets

    strategy.book = dict(injection.get("a1_book") or {})
    strategy.pending = False
    strategy.ranked = None
    return strategy


def compute_targets(view, portfolio, params) -> dict[str, Decimal]:
    """Plugin-contract entry point; the injection arrives inside params."""
    injection = params.get("injection")
    if injection is None:
        raise ValueError(
            "a1 needs params['injection'] (see the module header); the entry "
            "layer normally calls make_strategy() instead, to keep the panel "
            "out of the run metadata")
    return make_strategy(injection)(view, portfolio, params)


# ============================================================================
# [5] Diagnostics
# ============================================================================

def signal_diagnostics(view, portfolio, params, injection: dict) -> dict:
    """The "a1" subtree of the B0 diagnostics (00_coordination.md section 2.6).

    Read-only and side-effect free: it recomputes what the decision would see
    without placing anything. It is called BEFORE submission, because the book
    statuses are read against the positions held at decision time and a
    submitted order changes them.
    """
    as_of = _view_date(view)
    sessions = [_as_date(s) for s in injection["sessions"]]
    every = int(params.get("rebalance_every", 21))
    is_rebalance, index = _rebalance_today(sessions, as_of, every)
    until_next = 0 if is_rebalance else (every - index % every if index >= 0
                                         else None)
    last_rebalance = None
    if index >= 0:
        last_rebalance = sessions[index - index % every].isoformat()

    frozen = bool(injection.get("a1_frozen"))
    try:
        ranked = _ranked_for(injection, as_of, params)
    except ValueError:
        # A panel with no rows keeps every consumer below on one code path;
        # the frozen flag is what tells the reader why it is empty.
        ranked = pd.DataFrame(columns=list(RANK_COLUMNS))
        frozen = True
    book = dict(injection.get("a1_book") or {})
    pick = list(book) if (frozen or not is_rebalance) \
        else select(ranked, book, params)
    rank_of = {str(r.symbol): (None if pd.isna(r.rank) else int(r.rank))
               for r in ranked.itertuples(index=False)}
    score_of = {str(r.symbol): (None if r.score is None or pd.isna(r.score)
                                else float(r.score))
                for r in ranked.itertuples(index=False)}
    held = {s for s, q in portfolio.positions.items() if q > 0}
    thin = set(injection.get("thin") or [])
    n_hold = int(params.get("n_hold", 20))
    band = int(params.get("band_multiple", 2))

    rows = []
    for symbol in pick:
        if frozen or symbol in thin:
            status = "frozen"
        elif symbol not in held:
            status = "entering"
        elif rank_of.get(symbol) is not None and rank_of[symbol] <= n_hold:
            status = "held"
        else:
            status = "held_in_band"
        rows.append({"symbol": symbol, "rank": rank_of.get(symbol),
                     "score": score_of.get(symbol),
                     "weight": 1.0 / len(pick) if pick else 0.0,
                     "status": status})
    for symbol in sorted(held - set(pick)):
        rows.append({"symbol": symbol, "rank": rank_of.get(symbol),
                     "score": score_of.get(symbol), "weight": 0.0,
                     "status": "exiting"})

    ordered = ranked.loc[ranked["rank"].notna()].sort_values(
        "rank", kind="mergesort") if len(ranked) else ranked
    next_in = [{"symbol": str(r.symbol), "rank": int(r.rank),
                "score": None if r.score is None or pd.isna(r.score)
                else float(r.score)}
               for r in ordered.itertuples(index=False)
               if str(r.symbol) not in set(pick)][:5]

    def _edge(position: int):
        if len(ordered) < position:
            return None
        value = ordered.iloc[position - 1]["score"]
        return None if value is None or pd.isna(value) else float(value)

    return {
        "rebalance": {
            "anchor": str(params.get("rebalance_anchor",
                                     params.get("live_from"))),
            "session_index": index, "every": every,
            "sessions_until_next": until_next,
            "last_rebalance": last_rebalance,
            "rank_as_of": str(_as_date(injection["rank_as_of"]))
            if injection.get("rank_as_of") is not None else None,
            "rank_stale_sessions":
                None if injection.get("rank_stale_sessions") is None
                else int(injection["rank_stale_sessions"]),
            "frozen": frozen,
        },
        "eligible_count": int(ranked["eligible"].sum()) if len(ranked) else 0,
        "book": rows,
        "next_in": next_in,
        "band_edge": {f"rank_{n_hold}_score": _edge(n_hold),
                      f"rank_{band * n_hold}_score": _edge(band * n_hold)},
    }
