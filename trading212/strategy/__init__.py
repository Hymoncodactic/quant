"""Trading 212 strategy package: the single copy of this venue's signals.

Responsibility: declare the strategy layer as a regular Python package, so that
"from trading212.strategy import a0_v0_0_1" resolves. The package holds the one
and only implementation of each signal; the backtest and any future execution
layer import the same file, so a "backtest version" and a "live version" cannot
diverge. Modules here are pure functions: they take a market view, a portfolio
and a parameter mapping, and return target share counts.

Naming rule per ARCHITECTURE.md section 2.0.1: a module is named
<name>_v<major>_<minor>_<patch>.py, and its STRATEGY_NAME and STRATEGY_VERSION
must agree with the file name or backtest/engine/strategy_loader.py refuses to
load it. Note that strategy_loader.py loads by file path through
importlib.util.spec_from_file_location and therefore bypasses this package;
only direct imports such as the one in a0_intraday_v0_0_1.py use it.

Out of scope: parameter values, which belong to trading212/config/strategies/
and are read once by the entry layer and passed in through params; fill
simulation and cost modeling, which belong to backtest/t212/; order submission,
which belongs to trading212/execution/.

Public functions: None. This module defines no symbol and imports nothing.
Importing a strategy module here would give every consumer numpy and pandas,
and would also make module import order matter for a layer whose whole contract
is purity.

Constants: None.

Inputs: None.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""
