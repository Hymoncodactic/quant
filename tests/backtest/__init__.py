"""Package holding the backtest unit and regression tests.

Responsibility: declare tests.backtest a regular package so the test modules
in it can import their shared fixtures by absolute dotted path, for example
"from tests.backtest.conftest import mk_broker, mk_ledger". Five modules in
this directory rely on that path.

Out of scope: the fixtures themselves, which live in
tests/backtest/conftest.py; the code under test, which lives in backtest/ and
in trading212/strategy/.

Public functions: None.

Public classes: None.

Constants: None.

Inputs: None.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""
