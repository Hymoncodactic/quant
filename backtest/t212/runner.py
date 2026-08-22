"""Wire a full T212 backtest run: data, feed, broker, engine, metrics, files.

Responsibility: the one composition point. Scripts and tests call this instead
of assembling components by hand, so every run gets the same wiring and the
same metadata. The fee tier selects the defaults
(fixplans/framework/04_cost_model.md section 6): "worst" means CostConfig()
defaults with every fault switch on and is the authoritative tier, while
"actual" means CostConfig.actual_tier() with every fault switch off and is the
comparison tier. An explicit cost_cfg or fault_cfg overrides that mapping for
experiments, and whichever configuration is used is serialized into the run
metadata together with an honesty stamp naming the fault switches that are on
but have nothing configured, so the report layer cannot claim a stress that
never fired.

Out of scope: any trading or accounting logic. Matching, ledger, metrics and
result persistence belong to backtest/engine/; cost arithmetic belongs to
backtest/t212/costs.py; fault parameters belong to backtest/t212/faults.py; the
strategy is supplied by the caller and lives in trading212/strategy/.

Public functions:
    run_t212_backtest(config, strategy, cost_cfg=None, fault_cfg=None,
                      data_root=None, out_dir=None, write=True)
        Run one backtest end to end. Returns (RunResult, metrics dict, written
        paths); the paths dict is empty when write is False. An unknown
        config.fee_tier raises rather than defaulting.
    daily_data_gaps(frames)
        Missing exchange-local trading days per symbol on daily data. A day
        counts as missing for a symbol when at least one OTHER symbol in the
        same exchange time zone has a bar, the symbol is inside its own
        first-to-last span, and it has none. Cross-exchange holiday asymmetry
        is thereby excluded, while genuine suspensions and data holes are
        counted rather than silently skipped (backtest-discipline section 2.8).
    liquidation_valuer(broker)
        The per-share exit-value closure injected into the engine. It values
        one share at what a market SELL would fetch right now by pricing a
        one-share sell through the real cost stack, so the exit mark can never
        drift from the fill mechanics; the flat PTM levy has no per-share form
        and is disclosed as unmodeled. The mid mark stays the diagnostic
        column, and the authoritative tier reads equity_liq_gbp for drawdown
        and final equity (backtest-discipline section 2.10: unrealizable marks
        must not carry the headline).

Constants: None. Run parameters arrive through EngineConfig, CostConfig and
    FaultConfig, and the annualization factor is imported from
    backtest/t212/instruments.py so the engine holds no venue default.

Inputs:
    data/t212/curated/<group>/<symbol>/<interval>/*.parquet, read through
    backtest.t212.data_source.load_bars and load_fx. When the FX series is
    absent at the requested interval the loader falls back to the 1d series and
    the bar duration follows.
Outputs (written only when write is True; out_dir defaults to
backtest/results/ through common.paths.DIR_BACKTEST_RESULTS, and <stem> is
built by backtest.engine.results.run_name from the config):
    <stem>.trades.parquet   One row per fill.
    <stem>.equity.parquet   Per-step cash, reserved, occupied and equity.
    <stem>.meta.json        Config, metrics, fault switches, order audit.
    <stem>.chart.html       Derived equity chart, outside the byte-identity
                            guarantee that covers the other three files.

Change log:
    2026-08-22  Header expanded to the six-section spec; the fee-tier mapping
                of the previous header is carried over into "Responsibility".
    2026-08-22  fill_timing validated and passed to the broker.
"""

from __future__ import annotations

__all__ = ["run_t212_backtest", "daily_data_gaps", "liquidation_valuer"]

from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pandas as pd

from backtest.engine.engine import BacktestEngine, RunResult, StrategyFn
from backtest.engine.feed import BarFeed, FxSeries, trading_key
from backtest.engine.report import write_chart
from backtest.engine.results import write_run
from backtest.engine.metrics import compute_metrics
from backtest.engine.types import Bar, EngineConfig
from backtest.t212.broker_sim import T212BrokerSim
from backtest.t212.costs import CostConfig, apply_spread, fill_cash_and_costs, \
    price_to_gbp
from backtest.t212.data_source import load_bars, load_fx
from backtest.t212.faults import BAR_SECONDS, FaultConfig
from backtest.t212.instruments import (T212_ANNUALIZATION_DAYS, exchange_tz,
                                       security_kind)


def daily_data_gaps(frames: dict[str, pd.DataFrame]) -> dict[str, int]:
    """Count missing exchange-local trading days per symbol (daily data).

    A day counts as missing for a symbol when at least one OTHER symbol on
    the same exchange time zone has a bar, the symbol is inside its own
    first-to-last span, and it has none. Cross-exchange holiday asymmetry is
    thereby excluded; genuine suspensions and data holes are counted, not
    silently skipped (backtest-discipline section 2.8).
    """
    days: dict[str, set] = {}
    for symbol, frame in frames.items():
        tz = exchange_tz(symbol)
        days[symbol] = {trading_key(pd.Timestamp(t), tz, True)
                        for t in frame["ts"]}
    gaps: dict[str, int] = {}
    for symbol, own in days.items():
        tz = exchange_tz(symbol)
        peers = set().union(*(d for s, d in days.items()
                              if s != symbol and exchange_tz(s) == tz)) \
            if len(days) > 1 else set()
        if not own or not peers:
            gaps[symbol] = 0
            continue
        span = {d for d in peers if min(own) <= d <= max(own)}
        gaps[symbol] = len(span - own)
    return gaps


def liquidation_valuer(broker: T212BrokerSim):
    """Per-share exit value closure injected into the engine.

    Values one share at what a market SELL would fetch right now: bid side
    of the spread (session-widened where applicable), worst-tier slippage,
    the FX fee on the conversion, and the per-share US sell fees -- computed
    by pricing a one-share sell through the real cost stack, so the exit
    mark can never drift from the fill mechanics. The flat PTM levy has no
    per-share form and is disclosed as unmodeled in the liquidation column.

    The mid mark stays the diagnostic column; the authoritative tier reads
    equity_liq_gbp for drawdown and final equity (backtest-discipline
    section 2.10: unrealizable marks must not carry the headline).
    """
    def to_liquidation(symbol: str, bar: Bar, key: pd.Timestamp) -> Decimal:
        exec_price = apply_spread(Decimal(str(bar.close)), False,
                                  broker.half_spread(symbol, key),
                                  broker.cost_cfg.slippage_bps)
        fx_mid = None
        if bar.quote_ccy == "USD":
            fx_mid = broker.fx.rate_at(broker.fx_query_ts(key))
        cash_delta, _, _ = fill_cash_and_costs(
            Decimal("-1"), exec_price, bar.quote_ccy, fx_mid,
            security_kind(symbol), symbol.endswith(".L"), broker.cost_cfg)
        return cash_delta
    return to_liquidation


def run_t212_backtest(config: EngineConfig, strategy: StrategyFn,
                      cost_cfg: CostConfig | None = None,
                      fault_cfg: FaultConfig | None = None,
                      data_root: Path | str | None = None,
                      out_dir: Path | str | None = None,
                      write: bool = True):
    """Run one T212 backtest end to end.

    Returns:
        (result, metrics, paths): RunResult, metrics dict, and the written
        file paths (empty dict when write=False).
    """
    if config.fee_tier not in ("worst", "actual"):
        raise ValueError(f"unknown fee tier {config.fee_tier!r}")
    if config.fill_timing not in ("next_open", "same_close"):
        raise ValueError(f"unknown fill timing {config.fill_timing!r}")
    if cost_cfg is None:
        cost_cfg = CostConfig() if config.fee_tier == "worst" \
            else CostConfig.actual_tier()
    if fault_cfg is None:
        fault_cfg = FaultConfig(seed=config.seed) if config.fee_tier == "worst" \
            else FaultConfig.all_off(seed=config.seed)

    daily = config.interval == "1d"
    frames = load_bars(config.symbols, config.interval, config.start,
                       config.end, data_root)
    try:
        fx_frame = load_fx(config.interval, config.start, config.end, data_root)
        fx_duration = BAR_SECONDS[config.interval]
    except FileNotFoundError:
        fx_frame = load_fx("1d", config.start, config.end, data_root)
        fx_duration = BAR_SECONDS["1d"]

    feed = BarFeed(frames, exchange_tz, daily)
    fx = FxSeries(fx_frame, fx_duration)
    broker = T212BrokerSim(cost_cfg, fault_cfg, config.interval, fx, daily,
                           fill_timing=config.fill_timing)
    engine = BacktestEngine(config, feed, fx, broker, strategy, price_to_gbp,
                            to_liquidation=liquidation_valuer(broker))
    result: RunResult = engine.run()

    metrics = compute_metrics(result.equity, result.trades,
                              float(config.initial_cash_gbp),
                              T212_ANNUALIZATION_DAYS)
    armed_empty = [fid for fid, keys in (
        ("F3_outage_window", fault_cfg.outage_windows),
        ("F4_reduce_only_window", fault_cfg.reduce_only_windows),
        ("F11_stale_ticker", fault_cfg.stale_tickers),
        ("F14_auth_outage_window", fault_cfg.auth_outage_windows),
    ) if fault_cfg.on(fid) and not keys]
    extra = {
        "cost_config": asdict(cost_cfg),
        "fault_config": asdict(fault_cfg),
        # Honesty stamp: these switches are ON but have nothing configured,
        # so no outage/restriction stress actually fired in this run. The
        # report layer must not claim it did.
        "fault_switches_armed_but_empty": armed_empty,
        "data_gaps_daily": daily_data_gaps(frames) if daily else "not_computed",
        "fx_bar_duration_sec": fx_duration,
    }
    paths: dict = {}
    if write:
        paths = write_run(result, config, metrics, extra_meta=extra,
                          out_dir=out_dir)
        chart_path = Path(str(paths["meta"]).replace(".meta.json",
                                                     ".chart.html"))
        paths["chart"] = write_chart(
            result, f"{chart_path.stem.replace('.chart', '')}", chart_path)
    return result, metrics, paths
