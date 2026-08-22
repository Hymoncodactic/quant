"""Aggregate the A0 hourly-frequency run into one JSON payload for the report.

Responsibility: read the authoritative result files of the 1h intraday run,
reduce its per-bar equity record to one observation per US trading session,
derive position value, exposure, drawdown and the ratio statistics from that
series, group fills into chart markers, cut the held dates into contiguous
intervals, verify the headline numbers against the recorded comparison table,
hash the evidence files and write one JSON file. Anchored assertion follows
/html-report section 2 item 1: computed CAGR, maximum drawdown and final
equity are compared against the row of a0_intraday_comparison_20260822.csv
that the ruling cites, and any deviation beyond ANCHOR_TOLERANCE raises
SystemExit rather than shipping a report that disagrees with the ruling.

Differences from make_a0_report_data.py, which describes the daily GBP-1000
run (that file is the blood line; the two share _verify and _holding_intervals
rather than duplicating them):

    C1  The session calendar is the US equity calendar taken from AAPL's own
        daily partitions, not every calendar date present in the record. The
        feed carries GBPUSD=X, whose London calendar contributes dates on
        which no US session took place; counting those dilutes the daily
        return series and inflates the denominator CAGR divides by.
    C2  Dates are exchange-local New York, not raw UTC dates.
    C3  The authoritative equity column is equity_liq_gbp, the liquidation
        mark, matching the ruling; equity_gbp stays a diagnostic.

Out of scope: producing experiment results, which belong to
scripts/20260822_a0_intraday_backtest.py; rendering, which belongs to
a0_report_template.html; assembling the HTML, which belongs to
build_a0_1h_report.py beside this file.

Public functions:
    build()   Assemble and return the complete report payload as a dict.
    main()    Write the payload to OUT and print a one-line summary.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from research.regime_lab.report.make_a0_report_data import (  # noqa: E402
    _holding_intervals, _verify)

RESULTS = ROOT / "backtest" / "results"
OUT = HERE / "a0_1h_report_data.json"

# Authoritative arm: same_close execution (submitted about a minute before
# the close, filled at the decision bar's close). Ruled 2026-08-22 in
# research/decisions/20260822_close_execution_timing.md; it is the live
# intent and it never executes at the opening print.
RUN_STEM = ("a0_intraday_v0_0_1_1h_same_close_actual_2023-11-07_2026-08-21"
            "_fee-actual_fill-same_close_seed20260820")
CONTROL_STEM = ("a0_v0_0_1_d1for1h_same_close_actual_2010-01-04_2026-08-21"
                "_fee-actual_fill-same_close_seed20260820")
# Kept for the comparison badge: the previous next_open caliber.
NEXT_OPEN_STEM = ("a0_intraday_v0_0_1_1h_next_open_actual_2023-11-07"
                  "_2026-08-21_fee-actual_fill-next_open_seed20260820")
COMPARISON_CSV = RESULTS / "a0_intraday_comparison_20260822.csv"
LIVE_FROM = "2023-11-07"
ANCHOR_TOLERANCE = 5e-4
FLAT_EPS = 1e-4
NY = "America/New_York"
EQUITY_COLUMN = "equity_liq_gbp"


def us_sessions() -> set:
    """US equity trading dates, from AAPL's own daily partitions (C1)."""
    parts = sorted((ROOT / "data" / "t212" / "curated" / "us_equity"
                    / "AAPL" / "1d").glob("*.parquet"))
    frame = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    return set(pd.to_datetime(frame["ts"], utc=True)
               .dt.tz_convert(NY).dt.date)


def session_series(stem: str) -> pd.DataFrame:
    """One equity observation per US trading session, window-clipped."""
    equity = pd.read_parquet(RESULTS / f"{stem}.equity.parquet")
    ts = equity["ts"]
    if isinstance(ts.dtype, pd.DatetimeTZDtype):
        local = ts.dt.tz_convert(NY).dt.date            # C2
    else:
        local = pd.to_datetime(ts).dt.date
    equity = equity.assign(date=local)
    sessions = us_sessions()
    equity = equity[equity["date"].isin(sessions)
                    & (equity["date"] >= pd.Timestamp(LIVE_FROM).date())]
    daily = equity.groupby("date").last().reset_index()
    daily["date"] = pd.to_datetime(daily["date"])
    return daily


def build() -> dict:
    """Assemble the full report payload."""
    daily = session_series(RUN_STEM)
    trades = pd.read_parquet(RESULTS / f"{RUN_STEM}.trades.parquet")
    meta = json.loads((RESULTS / f"{RUN_STEM}.meta.json").read_text())
    metrics = meta["metrics"]

    eq = daily[EQUITY_COLUMN].to_numpy()                # C3
    cash = daily["cash_gbp"].to_numpy()
    position_value = np.maximum(daily["equity_gbp"].to_numpy() - cash, 0.0)
    exposure = np.where(eq > 0, position_value / eq, 0.0)
    drawdown = eq / np.maximum.accumulate(eq) - 1.0

    rets = pd.Series(eq).pct_change().dropna()
    n_sessions = len(daily)
    cagr = (eq[-1] / 10000.0) ** (252 / n_sessions) - 1
    max_dd = float(-drawdown.min())
    sharpe = float(rets.mean() / rets.std(ddof=1) * np.sqrt(252))

    control = session_series(CONTROL_STEM)
    ctrl_eq = control[EQUITY_COLUMN].to_numpy()
    nextopen = session_series(NEXT_OPEN_STEM)
    nextopen_eq = nextopen[EQUITY_COLUMN].to_numpy()

    trades["ts"] = pd.to_datetime(trades["ts"], utc=True)
    trades["date"] = trades["ts"].dt.tz_convert(NY).dt.date
    trades["side"] = np.where(trades["quantity"] > 0, "buy", "sell")
    equity_by_date = dict(zip(daily["date"].dt.date, eq))

    markers = []
    for (date, side), group in trades.groupby(["date", "side"]):
        if date not in equity_by_date:
            continue
        markers.append({
            "date": str(date), "side": side,
            "y": round(float(equity_by_date[date]), 2),
            "n": int(len(group)),
            "value_gbp": round(float(group["cash_delta_gbp"].abs().sum()), 2),
            "symbols": ", ".join(sorted(group["symbol"].unique())[:6])
                       + ("..." if group["symbol"].nunique() > 6 else ""),
        })

    table = pd.read_csv(COMPARISON_CSV)
    anchor = table[(table["interval"] == "1h") & (table["arm"] == "1h")
                   & (table["fee_tier"] == "actual")
                   & (table["fill_timing"] == "same_close")].iloc[0]
    checks = {
        "cagr": (cagr, float(anchor["liq_cagr"])),
        "max_dd": (max_dd, float(anchor["liq_max_drawdown"])),
        "final_equity": (float(eq[-1]), float(anchor["liq_final_gbp"])),
        "sharpe": (sharpe, float(anchor["liq_sharpe_rf0"])),
    }
    failures = [f"{k}: computed {a:.6f} vs recorded {b:.6f}"
                for k, (a, b) in checks.items()
                if abs(a - b) > max(ANCHOR_TOLERANCE, abs(b) * ANCHOR_TOLERANCE)]
    if failures:
        raise SystemExit("ANCHOR MISMATCH\n" + "\n".join(failures))

    trough = int(np.argmin(drawdown))
    peak = int(np.argmax(eq[:trough + 1])) if trough > 0 else 0
    years = n_sessions / 252.0
    costs = sum(metrics["costs_gbp_total"].values())

    return {
        "run": {
            "stem": RUN_STEM,
            "initial_cash_gbp": float(meta["config"]["initial_cash_gbp"]),
            "fee_tier": meta["config"]["fee_tier"],
            "strategy": f'{meta["config"]["strategy_name"]} '
                        f'v{meta["config"]["strategy_version"]}',
            "engine_window": f'{meta["config"]["start"]} .. '
                             f'{meta["config"]["end"]}',
            "live_from": LIVE_FROM,
            "symbol_count": len(meta["config"]["params"]["trade_symbols"]),
            "interval": meta["config"]["interval"],
            "decision_time": meta["config"]["params"]["decision_time_local"],
            "fill_timing": meta["config"]["fill_timing"],
        },
        "kpi": {
            "cagr": round(cagr, 6), "max_dd": round(max_dd, 6),
            "sharpe": round(sharpe, 4),
            "win_rate": round(float(metrics["win_rate"]), 6),
            "profit_factor": round(float(metrics["profit_factor"]), 4),
            "final_equity": round(float(eq[-1]), 2),
            "initial_equity": 10000.0,
            "ann_vol": round(float(rets.std(ddof=1) * np.sqrt(252)), 6),
            "fills": int(len(trades)),
            "trading_days": n_sessions,
            "days_in_market": int((exposure > FLAT_EPS).sum()),
            "mean_exposure": round(float(exposure.mean()), 6),
            "cost_drag_pct": round(costs / float(eq[-1]) * 100, 4),
            "costs_gbp": round(costs, 2),
            "control_final_equity": round(float(ctrl_eq[-1]), 2),
            "next_open_final_equity": round(float(nextopen_eq[-1]), 2),
            "fill_timing": meta["config"]["fill_timing"],
            "orders_total": int(meta["run"]["orders_total"]),
            "orders_rejected": int(meta["run"]["orders_rejected"]),
            "capital_peak_gbp": round(
                float(metrics["capital_peak_occupied_gbp"]), 2),
            "total_return_on_capital": round(
                float(metrics["total_return_liquidation_rate_on_capital"]), 6),
            "turnover_annualized": round(
                float(metrics["turnover_both_legs_on_capital"]) / years, 4),
            "avg_holding_days": round(float(metrics["avg_holding_days"]), 2),
            "median_holding_days": round(float(metrics["median_holding_days"]), 2),
        },
        "series": {
            "dates": [str(d.date()) for d in daily["date"]],
            "equity": [round(float(v), 2) for v in eq],
            "exposure_pct": [round(float(v) * 100, 3) for v in exposure],
            "drawdown_pct": [round(float(v) * 100, 3) for v in drawdown],
        },
        "holding_spans": _holding_intervals(daily["date"], position_value),
        "markers": markers,
        "worst_drawdown": {
            "peak_date": str(daily["date"].iloc[peak].date()),
            "trough_date": str(daily["date"].iloc[trough].date()),
            "depth_pct": round(max_dd * 100, 2),
            "peak_equity": round(float(eq[peak]), 2),
            "trough_equity": round(float(eq[trough]), 2),
        },
        "metrics_full": {k: (v if not isinstance(v, dict) else
                             {ik: round(float(iv), 4) for ik, iv in v.items()})
                         for k, v in sorted(metrics.items())},
        "evidence": [
            {"id": "R-1", "kind": "结果", "supports": "小时频臂净值、现金、占用逐 bar 序列",
             **_verify(RESULTS / f"{RUN_STEM}.equity.parquet")},
            {"id": "R-2", "kind": "结果", "supports": "小时频臂逐笔成交，用于成交点标注",
             **_verify(RESULTS / f"{RUN_STEM}.trades.parquet")},
            {"id": "R-3", "kind": "结果", "supports": "运行配置、费率档与指标全集",
             **_verify(RESULTS / f"{RUN_STEM}.meta.json")},
            {"id": "R-4", "kind": "结果", "supports": "日频对照臂净值序列",
             **_verify(RESULTS / f"{CONTROL_STEM}.equity.parquet")},
            {"id": "R-5", "kind": "结果", "supports": "KPI 锚定断言的比对基准",
             **_verify(COMPARISON_CSV)},
            {"id": "S-1", "kind": "脚本", "supports": "本轮三档回测入口",
             **_verify(ROOT / "scripts" / "20260822_a0_intraday_backtest.py")},
            {"id": "S-2", "kind": "脚本", "supports": "真实数据验收 24 项",
             **_verify(ROOT / "scripts" / "20260822_a0_intraday_verify.py")},
            {"id": "C-1", "kind": "代码", "supports": "A0 信号唯一副本",
             **_verify(ROOT / "trading212" / "strategy" / "a0_v0_0_1.py")},
            {"id": "C-2", "kind": "代码", "supports": "小时频时序适配层",
             **_verify(ROOT / "trading212" / "strategy"
                       / "a0_intraday_v0_0_1.py")},
            {"id": "C-3", "kind": "配置", "supports": "A0 参数基线",
             **_verify(ROOT / "trading212" / "config" / "strategies"
                       / "a0_v0_0_1.yaml")},
            {"id": "V-1", "kind": "裁定", "supports": "本轮口径、结果与结论限定",
             **_verify(ROOT / "research" / "decisions"
                       / "20260822_a0_intraday_frequency_ruling.md")},
            {"id": "K-1", "kind": "纪律", "supports": "保守口径硬清单与权威口径规则",
             **_verify(ROOT / ".claude" / "skills" / "backtest-discipline"
                       / "SKILL.md")},
        ],
        "anchor_report": {k: {"computed": round(a, 6), "recorded": round(b, 6)}
                          for k, (a, b) in checks.items()},
    }


def main() -> int:
    payload = build()
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"anchors OK: {payload['anchor_report']}")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB), "
          f"{len(payload['series']['dates'])} sessions, "
          f"{len(payload['markers'])} marker groups, "
          f"{len(payload['holding_spans'])} holding spans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
