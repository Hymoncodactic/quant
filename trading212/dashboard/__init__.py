"""Trading 212 dashboard package.

Responsibility: declare the dashboard as a regular Python package so that
"from trading212.dashboard import server" resolves.

Out of scope: everything the dashboard does. Serving belongs to server.py,
sampling to collector.py, persistence to snapshots.py, quotes to quotes.py,
configuration editing to settings.py, and the HTTP surface to api.py. This
module imports none of them, so importing the package stays free of pandas,
of network clients, and of import-order effects.

Public functions: None. This module defines no symbol.

Constants: None.

Inputs: None.
Outputs: None.

Change log:
    2026-08-22  Created.
"""
