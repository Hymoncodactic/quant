"""Aggregate one A0 GBP-1000 backtest run into a single JSON payload for the report.

Responsibility: read the authoritative result files of the run named by
RUN_STEM, reduce the per-fill equity curve to a daily series by taking the last
row of each calendar date and clipping it to the window starting at LIVE_FROM,
derive position value, exposure, drawdown, CAGR, annualized volatility and
Sharpe from that series, group fills by date and side into chart annotation
markers, cut the dates whose position value exceeds FLAT_EPS into contiguous
holding intervals, verify the headline numbers against the recorded scaling
table, hash the evidence files, and write one JSON file. The anchored assertion
follows /html-report section 2, item 1: computed CAGR, maximum drawdown, Sharpe
and final equity are compared against the row of the scaling table that the
ruling cites, and any deviation beyond ANCHOR_TOLERANCE raises SystemExit rather
than shipping a report that disagrees with the ruling. Evidence files are read
from disk for size, MD5 prefix and modification time only; a file that is absent
is recorded as not found instead of failing the run. Run from the repository
root as "python research/regime_lab/report/make_a0_report_data.py".

Out of scope: producing new experiment results, which belong to the backtest
entry scripts under scripts/ that write into backtest/results/; rendering, which
belongs to a0_report_template.html; assembling the delivered HTML, which belongs
to build_a0_report.py in this directory. Project-wide path constants live in
common/paths.py, but this script resolves its own paths from __file__ because it
is a standalone command-line entry point that no module imports.

Public functions:
    build()   Assemble and return the complete report payload as a dict.
    main()    Write the payload to OUT and print a one-line summary.

Constants:
    RUN_STEM          str    File-name stem selecting the single run this report
                             describes; it encodes strategy, capital tier, fee
                             tier, window and seed. Source: the result file names
                             present in backtest/results/.
    LIVE_FROM         str    Chart window start, "2018-01-01". Signals go live on
                             that date, and 2010 through 2017 is state
                             accumulation with zero capital occupied. Source:
                             comment at the declaration.
    ANCHOR_TOLERANCE  float  Anchored-assertion tolerance, 5e-4, applied as the
                             larger of the absolute and the relative bound.
                             Source unknown, needs verification.
    FLAT_EPS          float  Fraction of equity below which a position counts as
                             flat, 1e-4, so float dust from a fully exited book
                             does not paint a spurious holding interval. Source:
                             comment at the declaration.
    ROOT              Path   Repository root, resolved as parents[3] of this file.
    RESULTS           Path   ROOT/backtest/results, the run output directory.
    SCALING_CSV       Path   RESULTS/a0_capital_scaling_20260821.csv.
    OUT               Path   Output JSON, written beside this file.

Inputs:
    backtest/results/<RUN_STEM>.equity.parquet   columns read: ts, equity_gbp,
                                                 cash_gbp
    backtest/results/<RUN_STEM>.trades.parquet   columns read: ts, symbol,
                                                 quantity, cash_delta_gbp
    backtest/results/<RUN_STEM>.meta.json        run configuration under
                                                 "config"
    backtest/results/a0_capital_scaling_20260821.csv   the row with cash 1000 and
                                                 tier "actual", supplying cagr,
                                                 maxdd, sharpe and final for the
                                                 assertion and win_rate, pf,
                                                 cost_drag_pct and costs_gbp for
                                                 the KPI block
    trading212/strategy/a0_v0_0_1.py                         hashed, not parsed
    trading212/config/strategies/a0_v0_0_1.yaml              hashed, not parsed
    research/decisions/20260820_regime_lf_ruling.md          hashed, not parsed
    research/decisions/20260821_a0_framework_comparison.md   hashed, not parsed
Outputs:
    research/regime_lab/report/a0_report_data.json

Change log:
    2026-08-22  Header expanded to the six-section spec. The previous header
                stated that main() writes results/a0_report_data.json; OUT
                resolves to this file's own directory, and the text now matches
                the implementation.
"""

from __future__ import annotations

__all__ = ["build", "main", "RUN_STEM", "LIVE_FROM", "ANCHOR_TOLERANCE"]

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "backtest" / "results"
OUT = Path(__file__).resolve().parent / "a0_report_data.json"

RUN_STEM = ("a0_v0_0_1_a0_cap1000_actual_2010-01-04_2026-08-19"
            "_fee-actual_seed20260820")
SCALING_CSV = RESULTS / "a0_capital_scaling_20260821.csv"

# Signals go live 2018-01-01; 2010-2017 is state accumulation with zero capital
# occupied, so the chart window starts at go-live.
LIVE_FROM = "2018-01-01"
ANCHOR_TOLERANCE = 5e-4

# Positions worth less than this fraction of equity count as flat, so float
# dust from a fully exited book does not paint a spurious holding interval.
FLAT_EPS = 1e-4


def _verify(path: Path) -> dict:
    """Disk-verify one evidence file (html-report section 4.3.2)."""
    if not path.exists():
        return {"path": str(path.relative_to(ROOT)), "found": False}
    raw = path.read_bytes()
    return {
        "path": str(path.relative_to(ROOT)),
        "found": True,
        "bytes": len(raw),
        "md5_8": hashlib.md5(raw).hexdigest()[:8],
        "mtime": pd.Timestamp(path.stat().st_mtime, unit="s").strftime("%Y-%m-%d %H:%M"),
    }


def _holding_intervals(dates: pd.Series, invested: np.ndarray) -> list[dict]:
    """Contiguous date ranges where the book holds any position."""
    held = invested > FLAT_EPS
    spans, start = [], None
    for i, flag in enumerate(held):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            spans.append({"x0": str(dates.iloc[start].date()),
                          "x1": str(dates.iloc[i - 1].date())})
            start = None
    if start is not None:
        spans.append({"x0": str(dates.iloc[start].date()),
                      "x1": str(dates.iloc[-1].date())})
    return spans


def build() -> dict:
    """Assemble the full report payload."""
    equity = pd.read_parquet(RESULTS / f"{RUN_STEM}.equity.parquet")
    trades = pd.read_parquet(RESULTS / f"{RUN_STEM}.trades.parquet")
    meta = json.loads((RESULTS / f"{RUN_STEM}.meta.json").read_text())

    equity["ts"] = pd.to_datetime(equity["ts"])
    daily = equity.groupby(equity["ts"].dt.date).last().reset_index(drop=True)
    daily["date"] = pd.to_datetime(equity.groupby(equity["ts"].dt.date).last().index)
    daily = daily[daily["date"] >= pd.Timestamp(LIVE_FROM)].reset_index(drop=True)

    eq = daily["equity_gbp"].to_numpy()
    cash = daily["cash_gbp"].to_numpy()
    position_value = np.maximum(eq - cash, 0.0)
    exposure = np.where(eq > 0, position_value / eq, 0.0)
    drawdown = eq / np.maximum.accumulate(eq) - 1.0

    rets = pd.Series(eq).pct_change().dropna()
    n = len(rets)
    cagr = (eq[-1] / eq[0]) ** (252 / n) - 1
    max_dd = float(-drawdown.min())
    sharpe = float(rets.mean() / rets.std(ddof=1) * np.sqrt(252))

    trades["ts"] = pd.to_datetime(trades["ts"])
    trades["date"] = trades["ts"].dt.date
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

    scaling = pd.read_csv(SCALING_CSV)
    anchor = scaling[(scaling["cash"] == 1000)
                     & (scaling["tier"] == "actual")].iloc[0]
    checks = {"cagr": (cagr, float(anchor["cagr"])),
              "max_dd": (max_dd, float(anchor["maxdd"])),
              "sharpe": (sharpe, float(anchor["sharpe"])),
              "final_equity": (float(eq[-1]), float(anchor["final"]))}
    failures = [f"{k}: computed {a:.6f} vs recorded {b:.6f}"
                for k, (a, b) in checks.items()
                if abs(a - b) > max(ANCHOR_TOLERANCE, abs(b) * ANCHOR_TOLERANCE)]
    if failures:
        raise SystemExit("ANCHOR MISMATCH\n" + "\n".join(failures))

    trough = int(np.argmin(drawdown))
    peak = int(np.argmax(eq[:trough + 1])) if trough > 0 else 0

    return {
        "run": {
            "stem": RUN_STEM,
            "initial_cash_gbp": float(meta["config"]["initial_cash_gbp"]),
            "fee_tier": meta["config"]["fee_tier"],
            "strategy": f'{meta["config"]["strategy_name"]} v{meta["config"]["strategy_version"]}',
            "engine_window": f'{meta["config"]["start"]} .. {meta["config"]["end"]}',
            "live_from": LIVE_FROM,
            "symbol_count": len(meta["config"]["params"]["trade_symbols"]),
        },
        "kpi": {
            "cagr": round(cagr, 6), "max_dd": round(max_dd, 6),
            "sharpe": round(sharpe, 4),
            "win_rate": round(float(anchor["pf"] * 0 + anchor["win_rate"]), 6),
            "profit_factor": round(float(anchor["pf"]), 4),
            "final_equity": round(float(eq[-1]), 2),
            "initial_equity": round(float(eq[0]), 2),
            "ann_vol": round(float(rets.std(ddof=1) * np.sqrt(252)), 6),
            "fills": int(len(trades)),
            "trading_days": int(len(daily)),
            "days_in_market": int((exposure > FLAT_EPS).sum()),
            "mean_exposure": round(float(exposure.mean()), 6),
            "cost_drag_pct": round(float(anchor["cost_drag_pct"]), 4),
            "costs_gbp": round(float(anchor["costs_gbp"]), 2),
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
        "evidence": [
            {"id": "R-1", "kind": "结果", "supports": "净值、现金、占用逐日序列",
             **_verify(RESULTS / f"{RUN_STEM}.equity.parquet")},
            {"id": "R-2", "kind": "结果", "supports": "逐笔成交，用于成交点标注",
             **_verify(RESULTS / f"{RUN_STEM}.trades.parquet")},
            {"id": "R-3", "kind": "结果", "supports": "运行配置与费率档元数据",
             **_verify(RESULTS / f"{RUN_STEM}.meta.json")},
            {"id": "R-4", "kind": "结果", "supports": "KPI 锚定断言的比对基准",
             **_verify(SCALING_CSV)},
            {"id": "C-1", "kind": "代码", "supports": "A0 信号唯一副本",
             **_verify(ROOT / "trading212" / "strategy" / "a0_v0_0_1.py")},
            {"id": "C-2", "kind": "配置", "supports": "A0 参数基线",
             **_verify(ROOT / "trading212" / "config" / "strategies" / "a0_v0_0_1.yaml")},
            {"id": "V-1", "kind": "裁定", "supports": "A0 定义与研究口径数字",
             **_verify(ROOT / "research" / "decisions" / "20260820_regime_lf_ruling.md")},
            {"id": "V-2", "kind": "裁定", "supports": "框架接入口径与持仓时间统计",
             **_verify(ROOT / "research" / "decisions" / "20260821_a0_framework_comparison.md")},
        ],
        "anchor_report": {k: {"computed": round(a, 6), "recorded": round(b, 6)}
                          for k, (a, b) in checks.items()},
    }


def main() -> int:
    payload = build()
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"anchors OK: {payload['anchor_report']}")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB), "
          f"{len(payload['series']['dates'])} days, "
          f"{len(payload['markers'])} marker groups, "
          f"{len(payload['holding_spans'])} holding spans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
