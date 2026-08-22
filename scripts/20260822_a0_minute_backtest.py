"""A0 on 1-minute bars: decide one minute before the close, fill at the next open.

Responsibility: entry layer for the minute-frequency comparison. Run two arms
over the same exchange-local window so that the only difference between them is
bar granularity and the execution path, then resample both equity curves to one
observation per trading session and write the comparison.

The m1 arm uses interval 1m and the a0_intraday strategy: each session's 15:59
bar carries the decision, taken on information through the 15:58 bar, and the
resulting market order fills at the next session's 09:30 opening bar. The d1 arm
is the control: interval 1d and the unmodified A0 strategy with live_from set to
the window start, deciding on day t's close and filling at day t+1's open, that
is the same signal on the same trade dates with daily execution.

The d1 feed must start long before the window because the view accumulates its
own history: TSMOM needs 252 bars and the volatility gate needs 756 volatility
observations. The m1 arm receives the same history injected as daily rows,
sliced to the same start date, so both arms compute the volatility percentile
over the same denominator.

Resampling to one observation per session is what makes the two comparable. A 1m
run marks equity every minute, so its raw drawdown is intraday sampled and
structurally deeper than a daily run's; the intraday-sampled figure is reported
separately and never against the daily one.

Out of scope: the signal itself, whose only copy is
trading212/strategy/a0_v0_0_1.py; the minute-to-daily adaptation, which belongs
to trading212/strategy/a0_intraday_v0_0_1.py; the engine and the cost model,
which live under backtest/; the acceptance assertions on the output, which live
in scripts/20260822_a0_minute_verify.py; the ruling drawn from these numbers,
which is research/decisions/20260822_a0_minute_frequency_ruling.md.

Public functions:
    daily_history(symbols, start, end)  Symbol mapped to ascending rows of
                                        (iso local date, open, high, low,
                                        close). Read once here and handed to the
                                        strategy as immutable data; the strategy
                                        slices it causally and never reads a
                                        file itself.
    session_dates(equity)               Exchange-local trading date of every
                                        equity record. The two intervals key
                                        their records differently: a 1m run
                                        stamps real UTC instants, while a 1d run
                                        stamps the feed's alignment key, which
                                        is a tz-naive exchange-local midnight.
                                        Treating that naive key as UTC would
                                        move every daily observation back one
                                        calendar day.
    daily_curve(equity, column)         Last record per exchange-local trading
                                        date.
    curve_stats(curve, initial)         Return and drawdown of one equity curve
                                        at its own sampling frequency.
    main()                              Run both arms under both fee tiers,
                                        write the outputs, and return the exit
                                        code.

Constants:
    ROOT           Path  Repository root, the parent of this file's directory.
    RESULTS_DIR    Path  backtest/results/, where the outputs are written.
    STAMP          str   Output file stamp, "20260822".
    WINDOW_START   str   First session of the comparison window, "2026-07-24".
                         Source: minute coverage measured 2026-08-22, when every
                         A0 symbol and the state symbol first carry 1m bars.
    WINDOW_END     str   Last session, "2026-08-21". That session ends 12:45 in
                         New York and therefore yields no 15:59 decision bar;
                         including it lets the final 2026-08-20 decision fill at
                         the 2026-08-21 open instead of dying unfilled.
    HISTORY_START  str   First daily-history date, "2010-01-04", shared by both
                         arms so that the expanding volatility percentile has
                         the same denominator in each.

Inputs:
    Command line: python scripts/20260822_a0_minute_backtest.py [--quick]
        --quick shortens the window to 2026-07-24 through 2026-07-30 and writes
        no runner files.
    trading212/config/strategies/a0_v0_0_1.yaml  Baseline strategy parameters,
        overridden here with live_from at the window start,
        decision_time_local "15:59" and exchange_tz "America/New_York".
    data/t212/curated/, through backtest.t212.data_source.
Outputs:
    backtest/results/a0_minute_20260822_<arm>_<tier>.trades.parquet
    backtest/results/a0_minute_comparison_20260822.csv
    backtest/results/a0_minute_curves_20260822.csv
    backtest/results/<run>.trades.parquet, <run>.equity.parquet and
        <run>.meta.json, written by the runner for every arm and tier.
    stdout carries the per-run progress lines and the final table.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.engine.types import EngineConfig                     # noqa: E402
from backtest.t212.data_source import load_bars                    # noqa: E402
from backtest.t212.runner import run_t212_backtest                 # noqa: E402
from backtest.engine.strategy_loader import load_strategy          # noqa: E402
from trading212.strategy import a0_intraday_v0_0_1 as a0m          # noqa: E402

RESULTS_DIR = ROOT / "backtest" / "results"
STAMP = "20260822"

# Minute coverage measured 2026-08-22: every A0 symbol and QQQ carry 1m bars
# from 2026-07-24; 2026-08-21 is a partial session (ends 12:45 NY) and so
# yields no 15:59 decision bar. Including it lets the final 2026-08-20
# decision actually fill at the 2026-08-21 open instead of dying unfilled.
WINDOW_START = "2026-07-24"
WINDOW_END = "2026-08-21"
# Daily history start, shared by both arms so the expanding vol percentile
# has the same denominator in each.
HISTORY_START = "2010-01-04"


def daily_history(symbols: list[str], start: str, end: str) -> dict[str, list]:
    """symbol -> ascending rows of (iso_local_date, open, high, low, close).

    Read once at the entry layer and handed to the strategy as immutable
    data; the strategy slices it causally and never reads a file itself.
    """
    frames = load_bars(symbols, "1d", start, end)
    out: dict[str, list] = {}
    for symbol, frame in frames.items():
        local = frame["ts"].dt.tz_convert("America/New_York").dt.date
        out[symbol] = [
            (d.isoformat(), float(o), float(h), float(l), float(c))
            for d, o, h, l, c in zip(local, frame["open"], frame["high"],
                                     frame["low"], frame["close"])]
    return out


def session_dates(equity: pd.DataFrame) -> pd.Series:
    """Exchange-local trading date of every equity record.

    The two intervals key their records differently and mixing them up
    silently shifts a whole curve by a day: a 1m run stamps real UTC
    instants, while a 1d run stamps the feed's alignment key, which is a
    TZ-NAIVE exchange-local midnight (engine/feed.py::trading_key). Treating
    that naive key as UTC and converting to New York moves every daily
    observation back one calendar day -- the symptom is Sunday-labelled rows.
    """
    ts = equity["ts"]
    if isinstance(ts.dtype, pd.DatetimeTZDtype):
        return ts.dt.tz_convert("America/New_York").dt.date
    naive = pd.to_datetime(ts)
    if naive.dt.tz is not None:
        return naive.dt.tz_convert("America/New_York").dt.date
    return naive.dt.date


def daily_curve(equity: pd.DataFrame, column: str) -> pd.Series:
    """Last record per exchange-local trading date.

    A 1m run marks equity every minute, so its raw drawdown is intraday
    sampled and structurally deeper than a daily run's. Resampling both to
    one observation per session is what makes the two comparable; the
    intraday-sampled figure is reported separately, never against the daily.
    """
    frame = equity.copy()
    frame["date"] = session_dates(frame)
    return frame.groupby("date")[column].last()


def curve_stats(curve: pd.Series, initial: float) -> dict:
    """Return and drawdown of one equity curve at its own sampling."""
    if curve.empty:
        return {}
    peak = curve.cummax()
    dd = (curve / peak - 1.0)
    rets = curve.pct_change().dropna()
    n = len(curve)
    total = float(curve.iloc[-1]) / initial - 1.0
    out = {
        "final_gbp": float(curve.iloc[-1]),
        "total_return": total,
        "max_drawdown": float(-dd.min()),
        "observations": n,
    }
    if len(rets) > 1 and rets.std(ddof=1) > 0:
        out["sharpe_rf0_daily_sampled"] = float(
            rets.mean() / rets.std(ddof=1) * (252 ** 0.5))
        out["ann_vol_daily_sampled"] = float(rets.std(ddof=1) * (252 ** 0.5))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true",
                        help="three-session smoke run, no files written")
    args = parser.parse_args()

    base = yaml.safe_load(
        (ROOT / "trading212" / "config" / "strategies" / "a0_v0_0_1.yaml")
        .read_text())
    symbols = list(base["trade_symbols"])
    feed_symbols = symbols + [base["state_symbol"], base["fx_symbol"]]

    win_start, win_end = WINDOW_START, WINDOW_END
    if args.quick:
        win_start, win_end = "2026-07-24", "2026-07-30"

    params = copy.deepcopy(base)
    params["live_from"] = win_start
    params["decision_time_local"] = "15:59"
    params["exchange_tz"] = "America/New_York"
    # Audit trail only; the arrays themselves ride in the closure so the run
    # metadata stays small.
    params["daily_history_source"] = (
        f"t212 curated 1d, {HISTORY_START}..{win_end}, causally sliced to "
        f"dates strictly before the decision date")

    history = daily_history(symbols + [base["state_symbol"]],
                            HISTORY_START, win_end)
    print(f"daily history: {len(history)} symbols, "
          f"{min(len(v) for v in history.values())}.."
          f"{max(len(v) for v in history.values())} rows each", flush=True)

    rows, curves = [], {}
    for tier in ("actual", "worst"):
        cfg_m = EngineConfig(
            symbols=feed_symbols, interval="1m", start=win_start,
            end=win_end, initial_cash_gbp=Decimal("10000"),
            arm=f"m1_{tier}", fee_tier=tier,
            strategy_name="a0_intraday", strategy_version="0.0.1",
            params=params)
        res_m, met_m, _ = run_t212_backtest(
            cfg_m, a0m.make_strategy(history), write=not args.quick)
        print(f"done m1/{tier}: fills={met_m.get('fills')}", flush=True)

        cfg_d = EngineConfig(
            symbols=feed_symbols, interval="1d", start=HISTORY_START,
            end=win_end, initial_cash_gbp=Decimal("10000"),
            arm=f"d1_{tier}", fee_tier=tier,
            strategy_name="a0", strategy_version="0.0.1", params=params)
        res_d, met_d, _ = run_t212_backtest(
            cfg_d, load_strategy("t212", "a0", "0.0.1"), write=not args.quick)
        print(f"done d1/{tier}: fills={met_d.get('fills')}", flush=True)

        for arm, res, met in (("m1", res_m, met_m), ("d1", res_d, met_d)):
            eq = res.equity
            if arm == "d1":
                eq = eq.loc[session_dates(eq)
                            >= pd.Timestamp(win_start).date()].copy()
            for col in ("equity_gbp", "equity_liq_gbp"):
                if col in eq.columns:
                    curves[(arm, tier, col)] = daily_curve(eq, col)
            liq = curves.get((arm, tier, "equity_liq_gbp"))
            mid = curves.get((arm, tier, "equity_gbp"))
            row = {"arm": arm, "fee_tier": tier, "fills": met.get("fills"),
                   "raw_max_drawdown_on_capital":
                       met.get("max_drawdown_on_capital"),
                   "raw_max_drawdown_liq_on_capital":
                       met.get("max_drawdown_liq_on_capital"),
                   "capital_peak_occupied_gbp":
                       met.get("capital_peak_occupied_gbp")}
            for label, curve in (("mid", mid), ("liq", liq)):
                if curve is not None:
                    for k, v in curve_stats(curve, 10000.0).items():
                        row[f"{label}_{k}"] = v
            rows.append(row)
            trades = res.trades
            if not trades.empty:
                key = f"{arm}_{tier}"
                trades.to_parquet(
                    RESULTS_DIR / f"a0_minute_{STAMP}_{key}.trades.parquet")

    table = pd.DataFrame(rows)
    if not args.quick:
        out = RESULTS_DIR / f"a0_minute_comparison_{STAMP}.csv"
        table.to_csv(out, index=False)
        curve_out = RESULTS_DIR / f"a0_minute_curves_{STAMP}.csv"
        pd.DataFrame({f"{a}_{t}_{c}": s for (a, t, c), s in curves.items()}
                     ).to_csv(curve_out)
        print(f"\nwritten {out}\nwritten {curve_out}")
    cols = ["arm", "fee_tier", "fills", "liq_final_gbp", "liq_total_return",
            "liq_max_drawdown", "raw_max_drawdown_liq_on_capital",
            "liq_sharpe_rf0_daily_sampled", "capital_peak_occupied_gbp"]
    print(table[[c for c in cols if c in table.columns]].to_string(
        index=False, float_format=lambda v: f"{v:.4f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
