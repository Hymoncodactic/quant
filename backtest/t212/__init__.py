"""Trading 212 backtest adapter. Module map in ARCHITECTURE.md section 2.2;
design plans in fixplans/ (framework + t212_faults)."""

from backtest.t212.broker_sim import T212BrokerSim
from backtest.t212.costs import CostConfig
from backtest.t212.faults import FaultConfig, FaultEngine
from backtest.t212.runner import run_t212_backtest

__all__ = ["T212BrokerSim", "CostConfig", "FaultConfig", "FaultEngine",
           "run_t212_backtest"]
