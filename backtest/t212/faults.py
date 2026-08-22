"""Fault injection for the T212 broker simulator.

Responsibility: reproduce the documented platform failure modes as togglable
rules (catalog and per-fault evidence: fixplans/t212_faults/01_fault_catalog.md;
latency regimes: fixplans/t212_faults/02_latency_model.md).
Not responsible for: order bookkeeping or cost math (broker_sim.py, costs.py).

Every random parameter without a measured distribution is marked INFERRED in
the catalog; treat those as sensitivity knobs. All randomness flows through
one seeded numpy Generator so identical configuration reproduces identical
results byte-for-byte.

Public classes and constants:
    FaultConfig     All fault switches and parameters of one run
    FaultEngine     Stateful evaluator consulted by the broker simulator
    FAULT_SWITCH_DEFAULTS   Switch registry: fault id -> default on/off
    BAR_SECONDS     Interval name -> seconds per bar
"""

from __future__ import annotations

__all__ = ["FaultConfig", "FaultEngine", "FAULT_SWITCH_DEFAULTS", "BAR_SECONDS"]

import math
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np
import pandas as pd

from backtest.engine.types import INTERVAL_SECONDS, Bar, OrderType

# ============================================================================
# [1] Registry and constants
# ============================================================================

# Fault ids follow fixplans/t212_faults/01_fault_catalog.md section 2.
# F10/F15 are real venue semantics kept togglable for sensitivity runs.
# F16 (market-closed queueing) is STRUCTURAL, not a switch: a market order
# queues because matching requires a bar, and no configuration can change
# that, so it is deliberately absent from this registry (catalog F16 row).
FAULT_SWITCH_DEFAULTS: dict[str, bool] = {
    "F1_latency_normal": True,
    "F2_latency_volatile": True,
    "F3_outage_window": True,
    "F4_reduce_only_window": True,
    "F5_reject_random": True,
    "F6_duplicate_on_retry": True,
    "F7_cancel_race": True,
    "F8_quantity_precision": True,
    "F9_buying_power_buffer": True,
    "F10_sell_reservation": True,
    "F11_stale_ticker": True,
    "F12_submit_pacing": True,
    "F13_partial_fill": True,
    "F14_auth_outage_window": True,
    "F15_day_expiry": True,
}

# Interval seconds live in engine/types.py (they are engine-wide time facts);
# the historical alias stays because the venue adapter reads them constantly.
BAR_SECONDS = INTERVAL_SECONDS

_ROLLING_WINDOW = 20  # bars of range/volume history for the volatile trigger


@dataclass
class FaultConfig:
    """Fault switches and parameters. Defaults = authoritative worst tier."""
    switches: dict[str, bool] = field(
        default_factory=lambda: dict(FAULT_SWITCH_DEFAULTS))
    seed: int = 20260820
    # F5/F6: community 61788 posts 63/66/121, 87988 post 80. Probabilities
    # INFERRED (existence documented, rates not).
    reject_prob: float = 0.02
    duplicate_prob: float = 0.10
    # F7: spec: "Cancellation is not guaranteed if the order is already in
    # the process". Probability INFERRED.
    cancel_race_prob: float = 0.50
    # F9: community 2025-10 + the official 95% quantity/value conversion
    # threshold (helpcentre 7897588388125).
    buying_power_factor: Decimal = Decimal("0.95")
    # F8: community 87988 posts 125/151: "invalid quantity precision 4".
    qty_decimals: int = 4
    # F13: zipline volume_limit default, vendor/zipline-reloaded slippage.py.
    volume_participation: float = 0.10
    # F2 trigger: bar range or volume above k x rolling median. k INFERRED.
    volatile_trigger_mult: float = 3.0
    # Latency seconds (fixplans/t212_faults/02_latency_model.md section 2):
    # L0 official "usually within seconds" + 20 s community report;
    # L1 evidence spans roughly 1-26 minutes.
    latency_normal_sec: tuple[float, float] = (1.0, 20.0)
    latency_volatile_sec: tuple[float, float] = (60.0, 1560.0)
    # F3/F14 windows: [start, end] ISO UTC strings, inclusive-exclusive.
    outage_windows: list[tuple[str, str]] = field(default_factory=list)
    auth_outage_windows: list[tuple[str, str]] = field(default_factory=list)
    # F4: (symbol, start, end) buy-blocked windows.
    reduce_only_windows: list[tuple[str, str, str]] = field(default_factory=list)
    # F11: symbols the API cannot see at all.
    stale_tickers: set[str] = field(default_factory=set)
    # Functional cap: 50 pending orders per ticker (official docs).
    max_pending_per_ticker: int = 50

    @staticmethod
    def all_off(seed: int = 20260820) -> "FaultConfig":
        """Ideal-execution comparison arm: every switch off."""
        return FaultConfig(switches={k: False for k in FAULT_SWITCH_DEFAULTS},
                           seed=seed)

    def on(self, fault_id: str) -> bool:
        return bool(self.switches.get(fault_id, False))


# ============================================================================
# [2] Evaluator
# ============================================================================

class FaultEngine:
    """Stateful fault evaluator; one instance per backtest run."""

    def __init__(self, cfg: FaultConfig, interval: str) -> None:
        if interval not in BAR_SECONDS:
            raise ValueError(f"unknown interval {interval!r}")
        self.cfg = cfg
        self.bar_seconds = BAR_SECONDS[interval]
        self._rng = np.random.default_rng(cfg.seed)
        self._ranges: dict[str, deque] = {}
        self._volumes: dict[str, deque] = {}
        self._out = [(pd.Timestamp(a, tz="UTC"), pd.Timestamp(b, tz="UTC"))
                     for a, b in cfg.outage_windows]
        self._auth = [(pd.Timestamp(a, tz="UTC"), pd.Timestamp(b, tz="UTC"))
                      for a, b in cfg.auth_outage_windows]
        self._reduce = [(s, pd.Timestamp(a, tz="UTC"), pd.Timestamp(b, tz="UTC"))
                        for s, a, b in cfg.reduce_only_windows]

    # ------------------------------------------------------------------
    # [2.1] Rolling stats for the volatile trigger (F2)
    # ------------------------------------------------------------------

    def observe_bar(self, symbol: str, bar: Bar) -> None:
        """Feed one delivered bar into the rolling range/volume windows."""
        rng_frac = (bar.high - bar.low) / bar.close if bar.close else 0.0
        self._ranges.setdefault(symbol, deque(maxlen=_ROLLING_WINDOW)).append(rng_frac)
        self._volumes.setdefault(symbol, deque(maxlen=_ROLLING_WINDOW)).append(bar.volume)

    def is_volatile(self, symbol: str, bar: Bar) -> bool:
        """F2 trigger: range or volume above k x rolling median."""
        ranges = self._ranges.get(symbol)
        if not ranges or len(ranges) < _ROLLING_WINDOW // 2:
            return False
        k = self.cfg.volatile_trigger_mult
        med_r = float(np.median(ranges))
        med_v = float(np.median(self._volumes[symbol]))
        rng_frac = (bar.high - bar.low) / bar.close if bar.close else 0.0
        return (med_r > 0 and rng_frac > k * med_r) or \
               (med_v > 0 and bar.volume > k * med_v)

    # ------------------------------------------------------------------
    # [2.2] Submission-time checks
    # ------------------------------------------------------------------

    def submit_blocked(self, ts: pd.Timestamp, symbol: str,
                       is_buy: bool) -> str | None:
        """Window and universe checks; returns a reject reason or None."""
        if self.cfg.on("F3_outage_window") and _in_windows(ts, self._out):
            return "outage_window"
        if self.cfg.on("F14_auth_outage_window") and _in_windows(ts, self._auth):
            return "auth_outage_401"
        if self.cfg.on("F11_stale_ticker") and symbol in self.cfg.stale_tickers:
            return "entity_not_found"
        if self.cfg.on("F4_reduce_only_window") and is_buy:
            for sym, a, b in self._reduce:
                if sym == symbol and a <= ts < b:
                    return "reduce_only"
        return None

    def reject_roll(self) -> tuple[bool, bool]:
        """F5/F6: (rejected_now, actually_accepted_anyway).

        The second flag is the duplicate trap: the venue reported failure but
        the order lives, so a client-side retry doubles the position (official
        non-idempotency warning). The roll is consumed even when F5 is off so
        that toggling F5 does not shift every later random draw.
        """
        u1 = float(self._rng.uniform())
        u2 = float(self._rng.uniform())
        if not self.cfg.on("F5_reject_random"):
            return False, False
        rejected = u1 < self.cfg.reject_prob
        duplicate = (rejected and self.cfg.on("F6_duplicate_on_retry")
                     and u2 < self.cfg.duplicate_prob)
        return rejected, duplicate

    def cancel_succeeds(self, order_fillable_this_bar: bool) -> bool:
        """F7: a cancel racing a fill loses with probability cancel_race_prob."""
        u = float(self._rng.uniform())
        if not self.cfg.on("F7_cancel_race") or not order_fillable_this_bar:
            return True
        return u >= self.cfg.cancel_race_prob

    def effective_buying_power(self, available_gbp: Decimal) -> Decimal:
        """F9: the venue admits buys only up to a fraction of free cash."""
        if not self.cfg.on("F9_buying_power_buffer"):
            return available_gbp
        return available_gbp * self.cfg.buying_power_factor

    def qty_decimals(self) -> int | None:
        """F8: max accepted decimal places of order quantity, None = no cap."""
        return self.cfg.qty_decimals if self.cfg.on("F8_quantity_precision") else None

    # ------------------------------------------------------------------
    # [2.3] Latency and pacing
    # ------------------------------------------------------------------

    def latency_seconds(self, symbol: str, bar: Bar | None,
                        order_type: OrderType) -> float:
        """Execution latency draw in seconds for a market leg; 0.0 when the
        order type is not market or both latency switches are off.

        Both uniforms are drawn on every call regardless of switches so that
        toggling F1/F2 in a sensitivity arm never reshuffles the draws of
        other faults (same discipline as reject_roll). Regimes follow
        fixplans/t212_faults/02_latency_model.md section 2.
        """
        u_norm = float(self._rng.uniform())
        u_vol = float(self._rng.uniform())
        if order_type is not OrderType.MARKET:
            return 0.0
        if (self.cfg.on("F2_latency_volatile") and bar is not None
                and self.is_volatile(symbol, bar)):
            lo, hi = self.cfg.latency_volatile_sec
            return float(np.exp(np.log(lo) + (np.log(hi) - np.log(lo)) * u_vol))
        if self.cfg.on("F1_latency_normal"):
            low, high = self.cfg.latency_normal_sec
            return low + (high - low) * u_norm
        return 0.0

    def bars_from_seconds(self, delay_sec: float) -> int:
        """Bar intervals an order waits before it may match, minimum 1.

        Baseline (no latency) is 1: the next full interval, never the same
        bar. d seconds map to max(1, ceil(d / bar_seconds)); the caller turns
        intervals into a TIME on the order (eligible_ts), never a step count.
        """
        if delay_sec <= 0:
            return 1
        return max(1, math.ceil(delay_sec / self.bar_seconds))

    def latency_extra_bars(self, symbol: str, bar: Bar | None,
                           order_type: OrderType) -> int:
        """Draw latency and convert to bar intervals (see latency_seconds)."""
        return self.bars_from_seconds(self.latency_seconds(symbol, bar,
                                                           order_type))

    def submit_caps_per_bar(self) -> tuple[int, int]:
        """F12: (market_cap, pending_type_cap) submissions per bar.

        Official per-endpoint limits: market 50/60s, limit/stop/stop_limit
        1/2s (docs.trading212.com/api.md). Folded to bar granularity.
        """
        if not self.cfg.on("F12_submit_pacing"):
            big = 10 ** 9
            return big, big
        market = max(1, self.bar_seconds * 50 // 60)
        pending = max(1, self.bar_seconds // 2)
        return market, pending

    def volume_cap_shares(self, bar: Bar) -> Decimal | None:
        """F13: max shares fillable on this bar, None = uncapped."""
        if not self.cfg.on("F13_partial_fill"):
            return None
        return Decimal(str(bar.volume)) * Decimal(str(self.cfg.volume_participation))


def _in_windows(ts: pd.Timestamp,
                windows: list[tuple[pd.Timestamp, pd.Timestamp]]) -> bool:
    return any(a <= ts < b for a, b in windows)
