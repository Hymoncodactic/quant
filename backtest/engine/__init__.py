"""Venue-neutral event-driven backtest engine package.

Responsibility: declare the public API surface of the engine by re-exporting
the twenty names listed in __all__ from the submodules, so a consumer can
depend on one import path instead of the internal module split. The module map
is registered in ARCHITECTURE.md section 2.2 and the design plans are in
fixplans/framework/.

Out of scope: all behavior. Bar streaming lives in feed.py, matching rules in
matching.py, accounting in ledger.py, the main loop in engine.py, statistics in
metrics.py, persistence in results.py, charting in report.py and strategy
loading in strategy_loader.py. Every venue-specific rule (fees, taxes, trading
calendars, fault probabilities, annualization factor) lives in
backtest/<venue>/.

Public classes and functions, in __all__ order:
    BrokerSim        Protocol every broker simulator implements.
    BacktestEngine   Drives one backtest run across the whole timeline.
    PortfolioView    Read-only account snapshot handed to the strategy.
    RunResult        Trades, equity and orders frames plus run metadata.
    BarFeed          Aligned multi-symbol bar stream.
    FxSeries         Lookahead-free GBPUSD rate lookup.
    MarketView       Cutoff-enforced market view for the strategy.
    Ledger           GBP cash, positions, occupancy and equity records.
    Position         One open position: quantity and GBP cost basis.
    compute_metrics  All mandatory performance and risk statistics of one run.
    run_name         Canonical result file-name stem for one run.
    write_run        Write the trades / equity / meta triplet of one run.
    Bar              One OHLCV bar in its source quote currency.
    EngineConfig     Complete configuration of one run.
    Fill             One execution with itemized GBP costs.
    Order            Broker-side order state.
    OrderSpec        Strategy-side order request.
    OrderStatus      Simulator order lifecycle states.
    OrderType        MARKET, LIMIT, STOP, STOP_LIMIT.
    TimeInForce      DAY or GOOD_TILL_CANCEL.

The four matching functions of matching.py and the public symbols of report.py
and strategy_loader.py are deliberately absent from this list; their consumers
import the submodule directly.

Constants: None.

Inputs: None.
Outputs: None. Importing this package performs no file or network access.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from backtest.engine.broker import BrokerSim
from backtest.engine.engine import BacktestEngine, PortfolioView, RunResult
from backtest.engine.feed import BarFeed, FxSeries, MarketView
from backtest.engine.ledger import Ledger, Position
from backtest.engine.metrics import compute_metrics
from backtest.engine.results import run_name, write_run
from backtest.engine.types import (Bar, EngineConfig, Fill, Order, OrderSpec,
                                   OrderStatus, OrderType, TimeInForce)

__all__ = ["BrokerSim", "BacktestEngine", "PortfolioView", "RunResult",
           "BarFeed", "FxSeries", "MarketView", "Ledger", "Position",
           "compute_metrics", "run_name", "write_run", "Bar", "EngineConfig",
           "Fill", "Order", "OrderSpec", "OrderStatus", "OrderType",
           "TimeInForce"]
