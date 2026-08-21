"""Vectorized daily portfolio backtest over the frozen configuration family.

Responsibility: build the aligned return/signal/regime panels once, then price
any configuration as a few elementwise frame products. Not responsible for the
family definition (configs.py), metrics (metrics.py) or judgement.

Timing convention (the no-look-ahead core): every decision input — signal,
volatility, regime state — is computed from data up to and including day t-1
(close), then shifted one day so it can only touch day t's return. Return
streams for day t:
    cc  close_{t-1} -> close_t   (hold)
    on  close_{t-1} -> open_t    (overnight only)
    id  open_t      -> close_t   (intraday only)
All three are decidable at the close of t-1 with zero costs assumed.

Portfolio: equal capital slots over the symbols available on day t (listed,
warmed up, tradeable); slot weight is signal x vol-target x regime multipliers,
capped at 1; unused weight is cash at zero. Universe membership changes over
time as symbols list (PLTR 2020, META 2012, ...).

Public classes / functions:
    Panels.build(symbols, group, market_symbol)   Load and precompute everything
    Panels.run(config)                            Daily returns for one config
    WARMUP_BARS
"""

from __future__ import annotations

__all__ = ["Panels", "WARMUP_BARS"]

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import data as data_module
from . import vol as vol_module

# Bars of history a symbol must have before it may enter the universe: enough
# for the longest signal look-back (252) plus a buffer.
WARMUP_BARS = 260

VOL_WINDOW = 20
REGIME_MIN_HISTORY = 756  # three years of vol observations before percentiles count


@dataclass
class Panels:
    """Aligned per-day panels for one universe plus the market series."""

    streams: dict[str, pd.DataFrame]      # timing -> date x symbol returns
    signals: dict[str, pd.DataFrame]      # signal name -> date x symbol {0,1}, shifted
    vol_scalars: dict[str, pd.DataFrame]  # vol-target name -> date x symbol scalar, shifted
    market_mult: dict[str, pd.Series]     # regime gate name -> date multiplier, shifted
    trend_mult: dict[str, pd.Series]      # market trend gate name -> date multiplier, shifted
    available: pd.DataFrame               # date x symbol bool
    index: pd.DatetimeIndex = field(default=None)

    @classmethod
    def build(
        cls,
        symbols: tuple[str, ...],
        group: str = "us_equity",
        market_symbol: str = "QQQ",
        market_group: str = "us_etf",
    ) -> "Panels":
        """Load every symbol, align on the union calendar, precompute all layers.

        Args:
            symbols: Trading universe.
            group: Data group of the universe.
            market_symbol: Symbol whose volatility and trend define market state.
            market_group: Data group of the market symbol.

        Returns:
            A ready Panels instance.
        """
        closes, opens = {}, {}
        yz = {}
        for symbol in symbols:
            bars = data_module.load_daily(symbol, group)
            bars = bars.set_index("date")
            closes[symbol] = bars["close"]
            opens[symbol] = bars["open"]
            vol_series = vol_module.yang_zhang(bars.reset_index(), VOL_WINDOW)
            vol_series.index = bars.index
            yz[symbol] = vol_series
        close_panel = pd.DataFrame(closes).sort_index()
        open_panel = pd.DataFrame(opens).reindex(close_panel.index)
        yz_panel = pd.DataFrame(yz).reindex(close_panel.index)
        index = close_panel.index

        prev_close = close_panel.shift(1)
        streams = {
            "cc": close_panel / prev_close - 1.0,
            "on": open_panel / prev_close - 1.0,
            "id": close_panel / open_panel - 1.0,
        }

        # Signals on closes, then shift(1): the value applied to day t was
        # decidable at the close of t-1.
        signals: dict[str, pd.DataFrame] = {"always": (close_panel.notna()).astype(float).shift(1)}
        for lookback in (126, 252):
            signals[f"tsmom{lookback}"] = (
                (close_panel > close_panel.shift(lookback)).astype(float)
                .where(close_panel.shift(lookback).notna()).shift(1))
        for length in (100, 200):
            sma = close_panel.rolling(length).mean()
            signals[f"ma{length}"] = ((close_panel >= sma).astype(float)
                                      .where(sma.notna()).shift(1))

        yz_shifted = yz_panel.shift(1)
        vol_scalars = {"off": pd.DataFrame(1.0, index=index, columns=list(symbols))}
        for target in (15, 20):
            vol_scalars[f"vt{target}"] = (target / 100.0 / yz_shifted).clip(upper=1.0)

        market = data_module.load_daily(market_symbol, market_group).set_index("date")
        market_yz = vol_module.yang_zhang(market.reset_index(), VOL_WINDOW)
        market_yz.index = market.index
        # Causal percentile: rank of today's vol within all history up to today.
        expanding_rank = market_yz.expanding().rank(pct=True)
        expanding_rank[market_yz.expanding().count() < REGIME_MIN_HISTORY] = np.nan
        pct = expanding_rank.reindex(index).ffill().shift(1)

        market_mult: dict[str, pd.Series] = {"none": pd.Series(1.0, index=index)}
        for threshold in (70, 80):
            stress = pct >= threshold / 100.0
            for stress_exposure, tag in ((0.0, "x0"), (0.5, "x50")):
                mult = pd.Series(1.0, index=index).where(~stress, stress_exposure)
                mult[pct.isna()] = 1.0
                market_mult[f"p{threshold}{tag}"] = mult

        market_close = market["close"].reindex(index).ffill()
        sma200 = market_close.rolling(200).mean()
        above = (market_close >= sma200).astype(float).where(sma200.notna()).shift(1).fillna(1.0)
        trend_mult = {"off": pd.Series(1.0, index=index), "qqq200": above}

        bar_count = close_panel.notna().cumsum()
        available = close_panel.notna() & (bar_count.shift(1) >= WARMUP_BARS)

        return cls(streams=streams, signals=signals, vol_scalars=vol_scalars,
                   market_mult=market_mult, trend_mult=trend_mult,
                   available=available, index=index)

    def run(self, config: tuple[str, str, str, str, str]) -> pd.Series:
        """Daily portfolio returns for one configuration.

        Args:
            config: (timing, signal, regime_gate, trend_gate, vol_target),
                keys of the respective panel dicts.

        Returns:
            Daily simple returns named by the config; days with an empty
            universe return 0 (all cash).
        """
        timing, signal, gate, trend, vt = config
        returns = self.streams[timing]
        exposure = (self.signals[signal] * self.vol_scalars[vt]).clip(upper=1.0)
        exposure = exposure.mul(self.market_mult[gate], axis=0).mul(self.trend_mult[trend], axis=0)

        mask = self.available & returns.notna() & exposure.notna()
        weighted = (exposure * returns).where(mask, 0.0)
        slots = mask.sum(axis=1)
        portfolio = weighted.sum(axis=1).div(slots.replace(0, np.nan)).fillna(0.0)
        portfolio.name = "|".join(config)
        return portfolio
