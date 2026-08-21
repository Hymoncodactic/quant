"""Test root package for the repository.

Responsibility: declare tests/ a regular package so that test modules resolve
each other by absolute dotted path, for example
"from tests.backtest.conftest import mk_broker". Naming the package is what
makes that import work under every pytest import mode.

Out of scope: test code itself, which lives in tests/backtest/; checks against
real downloaded data, which are excluded from this tree by
fixplans/validation/02_test_plan.md section 2 and live in dated one-off
scripts under scripts/.

Public functions: None.

Public classes: None.

Constants: None.

Inputs: None.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""
