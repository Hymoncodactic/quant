"""A0 over the most recent two weeks, conservative caliber.

Responsibility: run the authoritative A0 arm over the last ten completed
trading sessions and report it under BOTH fee tiers, with the worst tier as
the headline because that is the conservative caliber the project requires
(/backtest-discipline section 8: authoritative = worst fee tier, every
conservative switch on).

Arms:
    1h/worst   interval 1h, fill_timing same_close, fee_tier worst. HEADLINE.
    1h/actual  same, measured fee tier. Comparison only.
    1d/worst   the daily arm over the same sessions, as a frequency control.

The engine window starts earlier than the reported window on purpose: the
intraday arm needs prior sessions for the dividend splice scale, and the daily
arm needs 2010-01-04 so the volatility gate's EXPANDING percentile has the
same denominator as every other A0 run (research/decisions/
20260822_a0_intraday_frequency_ruling.md section 6.3 measured that truncating
that history flips a gate). live_from confines capital to the reported window.

Out of scope: the strategy itself (trading212/strategy/), the engine
(backtest/), and the window-level statistics of the full study
(scripts/20260822_a0_intraday_backtest.py).

Public functions:
    main()   Run every arm, print the table, write the summary.

Constants:
    WINDOW_START / WINDOW_END  str  Reported window, the last ten completed
                                    sessions that carry a 15:30 decision bar.
    INTRADAY_ENGINE_START      str  Intraday feed start, earlier for warm-up.
    DAILY_ENGINE_START         str  Daily feed start, fixed at 2010-01-04.

Change log:
    2026-08-29  Created for the recent-window request.
"""

from __future__ import annotations

__all__ = ["main"]

import copy
import json
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.engine.strategy_loader import load_strategy          # noqa: E402
from backtest.engine.types import EngineConfig                     # noqa: E402
from backtest.t212.data_source import load_bars                    # noqa: E402
from backtest.t212.runner import run_t212_backtest                 # noqa: E402
from trading212.strategy import a0_intraday_v0_0_1 as a0m          # noqa: E402

RESULTS = ROOT / "backtest" / "results"
NY = "America/New_York"
WINDOW_START = "2026-08-14"
WINDOW_END = "2026-08-27"
INTRADAY_ENGINE_START = "2026-08-03"
DAILY_ENGINE_START = "2010-01-04"
HISTORY_START = "2010-01-04"


def daily_history(symbols: list[str], end: str) -> dict[str, list]:
    """symbol -> ascending (iso_date, o, h, l, c), from HISTORY_START."""
    frames = load_bars(symbols, "1d", HISTORY_START, end)
    out: dict[str, list] = {}
    for symbol, frame in frames.items():
        local = frame["ts"].dt.tz_convert(NY).dt.date
        out[symbol] = [(d.isoformat(), float(o), float(h), float(l), float(c))
                       for d, o, h, l, c in zip(local, frame["open"],
                                                frame["high"], frame["low"],
                                                frame["close"])]
    return out


def us_sessions() -> set:
    """US trading dates, from AAPL's own daily partitions.

    The feed also carries GBPUSD=X, whose calendar includes dates with no US
    session; counting those as sessions inflates the session count and, in any
    per-session statistic, dilutes the series.
    """
    frames = load_bars(["AAPL"], "1d", "2000-01-01", "2099-01-01")
    return set(frames["AAPL"]["ts"].dt.tz_convert(NY).dt.date)


def session_curve(equity: pd.DataFrame, column: str) -> pd.Series:
    """Last record per US trading session, clipped to the window."""
    ts = equity["ts"]
    if isinstance(ts.dtype, pd.DatetimeTZDtype):
        date = ts.dt.tz_convert(NY).dt.date
    else:
        date = pd.to_datetime(ts).dt.date
    frame = equity.assign(date=date)
    lo, hi = pd.Timestamp(WINDOW_START).date(), pd.Timestamp(WINDOW_END).date()
    frame = frame[(frame["date"] >= lo) & (frame["date"] <= hi)
                  & frame["date"].isin(us_sessions())]
    return frame.groupby("date")[column].last()


def main() -> int:
    global WINDOW_START, WINDOW_END, INTRADAY_ENGINE_START
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default=WINDOW_START,
                        help="first reported session, must carry a 15:30 bar")
    parser.add_argument("--end", default=WINDOW_END,
                        help="last reported session")
    parser.add_argument("--intraday-start", default=INTRADAY_ENGINE_START,
                        help="1h feed start; earlier than --start for warm-up")
    parser.add_argument("--tag", default="recent2w",
                        help="file-name tag for this window's results")
    args = parser.parse_args()
    WINDOW_START, WINDOW_END = args.start, args.end
    INTRADAY_ENGINE_START = args.intraday_start

    base = yaml.safe_load(
        (ROOT / "trading212" / "config" / "strategies" / "a0_v0_0_1.yaml")
        .read_text())
    symbols = list(base["trade_symbols"])
    feed = symbols + [base["state_symbol"], base["fx_symbol"]]

    params = copy.deepcopy(base)
    params["live_from"] = WINDOW_START
    params["decision_time_local"] = "15:30"
    params["exchange_tz"] = NY
    params["bars_per_session"] = 7
    history = daily_history(symbols + [base["state_symbol"]], WINDOW_END)

    arms = [("1h", "worst"), ("1h", "actual"), ("1d", "worst")]
    rows = []
    for interval, tier in arms:
        if interval == "1h":
            strategy = a0m.make_strategy(history)
            name, version = "a0_intraday", "0.0.1"
            start = INTRADAY_ENGINE_START
        else:
            strategy = load_strategy("t212", "a0", "0.0.1")
            name, version = "a0", "0.0.1"
            start = DAILY_ENGINE_START
        cfg = EngineConfig(
            symbols=feed, interval=interval, start=start, end=WINDOW_END,
            initial_cash_gbp=Decimal("10000"),
            arm=f"{args.tag}_{interval}_{tier}", fee_tier=tier,
            fill_timing="same_close", strategy_name=name,
            strategy_version=version, params=params)
        result, metrics, _ = run_t212_backtest(cfg, strategy, write=True)
        liq = session_curve(result.equity, "equity_liq_gbp")
        trades = result.trades
        if not trades.empty:
            trades.to_parquet(
                RESULTS / f"a0_{args.tag}_{interval}_{tier}.trades.parquet")
        row = {
            "arm": f"{interval}/{tier}",
            "sessions": int(len(liq)),
            "orders": result.meta.get("orders_total"),
            "rejected": result.meta.get("orders_rejected"),
            "fills": metrics.get("fills"),
            "start_gbp": float(liq.iloc[0]) if len(liq) else float("nan"),
            "final_gbp": float(liq.iloc[-1]) if len(liq) else float("nan"),
            "capital_peak_gbp": metrics.get("capital_peak_occupied_gbp"),
            "costs_gbp": sum(metrics.get("costs_gbp_total", {}).values()),
        }
        if len(liq) > 1:
            row["return_on_initial"] = float(liq.iloc[-1]) / 10000.0 - 1.0
            peak = liq.cummax()
            row["max_drawdown"] = float(-(liq / peak - 1.0).min())
        rows.append(row)
        print(f"done {interval}/{tier}: fills={row['fills']} "
              f"final=GBP{row['final_gbp']:,.2f}", flush=True)

    table = pd.DataFrame(rows)
    out = RESULTS / f"a0_{args.tag}_20260829.csv"
    table.to_csv(out, index=False)
    print("\n" + table.to_string(index=False,
                                 float_format=lambda v: f"{v:,.4f}"))
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
