"""T212 strategy registry: versioned modules behind a dispatch table.

Adding a strategy version = adding a module file + one registry entry
(quant-code-standards sections 4.5.1 and 4.6); nothing else changes. The
backtest reaches the same modules through
backtest/engine/strategy_loader.py; this registry is the execution-side
entry so the two layers stay import-independent while sharing the single
signal copy (ARCHITECTURE.md section 2.0).

Public functions:
    get_strategy(name, version)   The module's compute_targets callable
"""

from __future__ import annotations

__all__ = ["get_strategy", "STRATEGIES"]

from trading212.strategy import a0_v0_0_1

# (name, version) -> module. Every module must declare STRATEGY_NAME /
# STRATEGY_VERSION and a pure compute_targets(view, portfolio, params).
STRATEGIES = {
    ("a0", "0.0.1"): a0_v0_0_1,
}


def get_strategy(name: str, version: str):
    """Return compute_targets for one registered strategy version.

    Raises:
        KeyError: Unknown (name, version).
        ValueError: The module's declared identity disagrees with the key
            (the registry would then attribute results to the wrong logic).
    """
    key = (name, version)
    if key not in STRATEGIES:
        raise KeyError(f"unregistered strategy {key}; known: {sorted(STRATEGIES)}")
    module = STRATEGIES[key]
    if (module.STRATEGY_NAME, module.STRATEGY_VERSION) != key:
        raise ValueError(f"registry key {key} maps to a module declaring "
                         f"({module.STRATEGY_NAME!r}, {module.STRATEGY_VERSION!r})")
    return module.compute_targets
