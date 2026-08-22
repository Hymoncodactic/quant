"""Trading 212 venue package, venue slug "t212".

Responsibility: declare the Trading 212 side of the project as a regular Python
package so that absolute imports rooted at "trading212" resolve. The venue's
code is split across the subpackages config, ingest, strategy and execution,
following the layering registered in ARCHITECTURE.md section 2.

Out of scope: order matching and performance statistics, which belong to
backtest/; infrastructure shared by both venues, which belongs to common/;
stored data bytes, which live under data/t212/; field and unit documentation,
which lives under docs/data/t212/; credentials, which live only in secrets/.

Public functions: None. This module defines no symbol and imports nothing on
purpose. Importing a subpackage here would make every consumer pay for pandas,
pyarrow and yfinance regardless of what it actually needs. Consumers import the
modules by their own paths, for example trading212.ingest.yahoo_bars and
trading212.strategy.a0_intraday_v0_0_1.

Constants: None.

Inputs: None.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""
