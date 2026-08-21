"""Crypto market data ingest package.

Responsibility: namespace root for the crypto data acquisition layer. It
currently contains binance_archive, a client for the public Binance bulk
archive at data.binance.vision, and schemas, the per-dataset column layout
table that binance_archive parses with. No OKX endpoint is covered yet. Data
source and trading venue are separate concepts here: Binance supplies research
data only, while orders go to OKX, and common/paths.py keeps the two apart as
DATA_SOURCES and VENUES.

Out of scope: writing parquet, which the entry scripts under scripts/ perform
through common/store.py; partition path construction, which belongs to
common/paths.py; the recorded meaning of the landed columns, which belongs to
docs/data/binance/; any trading decision.

Public functions: None. The package exposes no symbols of its own; importers
use the fully qualified module path, for example
"from crypto_trading.ingest import binance_archive".

Public classes: None.

Parameters / Constants: None.

Inputs / Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""
