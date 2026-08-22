"""Execution-side loader for versioned strategy modules.

Responsibility: turn a (name, version) pair into the strategy callable that
trading212/execution/ runs, loading the module from its versioned path and
verifying that the module's declared identity matches what was requested.
Loading by path rather than by package import keeps trading212/strategy/
free of imports, which is what stops every consumer of that package from
pulling numpy and pandas (trading212/strategy/__init__.py, Out of scope).

Out of scope: the signal itself, which belongs to
trading212/strategy/a0_v0_0_1.py; the intraday timing shim, which belongs to
trading212/strategy/a0_intraday_v0_0_1.py; parameter values, which belong to
trading212/config/strategies/ and are read once by the entry layer;
backtest-side loading, which belongs to backtest/engine/strategy_loader.py.
That module and this one are deliberately separate because the execution
layer must never import backtest code (ARCHITECTURE.md section 2), yet both
resolve to the SAME strategy file, so the signal still has one copy only.

Public functions:
    strategy_path(name, version)     Path of one versioned strategy module.
    load_module(name, version)       Import it and verify its identity.
    load_strategy(name, version)     Return its compute_targets callable.
    load_intraday_strategy(name, version, daily_history)
                                     Return the callable bound to a daily
                                     history through make_strategy(), which
                                     keeps that history out of run metadata.

Constants:
    ENTRY_POINT     str  "compute_targets", the plugin contract entry name.
                         Source: docs/backtest/framework/06_strategy_plugin.md
                         section 2.
    FACTORY_POINT   str  "make_strategy", the optional factory an intraday
                         shim exposes to bind its injected daily history.
                         Source: trading212/strategy/a0_intraday_v0_0_1.py.

Inputs:
    trading212/strategy/<name>_v<major>_<minor>_<patch>.py
Outputs:
    None.

Change log:
    2026-08-22  Created. The registry that previously lived in
                trading212/strategy/__init__.py was removed when that package
                was declared import-free; path loading replaces it and also
                covers the intraday factory, which a plain registry could not.
"""

from __future__ import annotations

__all__ = ["strategy_path", "load_module", "load_strategy",
           "load_intraday_strategy", "ENTRY_POINT", "FACTORY_POINT"]

import importlib.util
from pathlib import Path
from types import ModuleType

from common.paths import venue_dir

ENTRY_POINT = "compute_targets"
FACTORY_POINT = "make_strategy"


def strategy_path(name: str, version: str) -> Path:
    """Return the file path of one versioned strategy module.

    Version "0.1.2" maps to the suffix "_v0_1_2.py"; file names carry no dots
    (quant-code-standards section 4.5.1).
    """
    return venue_dir("t212") / "strategy" / f"{name}_v{version.replace('.', '_')}.py"


def load_module(name: str, version: str) -> ModuleType:
    """Import one strategy module and verify it declares the requested identity.

    A module whose STRATEGY_NAME or STRATEGY_VERSION disagrees with its file
    name would attribute live orders to the wrong logic version, so the
    mismatch is fatal rather than a warning.
    """
    path = strategy_path(name, version)
    if not path.is_file():
        raise FileNotFoundError(f"no strategy module at {path}")
    spec = importlib.util.spec_from_file_location(
        f"t212_strategy_{name}_v{version.replace('.', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    declared = (getattr(module, "STRATEGY_NAME", None),
                getattr(module, "STRATEGY_VERSION", None))
    if declared != (name, version):
        raise ValueError(
            f"{path} declares {declared}; requested {(name, version)}. File "
            f"identity and module identity must agree.")
    return module


def load_strategy(name: str, version: str):
    """Return the module's compute_targets callable."""
    module = load_module(name, version)
    entry = getattr(module, ENTRY_POINT, None)
    if not callable(entry):
        raise ValueError(f"{strategy_path(name, version)} lacks a callable "
                         f"{ENTRY_POINT}()")
    return entry


def load_intraday_strategy(name: str, version: str, daily_history: dict):
    """Return the callable produced by the module's make_strategy() factory.

    The intraday shim needs a daily history that is far too large to travel
    inside params, so it exposes a factory that binds the history as a
    closure. A module without the factory falls back to compute_targets, which
    then expects the history inside params itself.
    """
    module = load_module(name, version)
    factory = getattr(module, FACTORY_POINT, None)
    if callable(factory):
        return factory(daily_history)
    return load_strategy(name, version)
