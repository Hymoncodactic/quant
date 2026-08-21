"""OKX signal package, currently an empty skeleton.

Responsibility: namespace root that holds the single copy of every OKX signal.
No strategy module has been written yet. A module added here must be a pure
function of a market view and a portfolio returning target positions, with no
network access, no state writes and no order placement. The naming and
constant contract is enforced by backtest/engine/strategy_loader.py: the file
is named <name>_v<M>_<m>_<p>.py, the module defines STRATEGY_NAME and
STRATEGY_VERSION matching that file name, and the entry point is
compute_targets(view, portfolio, params) returning a mapping to Decimal.
trading212/strategy/a0_v0_0_1.py is an existing module in the same shape.

Out of scope: order placement, which belongs to crypto_trading/execution/;
parameter values, which belong to crypto_trading/config/strategies/; the
backtest driver, which belongs to backtest/. Both backtest/ and
crypto_trading/execution/ import their signal from here, so a separate
backtest copy and live copy of one signal must never exist.

Public functions: None.

Public classes: None.

Parameters / Constants: None.

Inputs / Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""
