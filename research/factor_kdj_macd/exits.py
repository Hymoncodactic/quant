"""Exit-rule registry for the exit search round.

Responsibility: name every exit rule frozen in
research/prereg/20260820_kdj_exit_search_prereg.md section 3.3 and build its
specification for engine.extract_trades_spec. Not responsible for: walking
bars, fills, statistics.

Three kinds of exit exist:
    mask    A causal boolean series over bars; the position closes on the first
            True at or after the entry fill bar, filled next open.
    trail   Close has fallen at least pct from the highest close since the entry
            fill bar (that bar's close included); filled next open.
    fixed   The position closes at the open exactly n trading days after the
            entry fill, with no signal bar.

Public functions:
    build_exit_spec(name, frame)   Materialize one exit for one indicator frame
    EXIT_NAMES                     Every legal exit name
    EXIT_FAMILY                    Exit name -> family, for the shortlist cap
"""

from __future__ import annotations

__all__ = ["build_exit_spec", "EXIT_NAMES", "EXIT_FAMILY"]

import numpy as np
import pandas as pd

from . import rules as rules_module


def _mask_death_cross(frame: pd.DataFrame) -> np.ndarray:
    """K crosses below D. The baseline exit of the previous round."""
    return rules_module.cross_down(frame["k"].to_numpy(), frame["d"].to_numpy())


def _mask_k_turn_down(frame: pd.DataFrame) -> np.ndarray:
    """K lower than on the previous bar. Causal proxy for 'the K peak'."""
    k = frame["k"].to_numpy()
    out = np.zeros(len(k), dtype=bool)
    out[1:] = k[1:] < k[:-1]
    return out


def _mask_k_over_80(frame: pd.DataFrame) -> np.ndarray:
    """K at or above 80: sell into the overbought zone."""
    return frame["k"].to_numpy() >= 80.0


def _mask_macd_death(frame: pd.DataFrame) -> np.ndarray:
    """DIF crosses below DEA: let the trend run until medium-term momentum flips."""
    return rules_module.cross_down(frame["dif"].to_numpy(), frame["dea"].to_numpy())


# Exit name -> ("mask", builder) | ("trail", pct) | ("fixed", n). Frozen in the
# prereg; adding an entry here without a prereg amendment is a protocol breach.
_SPECS: dict[str, tuple] = {
    "death_cross": ("mask", _mask_death_cross),
    "k_turn_down": ("mask", _mask_k_turn_down),
    "k_over_80": ("mask", _mask_k_over_80),
    "macd_death": ("mask", _mask_macd_death),
    "trail_5": ("trail", 0.05),
    "trail_10": ("trail", 0.10),
    "fixed_5": ("fixed", 5),
    "fixed_10": ("fixed", 10),
    "fixed_20": ("fixed", 20),
}

EXIT_NAMES = tuple(_SPECS)

# Family labels for the at-most-two-per-family shortlist cap, prereg 5.1.
EXIT_FAMILY = {
    "death_cross": "kdj_mask", "k_turn_down": "kdj_mask", "k_over_80": "kdj_mask",
    "macd_death": "macd_death",
    "trail_5": "trail", "trail_10": "trail",
    "fixed_5": "fixed", "fixed_10": "fixed", "fixed_20": "fixed",
}


def build_exit_spec(name: str, frame: pd.DataFrame) -> dict:
    """Materialize one exit specification for one indicator frame.

    Args:
        name: Key of EXIT_NAMES.
        frame: Indicator frame the mask-type exits are evaluated on.

    Returns:
        Dict consumed by engine.extract_trades_spec: {"kind": "mask", "mask": ...}
        or {"kind": "trail", "pct": ...} or {"kind": "fixed", "n": ...}.

    Raises:
        KeyError: Unknown exit name.
    """
    if name not in _SPECS:
        raise KeyError(f"unknown exit {name!r}, expected one of {EXIT_NAMES}")
    kind, payload = _SPECS[name]
    if kind == "mask":
        return {"kind": "mask", "mask": payload(frame)}
    if kind == "trail":
        return {"kind": "trail", "pct": payload}
    return {"kind": "fixed", "n": payload}
