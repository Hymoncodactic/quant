"""Real-data acceptance checks for the A0 intraday runs at 1m, 5m and 1h.

Tests under tests/ may not read data/, so the real-data assertions live here.
Every check is chosen to FAIL if the timing rule is implemented wrongly; a
check that would pass under a defective implementation is worthless and is
not listed.

Usage:
    python scripts/20260822_a0_intraday_verify.py
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "backtest" / "results"
STAMP = "20260822"
NY = "America/New_York"

# interval -> (window start, window end, decision time, session bars)
SPEC = {
    "1m": ("2026-07-24", "2026-08-21", "15:59", 390),
    "5m": ("2026-06-02", "2026-08-21", "15:55", 78),
    "1h": ("2023-11-07", "2026-08-21", "15:30", 7),
}


def ny(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True).dt.tz_convert(NY)


def mdd(values) -> float:
    arr = pd.Series(list(values), dtype=float)
    return float(-(arr / arr.cummax() - 1.0).min())


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    for iv, (start, end, dtime, _bars) in SPEC.items():
      for timing in ("same_close", "next_open"):
        i = pd.read_parquet(
            RESULTS / f"a0_intraday_{STAMP}_{iv}_{iv}"
                      f"_{timing}_actual.trades.parquet")
        d = pd.read_parquet(
            RESULTS / f"a0_intraday_{STAMP}_{iv}_d1"
                      f"_{timing}_actual.trades.parquet")
        i["sub"], i["fill"] = ny(i["submitted_ts"]), ny(i["ts"])
        d["fill"] = ny(d["ts"])

        tag = f"{iv}/{timing}"
        subs = sorted(set(i["sub"].dt.strftime("%H:%M")))
        checks.append((f"{tag} A1 提交时刻恒为决策 bar {dtime}",
                       subs == [dtime], str(subs)))

        same = int((i["sub"].dt.date == i["fill"].dt.date).sum())
        fills = sorted(set(i["fill"].dt.strftime("%H:%M")))
        gap = (i["fill"].dt.normalize() - i["sub"].dt.normalize()).dt.days
        if timing == "same_close":
            # The whole point of same_close: the order is submitted about a
            # minute before the close and executes on the SAME session, at
            # the decision bar. Nothing may execute at the opening print.
            checks.append((f"{tag} A2 全部当场次成交", same == len(i),
                           f"{same}/{len(i)} 笔"))
            checks.append((f"{tag} A3 成交时刻恒为决策 bar {dtime}，且从不在 09:30",
                           fills == [dtime], str(fills)))
            checks.append((f"{tag} A4 提交与成交同日", bool(gap.max() == 0),
                           f"max={int(gap.max())}"))
            atc = i["at_close"].all() if "at_close" in i.columns else False
            checks.append((f"{tag} A4b 全部成交带 at_close 标记", bool(atc),
                           f"{int(i['at_close'].sum()) if 'at_close' in i.columns else 0}"
                           f"/{len(i)}"))
        else:
            checks.append((f"{tag} A2 无当场次成交", same == 0, f"{same} 笔"))
            checks.append((f"{tag} A3 成交时刻恒为次场次开盘 09:30",
                           fills == ["09:30"], str(fills)))
            checks.append((f"{tag} A4 提交到成交跨度 <= 5 日",
                           bool(gap.max() <= 5), f"max={int(gap.max())}"))

        # A5/A6: at 1m and 5m the information lag is short enough that the
        # decision matches the close-based one exactly, so the trade sets must
        # be identical; at 1h the 30-minute lag genuinely changes trades, so
        # equality is NOT expected and asserting it would be wrong.
        key = ["symbol", "side"]
        a = i.assign(day=i["fill"].dt.date).groupby(key + ["day"])["quantity"].sum()
        b = d.assign(day=d["fill"].dt.date).groupby(key + ["day"])["quantity"].sum()
        overlap = len(set(a.index) & set(b.index))
        if iv in ("1m", "5m"):
            checks.append((f"{tag} A5 两臂成交的(标的,场次,方向)集合一致",
                           set(a.index) == set(b.index),
                           f"{len(set(a.index))} vs {len(set(b.index))} 组"))
        else:
            checks.append((f"{tag} A5 两臂成交集合确有分歧（30 分钟滞后应改变交易）",
                           set(a.index) != set(b.index),
                           f"{len(set(a.index))} vs {len(set(b.index))} 组, "
                           f"交集 {overlap}"))

        joined = pd.concat([a, b], axis=1, keys=["i", "d"]).dropna()
        pi = i.groupby(["symbol", i["fill"].dt.date, "side"])["price"].mean()
        pdd = d.groupby(["symbol", d["fill"].dt.date, "side"])["price"].mean()
        both = pd.concat([pi, pdd], axis=1, keys=["i", "d"]).dropna()
        rel = ((both["i"] - both["d"]) / both["d"]).abs()
        checks.append((f"{tag} A6 成交价确有差异（A5 的判别力保证）",
                       bool(len(rel) and rel.max() > 1e-6),
                       f"最大 {rel.max():.4%}, 中位 {rel.median():.4%}"))

        eqf = sorted(glob.glob(str(
            RESULTS / f"a0_intraday_v0_0_1_{iv}_{timing}_actual_{start}_{end}"
                      f"_fee-actual_fill-{timing}_*.equity.parquet")))
        eq = pd.read_parquet(eqf[-1])
        eq["day"] = ny(eq["ts"]).dt.date
        bar_mdd = mdd(eq["equity_liq_gbp"])
        day_mdd = mdd(eq.groupby("day")["equity_liq_gbp"].last())
        checks.append((f"{tag} A7 bar 抽样回撤 > 日抽样回撤（同一分母）",
                       bar_mdd > day_mdd,
                       f"{bar_mdd:.4%} vs {day_mdd:.4%} (+{bar_mdd-day_mdd:.4%})"))

        checks.append((f"{tag} A8 结果件窗口与声明一致",
                       f"_{start}_{end}_" in eqf[-1],
                       Path(eqf[-1]).name))

    width = max(len(c[0]) for c in checks)
    ok = True
    for name, passed, detail in checks:
        ok &= bool(passed)
        print(f"{name:<{width}}  {'PASS' if passed else 'FAIL'}   {detail}")
    print(f"\n{'全部通过' if ok else '存在失败项'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
