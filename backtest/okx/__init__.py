"""OKX backtest adapter package for the crypto line.

Responsibility: expose the crypto-side backtest entry points. Only the data
loading layer exists today, so load_klines is the single re-export. The line's
data inventory, its conventions and the differences from the equity line are
recorded in backtest/okx/README.md.

Out of scope: matching, cost and fault modeling for OKX. Those modules are not
built yet. They are blocked on S4 evidence for OKX spot fee tiers, for the
lotSz / minSz / tickSz returned by GET /api/v5/public/instruments, and for the
rate-limit and cancel semantics (backtest/okx/README.md section 3, and
fixplans/framework/06_strategy_plugin.md section 4); until they exist this line
produces no backtest conclusion. Venue-agnostic mechanics belong to
backtest/engine/.

Public functions:
    load_klines(...)
        Re-exported from backtest.okx.data_source: crypto bars from the local
        Binance archive, reduced to the engine bar schema.

Constants: None.

Inputs: None here. backtest.okx.data_source reads the Binance curated
    partitions under data/binance/curated/.
Outputs: None.

Change log:
    2026-08-22  Header expanded to the six-section spec. The earlier note that
                the matching and cost adapter awaits S4 fee research is kept
                under "Out of scope".
"""

from backtest.okx.data_source import load_klines

__all__ = ["load_klines"]
