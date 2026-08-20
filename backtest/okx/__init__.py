"""OKX backtest adapter (crypto line). Data loading only for now: the
matching/cost adapter is pending S4 fee research, see
fixplans/framework/06_strategy_plugin.md section 4."""

from backtest.okx.data_source import load_klines

__all__ = ["load_klines"]
