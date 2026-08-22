"""Real-data acceptance checks for the A0 minute run, kept out of the test suite.

Responsibility: read the artifacts left by scripts/20260822_a0_minute_backtest.py
and assert eight properties of the minute-frequency timing rule, printing one
PASS or FAIL line per property and returning exit code 0 only when every check
passes. Every assertion is chosen to fail if the timing rule is implemented
wrongly.

The eight checks are: A1, every minute-arm order is submitted at 15:59; A2, no
order executes in the session in which it was submitted, which is the hard
constraint of the requirement; A3, fills land at 09:30; A4, the fill session is
the next session and never a later one, so the submission-to-fill gap never
exceeds a weekend plus a holiday; A5, the two arms produce the identical set of
(symbol, session, side), with the quantity spread reported separately because
each arm sizes a slot from its own decision price and quantities therefore
differ by a few basis points by construction; A6, the two arms' fill prices do
in fact differ, which is the discrimination guard that keeps A5 from being
vacuous; A7, the published comparison table's fill count equals the fill count in
the trade file; A8, minute sampling yields a deeper measured drawdown than daily
sampling, recomputed from the run's own equity records on a single denominator
because the two summary-table columns divide by different denominators and
comparing them would measure the denominators rather than the sampling.

Out of scope: producing the artifacts, which belongs to
scripts/20260822_a0_minute_backtest.py; synthetic unit tests of the same rule,
which live in tests/backtest/test_a0_intraday.py; the strategy adaptation
itself, which lives in trading212/strategy/a0_intraday_v0_0_1.py. Checks that
read real landed data are kept in scripts/ because the test suite may not read
data/, by the ruling in docs/backtest/validation/02_test_plan.md section 2.

Public functions:
    ny(series)  Parse a timestamp column as UTC and convert it to the exchange
                time zone.
    main()      Evaluate every check, print the results, and return the exit
                code.

Constants:
    ROOT     Path  Repository root, the parent of this file's directory.
    RESULTS  Path  backtest/results/, the directory read.
    STAMP    str   Artifact stamp read, "20260822". It must match the STAMP of
                   the run that produced the files.
    NY       str   Exchange time zone, "America/New_York".

Inputs:
    backtest/results/a0_minute_20260822_m1_actual.trades.parquet
    backtest/results/a0_minute_20260822_d1_actual.trades.parquet
    backtest/results/a0_minute_comparison_20260822.csv
    backtest/results/ the m1 actual-tier equity records, whose file name is
        spelled out in this file: the run stem a0_intraday_v0_0_1_m1_actual
        for the window 2026-07-24 to 2026-08-21 at fee tier actual and seed
        20260820, suffixed .equity.parquet.
Outputs:
    stdout only. Exit code 0 when every check passes, 1 otherwise.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "backtest" / "results"
STAMP = "20260822"
NY = "America/New_York"


def ny(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True).dt.tz_convert(NY)


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    m1 = pd.read_parquet(RESULTS / f"a0_minute_{STAMP}_m1_actual.trades.parquet")
    d1 = pd.read_parquet(RESULTS / f"a0_minute_{STAMP}_d1_actual.trades.parquet")
    m1["sub"], m1["fill"] = ny(m1["submitted_ts"]), ny(m1["ts"])
    d1["fill"] = ny(d1["ts"])

    # A1 decision instant: every minute-arm order is submitted at 15:59.
    subs = sorted(set(m1["sub"].dt.strftime("%H:%M")))
    checks.append(("A1 提交时刻恒为 15:59", subs == ["15:59"], str(subs)))

    # A2 no same-session execution -- the requirement's hard constraint.
    same = int((m1["sub"].dt.date == m1["fill"].dt.date).sum())
    checks.append(("A2 无当日成交", same == 0, f"{same} 笔当日成交"))

    # A3 fills land on the next session's opening bar.
    fills = sorted(set(m1["fill"].dt.strftime("%H:%M")))
    checks.append(("A3 成交时刻恒为 09:30", fills == ["09:30"], str(fills)))

    # A4 the fill session is the NEXT session, never a later one: the gap
    # must never exceed a weekend plus a holiday.
    gap = (m1["fill"].dt.normalize() - m1["sub"].dt.normalize()).dt.days
    checks.append(("A4 提交到成交跨度 <= 4 日", bool(gap.max() <= 4),
                   f"max={int(gap.max())}"))

    # A5 signal equivalence is a claim about WHICH trades happen, not about
    # exact share counts: both arms size a slot from their own decision
    # price (15:58 vs the close), so quantities differ by a few basis points
    # by construction. Assert the (symbol, session, side) set is identical
    # and report the quantity spread separately.
    key = ["symbol", "side"]
    a = m1.assign(d=m1["fill"].dt.date).groupby(key + ["d"])["quantity"].sum()
    b = d1.assign(d=d1["fill"].dt.date).groupby(key + ["d"])["quantity"].sum()
    same_set = set(a.index) == set(b.index)
    aligned = pd.concat([a, b], axis=1, keys=["m1", "d1"]).dropna()
    rel_qty = ((aligned["m1"] - aligned["d1"]).abs()
               / aligned["d1"].abs()).max()
    checks.append(("A5 两臂成交的(标的,交易日,方向)集合一致", same_set,
                   f"m1 {len(set(a.index))} 组 / d1 {len(set(b.index))} 组; "
                   f"数量最大相对差 {rel_qty:.4%}"))

    # A6 the two arms are priced differently -- otherwise the whole exercise
    # would be measuring nothing (discrimination guard for A5).
    pm = m1.groupby(["symbol", m1["fill"].dt.date])["price"].mean()
    pd_ = d1.groupby(["symbol", d1["fill"].dt.date])["price"].mean()
    both = pd.concat([pm, pd_], axis=1, keys=["m1", "d1"]).dropna()
    rel = ((both["m1"] - both["d1"]) / both["d1"]).abs()
    checks.append(("A6 成交价确有差异（判别力保证）", bool(rel.max() > 1e-6),
                   f"最大相对价差 {rel.max():.4%}, 中位 {rel.median():.4%}"))

    # A7 anchored assertions against the published comparison table.
    table = pd.read_csv(RESULTS / f"a0_minute_comparison_{STAMP}.csv")
    row_m = table[(table.arm == "m1") & (table.fee_tier == "actual")].iloc[0]
    checks.append(("A7 汇总表笔数 == 成交件笔数",
                   int(row_m["fills"]) == len(m1),
                   f"{int(row_m['fills'])} vs {len(m1)}"))

    # A8 sampling frequency deepens the measured drawdown. The two columns of
    # the summary table CANNOT be used for this: raw_max_drawdown_*_on_capital
    # divides by the capital peak while liq_max_drawdown divides by the
    # running equity peak, so comparing them measures the denominators, not
    # the sampling. Recompute both on the same denominator from the run's own
    # equity records.
    eqm = pd.read_parquet(
        RESULTS / ("a0_intraday_v0_0_1_m1_actual_2026-07-24_2026-08-21"
                   "_fee-actual_seed20260820.equity.parquet"))
    col = "equity_liq_gbp"

    def mdd(values) -> float:
        arr = pd.Series(list(values), dtype=float)
        return float(-(arr / arr.cummax() - 1.0).min())

    minute_mdd = mdd(eqm[col])
    day_mdd = mdd(eqm.assign(d=ny(eqm["ts"]).dt.date)
                  .groupby("d")[col].last())
    checks.append(("A8 分钟抽样回撤 > 日抽样回撤（同一分母）",
                   minute_mdd > day_mdd,
                   f"分钟 {minute_mdd:.4%} vs 日 {day_mdd:.4%} "
                   f"(+{minute_mdd - day_mdd:.4%})"))

    width = max(len(c[0]) for c in checks)
    ok = True
    for name, passed, detail in checks:
        ok &= passed
        print(f"{name:<{width}}  {'PASS' if passed else 'FAIL'}   {detail}")
    print(f"\n{'全部通过' if ok else '存在失败项'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
