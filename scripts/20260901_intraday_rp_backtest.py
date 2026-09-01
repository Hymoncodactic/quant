"""Intraday risk-parity enhancement layer: 10:30 in, 15:30 out, every session.

Responsibility: the study frozen in research/prereg/20260901_intraday_rp_prereg.md.
Buy a weighted basket at the 10:30 bar open (decided on the completed 09:30
bar), sell everything at the 15:30 bar open (decided on the 14:30 bar), so the
capital is free exactly when A0's 15:30 decision runs. Two pools, three weight
schemes, gross and net cost tiers, plus a deliberate-lookahead probe arm.

Costs (net tier): CostConfig.actual_tier() plus the per-symbol slippage table
derived from the 2026-08-31 demo measurement: extra one-way bps =
max(0, round_trip_median_bps / 2 - 1.0), the 1.0 being the half spread the
framework already charges. Unmeasured symbols (the ETF pool) use the pooled
median round trip of 3.435 bps from the same file. Gross tier zeroes the FX
fee, slippage and (via a disclosed monkeypatch) the half spread, to isolate
the intraday drift itself.

Weights are DAILY and causal: the weight used on session t is computed from
daily closes up to t-1 only, with an assertion in the builder. EW is 1/N,
IVOL is inverse 60-day volatility, ERC is equal risk contribution on a
120-day covariance solved by a deterministic fixed-point iteration.

Out of scope: the merge with A0 (later round), overnight-hold branches, and
any engine change (the per-symbol slippage extension lives in
backtest/t212/costs.py with its own tests).

Public functions:
    build_weights(closes, scheme, dates)   Causal weight schedule.
    make_strategy(...)                     The pure strategy closure.
    main()                                 Run all arms, write results.

Constants:
    WINDOW_START / WINDOW_END   str   2023-11-16 / 2026-08-28. The prereg
                                      said 2023-11-07, but the FX 1h series
                                      now begins 2023-11-14 (Yahoo's 730-day
                                      rolling cap advanced since the A0 runs),
                                      so the first safe US session moved;
                                      amendment recorded in the prereg.
    ETF10 / TECH18              list  The two frozen pools (prereg section 2).
    NO_1530_DAYS                set   Sessions without a 15:30 bar; no entry.
    POOLED_RT_BPS               float 3.435, the demo file's pooled median.

Change log:
    2026-09-01  Created for the intraday risk-parity study.
"""

from __future__ import annotations

__all__ = ["build_weights", "make_strategy", "main"]

import argparse
import glob
import sys
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.engine.types import EngineConfig                     # noqa: E402
from backtest.t212.costs import CostConfig                         # noqa: E402
from backtest.t212.data_source import load_bars                    # noqa: E402
from backtest.t212.faults import FaultConfig                       # noqa: E402
from backtest.t212.runner import run_t212_backtest                 # noqa: E402
from backtest.t212 import instruments                              # noqa: E402

RESULTS = ROOT / "backtest" / "results"
NY = "America/New_York"
D = Decimal

WINDOW_START = "2023-11-16"
WINDOW_END = "2026-08-28"
HISTORY_START = "2015-01-01"

ETF10 = ["SPY", "QQQ", "IWM", "GLD", "TLT", "IEF", "UUP", "XLP", "XLU", "XLV"]
TECH18 = ["AAPL", "AMAT", "AMD", "AMZN", "AVGO", "DELL", "GOOGL", "INTC",
          "LRCX", "META", "MRVL", "MSFT", "MU", "NVDA", "ORCL", "PLTR",
          "TSLA", "TSM"]
FX = "GBPUSD=X"

# Sessions with no 15:30 bar in the stored 1h data (scheduled early closes
# plus two observed gaps); the strategy takes no entry on them. Frozen in the
# prereg; live trading would read the exchange calendar instead.
NO_1530_DAYS = {"2023-11-24", "2024-07-03", "2024-11-29", "2024-12-24",
                "2025-07-03", "2025-11-28", "2025-12-24", "2026-01-30",
                "2026-08-31"}

SLIPPAGE_CSV = ROOT / "data" / "reference" / \
    "t212_demo_slippage_by_symbol_20260831.csv"
POOLED_RT_BPS = 3.435


def slippage_table(symbols: list[str]) -> dict[str, Decimal]:
    """Per-leg extra slippage per symbol, from the demo measurement.

    extra = max(0, round_trip_median / 2 - 1.0): the framework already
    charges a 1.0 bp half spread per leg, so only the measured excess over
    that rides in as slippage. Symbols absent from the file fall back to the
    pooled median round trip of the measured set.
    """
    table = pd.read_csv(SLIPPAGE_CSV, comment="#")
    measured = {row.symbol: max(0.0, row.round_trip_median_bps / 2.0 - 1.0)
                for row in table.itertuples(index=False)}
    fallback = max(0.0, POOLED_RT_BPS / 2.0 - 1.0)
    return {s: D(str(round(measured.get(s, fallback), 4))) for s in symbols}


def load_daily_closes(symbols: list[str]) -> pd.DataFrame:
    """Daily close panel indexed by exchange-local date."""
    frames = load_bars(symbols, "1d", HISTORY_START, WINDOW_END)
    out = {}
    for symbol, frame in frames.items():
        local = frame["ts"].dt.tz_convert(NY).dt.date
        out[symbol] = frame.assign(d=local).groupby("d")["close"].last()
    return pd.DataFrame(out).sort_index()


def _erc(cov: np.ndarray, iters: int = 400, tol: float = 1e-10) -> np.ndarray:
    """Equal-risk-contribution weights by deterministic fixed point."""
    n = cov.shape[0]
    w = np.full(n, 1.0 / n)
    for _ in range(iters):
        marginal = cov @ w
        contrib = w * marginal
        target = contrib.mean()
        new = w * np.sqrt(target / np.maximum(contrib, 1e-18))
        new = np.clip(new, 1e-8, None)
        new /= new.sum()
        if np.abs(new - w).max() < tol:
            w = new
            break
        w = new
    return w


def build_weights(closes: pd.DataFrame, scheme: str,
                  sessions: list) -> dict[str, dict[str, float]]:
    """Causal weight schedule: session t uses closes strictly before t."""
    rets = closes.pct_change()
    out: dict[str, dict[str, float]] = {}
    dates = list(closes.index)
    for day in sessions:
        past = rets[rets.index < day]          # cutoff: strictly before t
        assert past.index.max() is None or past.index.max() < day, \
            "lookahead in weight builder"
        if len(past) < 130:
            continue
        cols = [c for c in closes.columns if past[c].notna().tail(130).all()]
        if len(cols) < 3:
            continue
        if scheme == "EW":
            w = np.full(len(cols), 1.0 / len(cols))
        elif scheme == "IVOL":
            vol = past[cols].tail(60).std(ddof=1).to_numpy()
            inv = 1.0 / np.maximum(vol, 1e-8)
            w = inv / inv.sum()
        elif scheme == "ERC":
            cov = np.cov(past[cols].tail(120).to_numpy(), rowvar=False)
            w = _erc(cov)
        else:
            raise ValueError(scheme)
        out[day.isoformat()] = dict(zip(cols, w.tolist()))
    return out


def make_strategy(weights_by_date: dict, allowed_days: set | None = None):
    """Entry on the 09:30 bar, full exit on the 14:30 bar, else no orders.

    allowed_days, when given, restricts entry to those sessions; the
    deliberate-lookahead probe passes the future-selected winning days here
    and MUST outperform, proving the harness registers a leak.
    """
    def strategy(view, portfolio, params) -> dict[str, Decimal]:
        now = view.now
        if now.tzinfo is None:
            return {}
        local = now.tz_convert(NY)
        hhmm = local.strftime("%H:%M")
        day = local.date()
        if hhmm == "14:30":
            return {s: D("0") for s, q in portfolio.positions.items()
                    if q > 0}
        if hhmm != "09:30" or day.isoformat() in NO_1530_DAYS:
            return {}
        if allowed_days is not None and day.isoformat() not in allowed_days:
            return {}
        weights = weights_by_date.get(day.isoformat())
        if not weights:
            return {}
        fx_bar = view.bar(params["fx_symbol"])
        if fx_bar is None or fx_bar.close <= 0:
            return {}
        fx = D(str(fx_bar.close))
        equity = portfolio.cash_gbp
        for symbol, qty in portfolio.positions.items():
            bar = view.bar(symbol)
            if bar is not None and qty:
                equity += qty * D(str(bar.close)) / fx
        targets: dict[str, Decimal] = {}
        for symbol, weight in weights.items():
            bar = view.bar(symbol)
            if bar is None or bar.close <= 0:
                continue
            shares = (equity * D(str(weight)) * D("0.99") * fx
                      / D(str(bar.close))).quantize(D("0.0001"), ROUND_DOWN)
            if shares > 0:
                targets[symbol] = shares
        return targets
    return strategy


def cheat_days(pool: list[str]) -> set:
    """DELIBERATE LOOKAHEAD: sessions whose realized 10:30->15:30 EW return
    is positive, computed from the full 1h data. Probe arm only."""
    frames = load_bars(pool, "1h", WINDOW_START, WINDOW_END)
    rets = {}
    for symbol, frame in frames.items():
        local = frame["ts"].dt.tz_convert(NY)
        frame = frame.assign(d=local.dt.date, hm=local.dt.strftime("%H:%M"))
        opens = frame[frame.hm == "10:30"].set_index("d")["open"]
        exits = frame[frame.hm == "15:30"].set_index("d")["open"]
        rets[symbol] = (exits / opens - 1.0)
    ew = pd.DataFrame(rets).mean(axis=1)
    return {d.isoformat() for d, r in ew.items() if r > 0}


def run_arm(pool_name: str, pool: list[str], scheme: str, cost_mode: str,
            weights: dict, probe_days: set | None = None):
    feed = pool + [FX]
    tag = f"irp_{pool_name}_{scheme}_{cost_mode}" + \
        ("_probe" if probe_days is not None else "")
    if cost_mode == "net":
        cost = CostConfig(slippage_bps=D("0"),
                          spread_session_multiplier=D("1"), cooldown_bars=1,
                          slippage_bps_by_symbol=slippage_table(pool))
        patched = None
    else:
        cost = CostConfig(fx_fee_rate=D("0"), slippage_bps=D("0"),
                          spread_session_multiplier=D("1"), cooldown_bars=1)
        patched = instruments.DEFAULT_HALF_SPREAD_BPS_US
        instruments.DEFAULT_HALF_SPREAD_BPS_US = D("0")
    try:
        cfg = EngineConfig(symbols=feed, interval="1h", start=WINDOW_START,
                           end=WINDOW_END, initial_cash_gbp=D("10000"),
                           arm=tag, fee_tier="actual",
                           strategy_name="intraday_rp",
                           strategy_version="0.0.1",
                           params={"fx_symbol": FX})
        strategy = make_strategy(weights, probe_days)
        result, metrics, _ = run_t212_backtest(
            cfg, strategy, cost_cfg=cost,
            fault_cfg=FaultConfig.all_off(seed=cfg.seed), write=True)
    finally:
        if patched is not None:
            instruments.DEFAULT_HALF_SPREAD_BPS_US = patched
    return tag, result, metrics


def summarize(tag: str, result, metrics) -> dict:
    eq = result.equity
    local = eq["ts"].dt.tz_convert(NY).dt.date
    frame = eq.assign(d=local)
    us = set(load_bars(["SPY"], "1d", WINDOW_START, WINDOW_END)["SPY"]
             ["ts"].dt.tz_convert(NY).dt.date)
    curve = frame[frame["d"].isin(us)].groupby("d")["equity_liq_gbp"].last()
    rets = curve.pct_change().dropna()
    n = len(curve)
    out = {"arm": tag, "sessions": n, "fills": metrics.get("fills"),
           "final_gbp": float(curve.iloc[-1]),
           "ann_return": float(rets.mean() * 252),
           "ann_vol": float(rets.std(ddof=1) * np.sqrt(252)),
           "max_drawdown": float(-(curve / curve.cummax() - 1.0).min()),
           # compute_metrics collapses equity to one record per DATE and this
           # strategy is flat at every session's last record, so its online-day
           # count is zero and the cost aggregate never materializes there;
           # sum the itemized fill costs directly instead.
           "costs_gbp": float(sum(result.trades[c].sum()
                                  for c in result.trades.columns
                                  if c.startswith("cost_")))
           if not result.trades.empty else 0.0,
           "fx_fee_gbp": float(result.trades
                               .get("cost_currency_conversion_fee",
                                    pd.Series(dtype=float)).sum())
           if not result.trades.empty else 0.0}
    x = rets.to_numpy() - rets.mean()
    lrv = float((x * x).mean())
    for lag in range(1, 21):
        lrv += 2 * (1 - lag / 21) * float((x[lag:] * x[:-lag]).mean())
    se = np.sqrt(max(lrv, 1e-18) / n)
    out["nw_t"] = float(rets.mean() / se) if se > 0 else np.nan
    out["sharpe"] = out["ann_return"] / out["ann_vol"] \
        if out["ann_vol"] > 0 else np.nan
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    global WINDOW_START
    if args.quick:
        WINDOW_START = "2026-06-01"

    us_sessions = sorted(set(
        load_bars(["SPY"], "1d", WINDOW_START, WINDOW_END)["SPY"]
        ["ts"].dt.tz_convert(NY).dt.date))
    rows, curves = [], {}
    for pool_name, pool in (("etf10", ETF10), ("tech18", TECH18)):
        closes = load_daily_closes(pool)
        for scheme in ("EW", "IVOL", "ERC"):
            weights = build_weights(closes, scheme, us_sessions)
            for cost_mode in ("net", "gross"):
                tag, result, metrics = run_arm(pool_name, pool, scheme,
                                               cost_mode, weights)
                row = summarize(tag, result, metrics)
                rows.append(row)
                print(f"  {tag:28s} ann={row['ann_return']:+.2%} "
                      f"t={row['nw_t']:+.2f} dd={row['max_drawdown']:.2%} "
                      f"fills={row['fills']}", flush=True)
    # Deliberate-lookahead probe: EW on ETF10, net costs.
    closes = load_daily_closes(ETF10)
    weights = build_weights(closes, "EW", us_sessions)
    # The probe runs GROSS: at net costs even a perfect-foresight day filter
    # loses money (the quick run measured it at -20% annualized), so the
    # discriminating comparison is gross probe vs gross EW.
    tag, result, metrics = run_arm("etf10", ETF10, "EW", "gross", weights,
                                   probe_days=cheat_days(ETF10))
    row = summarize(tag, result, metrics)
    rows.append(row)
    print(f"  {tag:28s} ann={row['ann_return']:+.2%} "
          f"t={row['nw_t']:+.2f}  <- 前视探针，应大幅为正", flush=True)

    table = pd.DataFrame(rows)
    out = RESULTS / "intraday_rp_summary_20260901.csv"
    table.to_csv(out, index=False)
    print("\n" + table.to_string(index=False,
                                 float_format=lambda v: f"{v:,.4f}"))
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
