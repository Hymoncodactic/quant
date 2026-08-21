"""Trading 212 backtest adapter package for the equity line.

Responsibility: expose the T212 adapter as one import surface -- the broker
simulator, the cost configuration, the fault configuration with its evaluator,
and the run composition function. The module map is in ARCHITECTURE.md
section 2.2, the venue-specific conventions are in backtest/t212/README.md, and
the design plans with their change records are in fixplans/ (framework and
t212_faults).

Out of scope: venue-agnostic mechanics, which belong to backtest/engine/;
signal logic, which belongs to trading212/strategy/ and is passed in by the
runner's caller; any network or order-placing behavior, which the backtest
never performs (ARCHITECTURE.md section 2, backtest row).

Public classes:
    T212BrokerSim   Order admission, lifecycle, matching and fill costing.
    CostConfig      Cost parameters of one run; worst tier by default.
    FaultConfig     Fault switches and parameters of one run.
    FaultEngine     Stateful fault evaluator consulted by the simulator.
Public functions:
    run_t212_backtest(...)   Wire and run one backtest end to end.

Constants: None.

Inputs: None here. backtest.t212.data_source reads data/t212/curated/.
Outputs: None here. backtest.t212.runner writes into backtest/results/.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from backtest.t212.broker_sim import T212BrokerSim
from backtest.t212.costs import CostConfig
from backtest.t212.faults import FaultConfig, FaultEngine
from backtest.t212.runner import run_t212_backtest

__all__ = ["T212BrokerSim", "CostConfig", "FaultConfig", "FaultEngine",
           "run_t212_backtest"]
