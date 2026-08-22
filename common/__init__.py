"""Venue-independent infrastructure shared by both trading lines and the backtester.

Responsibility: declare common/ as a regular package so that every
`from common.X import ...` in this project resolves against it. The package
collects the modules that carry no venue-specific convention: path construction
(paths.py), configuration loading (config.py), credential reading (secrets.py),
logger initialization (logging_setup.py), retry timing and rate limiting
(net.py), and Parquet storage (store.py).

Out of scope: venue-specific conventions such as fee schedules, order-size
precision, trading calendars and endpoint field names, which belong to
crypto_trading/ and trading212/; strategy signals, which belong to
<venue>/strategy/; backtest logic, which belongs to backtest/. This package
imports only the standard library and third-party libraries, never project
modules outside common/ (ARCHITECTURE.md section 2).

Public functions: None. This file defines no symbols; the submodules are imported
    directly, for example `from common.paths import data_dir`.

Public classes: None.

Constants: None.

Inputs:
    None.
Outputs:
    None.

Change log:
    2026-08-22  Header expanded to the six-section spec. The file previously held
                no content at all.
"""
