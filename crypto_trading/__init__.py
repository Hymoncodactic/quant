"""OKX-side trading package for the crypto leg of the project.

Responsibility: namespace root that groups the crypto code and gives it a
stable absolute-import prefix. It holds no code of its own. Three subpackages
carry the layers: ingest for market data download and parsing, strategy for
the single copy of the signal logic, and execution for order placement and
reconciliation. A fourth subdirectory, config, holds non-secret YAML runtime
configuration and universe files and is deliberately not a Python package.
The venue slug "okx" maps to this directory in common/paths.py, so both
venue_dir and config_dir resolve here for that slug.

Out of scope: infrastructure shared by both legs, such as paths, configuration
loading, secrets, logging, networking and storage, which lives in common/;
backtesting, which lives in backtest/; the Trading 212 leg, which lives in
trading212/; credentials, which live in secrets/.

Public functions: None.

Public classes: None.

Parameters / Constants: None.

Inputs / Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""
