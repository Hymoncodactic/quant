"""A0 across every intraday interval the local data supports: 1m, 5m, 1h.

Generalizes scripts/20260822_a0_minute_backtest.py to all three stored
intraday grids. For each interval two arms run over the SAME exchange-local
window and the SAME decision days, so the only difference between them is
bar granularity and the execution path:

    <iv>   the intraday arm, strategy a0_intraday v0.0.1. Decision on the
           session's LAST bar using information through the bar before it;
           the market order fills at the next session's opening bar.
    d1     the control, interval 1d, unmodified a0 v0.0.1. Decision on day
           t's close, fill at day t+1's open.

Information lag before the 16:00 close, by interval: about 1 minute at 1m,
5 minutes at 5m, 30 minutes at 1h (the 1h decision bar starts 15:30, so the
last COMPLETED bar closes at 15:30).

Windows are the intersection of every symbol's coverage with GBPUSD=X's, in
exchange-local days; FX starts later than the equities on both 5m and 1h, and
FxSeries raises rather than extrapolate, so the window must respect it.

A session that closes early (a US half day) has no bar at the interval's
decision time, so the intraday arm takes no decision there. The control arm
is held to the intraday arm's own decision days, otherwise the two would
trade on different sets of sessions and the comparison would confound an
execution difference with a calendar difference.

Usage:
    python scripts/20260822_a0_intraday_backtest.py [--intervals 1m,5m,1h]
                                                    [--tiers actual,worst]
                                                    [--quick]
"""

from __future__ import annotations

import argparse
import copy
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

RESULTS_DIR = ROOT / "backtest" / "results"
STAMP = "20260822"
NY = "America/New_York"
HISTORY_START = "2010-01-04"

# Coverage measured 2026-08-22 by intersecting every A0 symbol, QQQ and
# GBPUSD=X in exchange-local days. `end` deliberately runs one session past
# the last decision so the final order actually fills; 2026-08-21 is a
# partial session (ends 12:45 NY) and therefore yields no decision bar, and
# GBPUSD=X stops on 2026-08-20 so that last fill reuses the 08-20 rate.
INTERVALS: dict[str, dict] = {
    "1m": {"start": "2026-07-24", "end": "2026-08-21",
           "decision": "15:59", "bars_per_session": 390, "lag": "1 分钟"},
    "5m": {"start": "2026-06-02", "end": "2026-08-21",
           "decision": "15:55", "bars_per_session": 78, "lag": "5 分钟"},
    "1h": {"start": "2023-11-07", "end": "2026-08-21",
           "decision": "15:30", "bars_per_session": 7, "lag": "30 分钟"},
}
# Why each start is what it is. The FX symbol rides in the FEED as well as in
# FxSeries, and FxSeries publishes a bar's close only at ts + bar_duration,
# raising rather than extrapolating before that. If the window's first
# timeline key IS the first FX bar, the very first valuation asks for a rate
# that does not exist yet and the run dies. load_fx() reaches 14 days further
# back than load_bars(), so the fix is to start the window on the first US
# session STRICTLY AFTER the FX series' first Europe/London local day: the
# feed then drops that first FX bar while FxSeries keeps it.
#   1m  FX first London day 2026-07-23 -> 2026-07-24  (21 sessions)
#   5m  FX first London day 2026-06-01 -> 2026-06-02  (57 sessions)
#   1h  FX first London day 2023-11-06 -> 2023-11-07  (699 sessions)


def daily_history(symbols: list[str], start: str, end: str) -> dict[str, list]:
    """symbol -> ascending rows of (iso_local_date, open, high, low, close)."""
    frames = load_bars(symbols, "1d", start, end)
    out: dict[str, list] = {}
    for symbol, frame in frames.items():
        local = frame["ts"].dt.tz_convert(NY).dt.date
        out[symbol] = [
            (d.isoformat(), float(o), float(h), float(l), float(c))
            for d, o, h, l, c in zip(local, frame["open"], frame["high"],
                                     frame["low"], frame["close"])]
    return out


def session_dates(equity: pd.DataFrame) -> pd.Series:
    """Exchange-local trading date of every equity record.

    An intraday run stamps real UTC instants; a daily run stamps the feed's
    alignment key, which is a TZ-NAIVE exchange-local midnight. Treating that
    naive key as UTC and converting to New York moves the whole curve back a
    calendar day (symptom: Sunday-labelled rows).
    """
    ts = equity["ts"]
    if isinstance(ts.dtype, pd.DatetimeTZDtype):
        return ts.dt.tz_convert(NY).dt.date
    naive = pd.to_datetime(ts)
    if naive.dt.tz is not None:
        return naive.dt.tz_convert(NY).dt.date
    return naive.dt.date


_US_DAYS: set | None = None


def us_trading_days() -> set:
    """The US equity trading calendar, from AAPL's own daily partitions.

    Neither arm's equity index can supply this: both feeds carry GBPUSD=X,
    whose London calendar adds dates on which no US session took place (26 of
    them over the 1h window). Counting those as observations inflates the
    session count that CAGR divides by and dilutes the daily return series
    Sharpe is computed from.
    """
    global _US_DAYS
    if _US_DAYS is None:
        frame = load_bars(["AAPL"], "1d", "2000-01-01", "2099-01-01")["AAPL"]
        _US_DAYS = set(frame["ts"].dt.tz_convert(NY).dt.date)
    return _US_DAYS


def regular_hours(equity: pd.DataFrame) -> pd.DataFrame:
    """Records inside the 09:30-16:00 US session, on US trading days only.

    Outside those bounds the equity book is not repriced -- only the FX leg
    moves -- so an "intrabar" drawdown measured over ALL records is mostly
    overnight and weekend FX revaluation, not intrabar price action. Measured
    2026-08-22: at 1m the excess over daily sampling is +0.2557pp across all
    records but only +0.0079pp inside regular hours.
    """
    frame = equity.copy()
    ts = frame["ts"]
    if not isinstance(ts.dtype, pd.DatetimeTZDtype):
        return frame
    local = ts.dt.tz_convert(NY)
    return frame.loc[local.dt.date.isin(us_trading_days())
                     & (local.dt.time >= pd.Timestamp("09:30").time())
                     & (local.dt.time <= pd.Timestamp("16:00").time())]


def daily_curve(equity: pd.DataFrame, column: str) -> pd.Series:
    """Last record per US trading session."""
    frame = equity.copy()
    frame["date"] = session_dates(frame)
    frame = frame.loc[frame["date"].isin(us_trading_days())]
    return frame.groupby("date")[column].last()


def mdd(values) -> float:
    """Peak-to-trough depth of a curve, as a fraction of its running peak."""
    arr = pd.Series(list(values), dtype=float)
    return float(-(arr / arr.cummax() - 1.0).min())


def curve_stats(curve: pd.Series, initial: float, ann_days: int = 252) -> dict:
    """Return, drawdown and ratio statistics at the curve's own sampling."""
    if curve.empty:
        return {}
    rets = curve.pct_change().dropna()
    out = {
        "final_gbp": float(curve.iloc[-1]),
        "total_return": float(curve.iloc[-1]) / initial - 1.0,
        "max_drawdown": mdd(curve),
        "sessions": int(len(curve)),
    }
    if len(rets) > 2 and rets.std(ddof=1) > 0:
        out["sharpe_rf0"] = float(
            rets.mean() / rets.std(ddof=1) * (ann_days ** 0.5))
        out["ann_vol"] = float(rets.std(ddof=1) * (ann_days ** 0.5))
    if len(curve) > 1:
        years = len(curve) / ann_days
        if years > 0 and curve.iloc[-1] > 0:
            out["cagr"] = float((curve.iloc[-1] / initial) ** (1 / years) - 1)
    return out


def recording(strategy):
    """Wrap a strategy so the sessions it actually decided on are captured."""
    seen: set = set()

    def wrapped(view, portfolio, params):
        targets = strategy(view, portfolio, params)
        if targets:
            now = pd.Timestamp(view.now)
            seen.add(now.tz_convert(NY).date() if now.tzinfo else now.date())
        return targets

    return wrapped, seen


def restricted(strategy, allowed: set):
    """Hold the control arm to the intraday arm's own decision days."""
    def wrapped(view, portfolio, params):
        now = pd.Timestamp(view.now)
        day = now.tz_convert(NY).date() if now.tzinfo else now.date()
        return strategy(view, portfolio, params) if day in allowed else {}
    return wrapped


FILL_TIMINGS = ("same_close", "next_open")


def run_interval(iv: str, spec: dict, base: dict, tiers: tuple) -> list[dict]:
    """Every arm of one interval, over that interval's own window.

    Two fill timings are run per interval (EngineConfig.fill_timing, ruled
    2026-08-22 in research/decisions/20260822_close_execution_timing.md):

        same_close  the order is submitted about a minute before the close
                    and executes at the decision bar's CLOSE, paying
                    close_gap_bps on top of spread and slippage. This is the
                    live intent: the last minutes of the session are usable,
                    and nothing ever executes at the opening print.
        next_open   the previous caliber, kept only as the comparison that
                    isolates what the close fill is worth.

    The daily control runs under same_close as well, so the intraday and
    daily arms are compared on the same execution philosophy.
    """
    symbols = list(base["trade_symbols"])
    feed_symbols = symbols + [base["state_symbol"], base["fx_symbol"]]
    start, end = spec["start"], spec["end"]

    params = copy.deepcopy(base)
    params["live_from"] = start
    params["decision_time_local"] = spec["decision"]
    params["exchange_tz"] = NY
    params["bars_per_session"] = spec["bars_per_session"]
    params["daily_history_source"] = (
        f"t212 curated 1d, {HISTORY_START}..{end}, causally sliced to dates "
        f"strictly before the decision date")

    history = daily_history(symbols + [base["state_symbol"]],
                            HISTORY_START, end)
    rows: list[dict] = []
    for tier in tiers:
      for timing in FILL_TIMINGS:
        intraday, decided = recording(a0m.make_strategy(history))
        cfg = EngineConfig(
            symbols=feed_symbols, interval=iv, start=start, end=end,
            initial_cash_gbp=Decimal("10000"),
            arm=f"{iv}_{timing}_{tier}",
            fee_tier=tier, fill_timing=timing, strategy_name="a0_intraday",
            strategy_version="0.0.1", params=params)
        res_i, met_i, _ = run_t212_backtest(cfg, intraday, write=True)
        print(f"  {iv}/{timing}/{tier}: fills={met_i.get('fills')}, "
              f"decisions={len(decided)}", flush=True)

        cfg_d = EngineConfig(
            symbols=feed_symbols, interval="1d", start=HISTORY_START,
            end=end, initial_cash_gbp=Decimal("10000"),
            arm=f"d1for{iv}_{timing}_{tier}", fee_tier=tier,
            fill_timing=timing, strategy_name="a0",
            strategy_version="0.0.1", params=params)
        res_d, met_d, _ = run_t212_backtest(
            cfg_d, restricted(load_strategy("t212", "a0", "0.0.1"), decided),
            write=True)
        print(f"  d1/{timing}/{tier}: fills={met_d.get('fills')}", flush=True)

        for arm, res, met in ((iv, res_i, met_i), ("d1", res_d, met_d)):
            eq = res.equity
            eq = eq.loc[session_dates(eq) >= pd.Timestamp(start).date()].copy()
            liq = daily_curve(eq, "equity_liq_gbp")
            row = {"interval": iv, "arm": arm, "fee_tier": tier,
                   "fill_timing": timing,
                   "decision_sessions": len(decided),
                   "info_lag": spec["lag"], "fills": met.get("fills"),
                   "orders_total": res.meta.get("orders_total"),
                   "orders_rejected": res.meta.get("orders_rejected"),
                   "capital_peak_gbp": met.get("capital_peak_occupied_gbp"),
                   **{f"liq_{k}": v
                      for k, v in curve_stats(liq, 10000.0).items()}}
            if arm == iv:
                rth = regular_hours(eq)
                row["intrabar_max_drawdown"] = mdd(rth["equity_liq_gbp"])
                row["all_record_max_drawdown"] = mdd(eq["equity_liq_gbp"])
                row["records_rth"] = int(len(rth))
                row["records_all"] = int(len(eq))
            trades = res.trades
            if not trades.empty:
                trades.to_parquet(
                    RESULTS_DIR / f"a0_intraday_{STAMP}_{iv}_{arm}"
                                  f"_{timing}_{tier}.trades.parquet")
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--intervals", default="1m,5m,1h")
    parser.add_argument("--tiers", default="actual,worst")
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    base = yaml.safe_load(
        (ROOT / "trading212" / "config" / "strategies" / "a0_v0_0_1.yaml")
        .read_text())
    tiers = tuple(t.strip() for t in args.tiers.split(","))

    rows: list[dict] = []
    for iv in (s.strip() for s in args.intervals.split(",")):
        spec = dict(INTERVALS[iv])
        if args.quick:
            spec["start"] = str(max(pd.Timestamp(spec["start"]),
                                    pd.Timestamp(spec["end"])
                                    - pd.Timedelta(days=12)).date())
        print(f"{iv}: {spec['start']} .. {spec['end']}, "
              f"决策时刻 {spec['decision']}, 信息滞后 {spec['lag']}", flush=True)
        rows += run_interval(iv, spec, base, tiers)

    table = pd.DataFrame(rows)
    if not args.quick:
        out = RESULTS_DIR / f"a0_intraday_comparison_{STAMP}.csv"
        table.to_csv(out, index=False)
        print(f"\nwritten {out}")
    cols = ["interval", "arm", "fill_timing", "fee_tier", "decision_sessions",
            "fills", "liq_final_gbp", "liq_total_return", "liq_cagr",
            "liq_max_drawdown", "intrabar_max_drawdown", "liq_sharpe_rf0"]
    print(table[[c for c in cols if c in table.columns]].to_string(
        index=False, float_format=lambda v: f"{v:.4f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
