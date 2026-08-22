"""Backtest package: a venue-agnostic engine plus one adapter per venue.

Responsibility: provide the package namespace for all backtesting code. The
subpackages are backtest/engine/ (the venue-agnostic event-driven bar engine),
backtest/t212/ (the Trading 212 adapter) and backtest/okx/ (the OKX adapter,
data loading only so far). How to run a backtest, what the engine guarantees
and what each output file contains are documented in backtest/README.md.

Out of scope: everything with behavior. This file declares no symbol and runs
no code. Matching, ledger and metrics belong to backtest/engine/; venue cost,
calendar and fault modeling belong to backtest/t212/ and backtest/okx/;
signals belong to <venue>/strategy/, which the backtest imports rather than
reimplements (ARCHITECTURE.md section 2.0). No module under this package may
call an exchange interface (ARCHITECTURE.md section 2, backtest row).

Public functions: None.
Public classes: None. Nothing is re-exported here on purpose, so an importer
    names the module it depends on and the dependency direction stays visible.

Constants: None.

Inputs: None.
Outputs: None. Run artifacts are written by backtest/engine/results.py and
    backtest/engine/report.py into backtest/results/.

Change log:
    2026-08-22  Package docstring added to the six-section spec. The file was
                previously empty.
"""
