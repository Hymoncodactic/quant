"""The frozen strategy family for the regime study.

450 configurations = 3 timings x 5 signals x 5 regime gates x 2 trend gates
x 3 vol targets. Adding a member after the search has run is a protocol breach
(prereg section on the family freeze).

Public:
    FAMILY          Tuple of all config tuples
    config_label    "timing|signal|gate|trend|vt" string
"""

from __future__ import annotations

__all__ = ["FAMILY", "config_label", "TIMINGS", "SIGNALS", "GATES", "TRENDS", "VOL_TARGETS"]

import itertools

TIMINGS = ("cc", "on", "id")
SIGNALS = ("always", "tsmom252", "tsmom126", "ma200", "ma100")
GATES = ("none", "p70x0", "p70x50", "p80x0", "p80x50")
TRENDS = ("off", "qqq200")
VOL_TARGETS = ("off", "vt15", "vt20")

FAMILY = tuple(itertools.product(TIMINGS, SIGNALS, GATES, TRENDS, VOL_TARGETS))


def config_label(config: tuple[str, str, str, str, str]) -> str:
    """Canonical label for one configuration."""
    return "|".join(config)
