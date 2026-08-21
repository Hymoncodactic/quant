"""A0: regime-gated equal-slot long book on US mega-cap tech, daily decisions.

THE single copy of the A0 signal (ARCHITECTURE.md section 2.0): the backtest
loads this module through backtest/engine/strategy_loader.py and any future
execution layer must import the same file. Derived from the round-3 research
ruling research/decisions/20260820_regime_lf_ruling.md (config
on|tsmom252|p80x0|qqq200|off, named A0 by the user 2026-08-20); this module is
the daily HELD adaptation: the engine fills market orders at the next bar's
open, so entries and exits trade at the open after the signal day, and the
research's overnight-only return stream is not expressible at daily bars.

Rule (all inputs are completed bars, ts <= now, cutoff guaranteed by the
engine's MarketView):
    per-symbol signal   tsmom252: close_now > close_252_bars_ago
                        (params can select ma200 / always for ablation arms)
    vol gate (market)   Yang-Zhang(20d) annualized vol of the state symbol,
                        expanding percentile within the view's own history;
                        at or above the 0.80 quantile -> whole book to cash.
                        Gate stays open until min_history observations exist.
    trend gate          state symbol close < SMA200 -> whole book to cash.
    sizing              equal cash slots over the active trade universe
                        (listed with >= warmup bars); fractional shares; a
                        held position is not resized while its state stays on
                        (no-churn band), it is fully exited when off.

Purity: no I/O, no globals, no state between calls; everything is recomputed
from the view. Randomness: none.

Params (from trading212/config/strategies/a0_v0_0_1.yaml, injected by the
entry layer):
    trade_symbols   list[str]   symbols the book may hold
    state_symbol    str         market-state series, "QQQ" (data-only)
    fx_symbol       str         "GBPUSD=X" (data-only, sizing conversion)
    signal_mode     str         "tsmom252" | "ma200" | "always"
    tsmom_lookback  int         252
    trend_ma        int         200
    vol_window      int         20
    vol_pct_threshold float     0.80
    vol_min_history int         756
    use_vol_gate    bool
    use_trend_gate  bool
    warmup_bars     int         260
    live_from       str         "YYYY-MM-DD"; flat before this date
    slot_headroom   float       fraction of a slot actually deployed (cost buffer)
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
