"""Load versioned strategy modules from <venue>/strategy/ for backtesting.

Responsibility: turn a (venue, name, version) triple into the strategy's pure
compute_targets function, after verifying that the module honors the
single-copy contract. The SAME module will later be imported by
<venue>/execution/, so the backtest and live trading can never run different
signal code (ARCHITECTURE.md section 2.0). The module's own STRATEGY_NAME and
STRATEGY_VERSION must match the request: a mismatch means the file name lies
about its identity, and a backtest result would then be attributed to the wrong
logic version, so the load is refused rather than made compatible. The full
contract text is docs/backtest/framework/06_strategy_plugin.md:
    file        <venue_dir>/strategy/<name>_v<M>_<m>_<p>.py
    constants   STRATEGY_NAME = "<name>", STRATEGY_VERSION = "<M>.<m>.<p>"
    function    compute_targets(view, portfolio, params) -> dict[str, Decimal]
                pure: no network, no state writes, no order placement.

Out of scope: the strategy content itself, which lives in <venue>/strategy/,
and parameter loading, which reads <venue>/config/strategies/ at the entry
layer.

Public functions:
    strategy_path(venue, name, version, strategy_dir)   File path of one
                                                        versioned strategy
                                                        module.
    load_strategy(venue, name, version, strategy_dir)   Import that module and
                                                        return its
                                                        compute_targets, or
                                                        raise on a contract
                                                        violation.

Constants:
    _ENTRY_POINT   str   "compute_targets", the single function name the
                   contract requires a strategy module to expose.

Inputs:
    <venue_dir>/strategy/<name>_v<M>_<m>_<p>.py, imported by file location.
    Version "0.1.2" maps to the file suffix "_v0_1_2.py", because file names
    carry no dots (quant-code-standards section 4.5.1). The folder resolves
    through common.paths.venue_dir unless strategy_dir is injected, which
    exists for tests.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["strategy_path", "load_strategy"]

import importlib.util
from pathlib import Path

from backtest.engine.engine import StrategyFn
from common.paths import venue_dir

_ENTRY_POINT = "compute_targets"


def strategy_path(venue: str, name: str, version: str,
                  strategy_dir: Path | str | None = None) -> Path:
    """Path of one versioned strategy module.

    Version "0.1.2" maps to file suffix "_v0_1_2.py" (file names carry no
    dots, quant-code-standards section 4.5.1). strategy_dir is injectable for
    tests; the default resolves through common/paths.
    """
    folder = Path(strategy_dir) if strategy_dir is not None \
        else venue_dir(venue) / "strategy"
    return folder / f"{name}_v{version.replace('.', '_')}.py"


def load_strategy(venue: str, name: str, version: str,
                  strategy_dir: Path | str | None = None) -> StrategyFn:
    """Import one strategy module and return its compute_targets function.

    The module's own STRATEGY_NAME / STRATEGY_VERSION must match the request:
    a mismatch means the file name lies about its identity, and a backtest
    result would then be attributed to the wrong logic version.

    Raises:
        FileNotFoundError: No module at the versioned path.
        ValueError: Contract violation (missing or mismatched constants,
            missing or non-callable compute_targets).
    """
    path = strategy_path(venue, name, version, strategy_dir)
    if not path.is_file():
        raise FileNotFoundError(f"no strategy module at {path}")
    spec = importlib.util.spec_from_file_location(
        f"strategy_{venue}_{name}_v{version.replace('.', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    declared_name = getattr(module, "STRATEGY_NAME", None)
    declared_version = getattr(module, "STRATEGY_VERSION", None)
    if declared_name != name or declared_version != version:
        raise ValueError(
            f"{path} declares STRATEGY_NAME={declared_name!r}, "
            f"STRATEGY_VERSION={declared_version!r}; requested "
            f"({name!r}, {version!r}). File identity and module identity "
            f"must agree.")
    entry = getattr(module, _ENTRY_POINT, None)
    if not callable(entry):
        raise ValueError(f"{path} lacks a callable {_ENTRY_POINT}()")
    return entry
