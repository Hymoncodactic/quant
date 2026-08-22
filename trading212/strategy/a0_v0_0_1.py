"""A0: regime-gated equal-slot long book on US mega-cap tech, daily decisions.

Responsibility: hold THE single copy of the A0 signal (ARCHITECTURE.md section 2.0).
The backtest loads this module through backtest/engine/strategy_loader.py and any
future execution layer must import the same file. Derived from the round-3 research
ruling research/decisions/20260820_regime_lf_ruling.md (config
on|tsmom252|p80x0|qqq200|off, named A0 by the user 2026-08-20). This module is the
daily HELD adaptation: the engine fills market orders at the next bar's open, so
entries and exits trade at the open after the signal day, and the research's
overnight-only return stream is not expressible at daily bars.

The rule, where every input is a completed bar with ts <= now and the cutoff is
guaranteed by the engine's MarketView:

    per-symbol signal   tsmom252, that is close_now > close_252_bars_ago. Params can
                        select ma200 or always instead, for ablation arms.
    vol gate (market)   Yang-Zhang(20d) annualized volatility of the state symbol,
                        ranked as an expanding percentile within the view's own
                        history. At or above the 0.80 quantile the whole book goes to
                        cash. The gate stays open until vol_min_history observations
                        exist.
    trend gate          A state symbol close below its SMA200 sends the whole book to
                        cash.
    sizing              Equal cash slots over the active trade universe, meaning the
                        symbols listed with enough bars. Fractional shares are used. A
                        held position is not resized while its state stays on, which is
                        the no-churn band, and it is fully exited when the state turns
                        off.

Purity: no I/O, no globals, no state between calls. Everything is recomputed from the
view on every step. Randomness: none.

Out of scope: parameter values, which belong to
trading212/config/strategies/a0_v0_0_1.yaml and reach this module through params;
the minute-bar timing variant, which belongs to a0_intraday_v0_0_1.py and delegates
its whole decision back to this module rather than reimplementing it; fills, costs
and performance statistics, which belong to backtest/t212/; order submission, which
belongs to trading212/execution/.

Public functions:
    compute_targets(view, portfolio, params)   Target shares per symbol for this step

Public constants:
    STRATEGY_NAME     str  "a0". Must match the file name or strategy_loader refuses
                           to load the module.
    STRATEGY_VERSION  str  "0.0.1". Same check.

Parameters, all read from params. Baseline values are quoted from
trading212/config/strategies/a0_v0_0_1.yaml, which cites
research/decisions/20260820_regime_lf_ruling.md section 3 as their basis; that ruling
file is not present on disk as of 2026-08-22, so the basis is unverified:
    trade_symbols      list[str]  Symbols the book may hold. Baseline: 18 US names.
    state_symbol       str        Market-state series, "QQQ". Data only, never traded.
    fx_symbol          str        "GBPUSD=X", USD per GBP mid. Data only, used to size.
    signal_mode        str        "tsmom252" (baseline), "ma200" or "always".
    tsmom_lookback     int        Bars, 252.
    trend_ma           int        Bars, 200.
    vol_window         int        Bars, 20.
    vol_pct_threshold  float      Expanding percentile that shuts the vol gate, 0.80.
    vol_min_history    int        Observations required before the vol gate may act,
                                  756.
    use_vol_gate       bool       Baseline true.
    use_trend_gate     bool       Baseline true.
    warmup_bars        int        Bars a symbol needs to join the book, 260. The
                                  admission test also passes at tsmom_lookback + 1
                                  bars, and the view is asked for only
                                  max(tsmom_lookback, trend_ma) + 1 bars, which is 253
                                  at baseline, so at baseline the 253-bar branch is the
                                  one that decides.
    live_from          str        "YYYY-MM-DD"; the book is flat before this date.
                                  Baseline "2018-01-01".
    slot_headroom      float      Fraction of a slot actually deployed, 0.99. Cost
                                  buffer.

Module constants:
    _SHARE_STEP  Decimal  Share quantization step, 0.0001, always rounded down. Source
                          unknown, needs verification: the fractional-share precision
                          Trading 212 actually accepts is not cited in this file.

Inputs: None. Everything arrives as the view, portfolio and params arguments; the
    module opens no file and makes no network call.
Outputs: None. The returned target mapping is the only effect; no argument is mutated.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["STRATEGY_NAME", "STRATEGY_VERSION", "compute_targets"]

from decimal import Decimal, ROUND_DOWN

import numpy as np
import pandas as pd

STRATEGY_NAME = "a0"
STRATEGY_VERSION = "0.0.1"

_SHARE_STEP = Decimal("0.0001")


def _now_date(view) -> str:
    """The current step's exchange-agnostic ISO date."""
    ts = view.now
    try:
        if ts.tzinfo is not None:
            ts = ts.tz_convert("America/New_York")
    except (TypeError, AttributeError):
        pass
    return str(ts.date())


def _yang_zhang(opens, highs, lows, closes, window: int) -> np.ndarray:
    """Yang-Zhang (2000) annualized volatility series, causal by construction.

    Same estimator as the research derivation (research/regime_lab/vol.py);
    re-stated here because the strategy module may not import research code.
    """
    o = pd.Series(np.log(opens[1:] / closes[:-1]))
    c = pd.Series(np.log(closes[1:] / opens[1:]))
    u = np.log(highs[1:] / opens[1:])
    d = np.log(lows[1:] / opens[1:])
    rs = pd.Series(u * (u - c.to_numpy()) + d * (d - c.to_numpy()))
    k = 0.34 / (1.34 + (window + 1) / (window - 1))
    # Vectorized rolling moments: the strategy recomputes the series on every
    # step (purity: no cross-call state), so a python loop here would make the
    # whole run quadratic in history length.
    var = (o.rolling(window).var(ddof=1)
           + k * c.rolling(window).var(ddof=1)
           + (1.0 - k) * rs.rolling(window).mean()).clip(lower=0.0)
    return np.sqrt(var.to_numpy() * 252.0)


def _gates_open(view, params) -> bool:
    """Evaluate both market-level gates on the state symbol. True = may hold."""
    state_bars = view.bars(params["state_symbol"], 8000)
    closes = np.array([b.close for b in state_bars], dtype=float)

    if params.get("use_trend_gate", True):
        ma = int(params.get("trend_ma", 200))
        if len(closes) >= ma and closes[-1] < closes[-ma:].mean():
            return False

    if params.get("use_vol_gate", True):
        window = int(params.get("vol_window", 20))
        if len(closes) >= window + 1:
            opens = np.array([b.open for b in state_bars], dtype=float)
            highs = np.array([b.high for b in state_bars], dtype=float)
            lows = np.array([b.low for b in state_bars], dtype=float)
            vol = _yang_zhang(opens, highs, lows, closes, window)
            vol = vol[~np.isnan(vol)]
            if len(vol) >= int(params.get("vol_min_history", 756)):
                pct = float((vol <= vol[-1]).mean())
                if pct >= float(params.get("vol_pct_threshold", 0.80)):
                    return False
    return True


def compute_targets(view, portfolio, params) -> dict[str, Decimal]:
    """Target shares per symbol for the current step. Pure function."""
    if _now_date(view) < str(params["live_from"]):
        return {}

    trade_symbols = list(params["trade_symbols"])
    lookback = int(params.get("tsmom_lookback", 252))
    trend_ma = int(params.get("trend_ma", 200))
    warmup = int(params.get("warmup_bars", 260))
    mode = params.get("signal_mode", "tsmom252")

    fx_bar = view.bar(params["fx_symbol"])
    if fx_bar is None or fx_bar.close <= 0:
        return {s: portfolio.positions.get(s, Decimal("0"))
                for s in trade_symbols}
    fx = Decimal(str(fx_bar.close))          # USD per GBP, mid

    open_for_business = _gates_open(view, params)

    # Active universe and per-symbol signal, from completed bars only.
    signal_on: dict[str, bool] = {}
    active: list[str] = []
    price_usd: dict[str, Decimal] = {}
    for symbol in trade_symbols:
        bars = view.bars(symbol, max(lookback, trend_ma) + 1)
        if len(bars) < warmup and len(bars) < lookback + 1:
            continue
        closes = [b.close for b in bars]
        active.append(symbol)
        price_usd[symbol] = Decimal(str(closes[-1]))
        if mode == "always":
            signal_on[symbol] = True
        elif mode == "ma200":
            signal_on[symbol] = (len(closes) >= trend_ma
                                 and closes[-1] >= float(np.mean(closes[-trend_ma:])))
        else:
            signal_on[symbol] = (len(closes) >= lookback + 1
                                 and closes[-1] > closes[-lookback - 1])

    targets: dict[str, Decimal] = {}
    if not active:
        return targets

    # Equity in GBP from the strategy's own mid-price view.
    equity_gbp = portfolio.cash_gbp
    for symbol, qty in portfolio.positions.items():
        if qty and symbol in price_usd:
            equity_gbp += qty * (price_usd[symbol] / fx)

    slot_gbp = equity_gbp / Decimal(len(active)) \
        * Decimal(str(params.get("slot_headroom", 0.99)))

    for symbol in active:
        held = portfolio.positions.get(symbol, Decimal("0")) \
            + portfolio.pending_signed_qty.get(symbol, Decimal("0"))
        want = open_for_business and signal_on[symbol]
        if not want:
            targets[symbol] = Decimal("0")
        elif held > 0:
            # No-churn band: keep the existing position size while on.
            targets[symbol] = held
        else:
            shares = (slot_gbp * fx / price_usd[symbol]) \
                .quantize(_SHARE_STEP, rounding=ROUND_DOWN)
            targets[symbol] = shares if shares > 0 else Decimal("0")
    return targets
