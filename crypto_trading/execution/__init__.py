"""OKX order execution package, currently an empty skeleton.

Responsibility: namespace root for the OKX execution layer, which is to hold
order placement and cancellation, the order state machine, cancel-and-replace,
position and cash reconciliation, and the resident main loop. No module has
been written yet, so the package only marks the directory as a regular Python
package. Two constraints bind every module added here: the layer must call
common.config.assert_live_allowed(cfg) immediately before any order-submitting
request, and the dry_run switch defaults to true in
crypto_trading/config/okx.example.yaml, so turning it off is a change to real
order behavior (see CLAUDE.md §3.1).

Out of scope: signal computation, which belongs to crypto_trading/strategy/
and must be imported from there rather than duplicated here; market data
download, which belongs to crypto_trading/ingest/; risk limits and execution
parameters, whose values belong to crypto_trading/config/okx.<env>.yaml;
backtesting, which belongs to backtest/.

Public functions: None.

Public classes: None.

Parameters / Constants: None.

Inputs / Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""
