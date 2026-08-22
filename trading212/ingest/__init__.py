"""Trading 212 market data ingest package.

Responsibility: declare the ingest layer as a regular Python package, so that
"from trading212.ingest import yahoo_bars" resolves. The layer holds the code
that fetches equity bars, reduces them to the project schema and writes them
into data/t212/curated/. yahoo_bars is currently the only module in it.

Out of scope: trading decisions, which belong to trading212/strategy/; order
submission, which belongs to trading212/execution/; field, unit and time zone
documentation, which belongs to docs/data/t212/DATA_SPEC.md; scheduling and
progress reporting, which belong to scripts/update_data.py.

Public functions: None. This module defines no symbol and imports nothing.
Importing yahoo_bars here would pull pandas and pyarrow into every consumer of
the package, so the module is imported by its own path instead.

Constants: None.

Inputs: None.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""
