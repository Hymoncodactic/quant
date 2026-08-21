"""Two-stage runner for the regime study: price all 450 configs, select on S,
grade on V, run the rigor statistics, persist everything.

Executes the frozen protocol of research/prereg/20260820_regime_lf_prereg.md.
Selection reads only S-stage metrics; V-stage metrics are computed for the
whole family (stored for the overfit diagnostic) but grading reads only the
shortlist plus the fixed benchmarks.

Usage:
    python -m regime_lab.run_search

Public functions:
    price_family(panels)      Daily returns for every config, one frame
    select(s_metrics)         Frozen shortlist rule
    main()                    Full protocol, writes results/
"""

from __future__ import annotations

__all__ = ["price_family", "select", "main", "S_WINDOW", "V_WINDOW", "BENCHMARK_CONFIG"]

import json

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from . import configs as configs_module
from . import data as data_module
from . import metrics as metrics_module
from . import rigor as rigor_module
from .engine import Panels

RESULTS_DIR = __import__("pathlib").Path(__file__).resolve().parent / "results"

S_WINDOW = ("2000-01-03", "2017-12-29")
V_WINDOW = ("2018-01-01", "2026-08-19")

# The pure benchmark; excluded from selection, prereg sections 4 and 7.
BENCHMARK_CONFIG = ("cc", "always", "none", "off", "off")

SHORTLIST_SIZE = 5
MAX_PER_TIMING = 2


def price_family(panels: Panels) -> pd.DataFrame:
    """Daily return series for every configuration in the frozen family."""
    columns = {}
    for config in configs_module.FAMILY:
        columns[configs_module.config_label(config)] = panels.run(config)
    return pd.DataFrame(columns)


def _window_metrics(returns_frame: pd.DataFrame, window: tuple[str, str]) -> pd.DataFrame:
    """Summarize every column of a return frame over one window."""
    rows = []
    sliced = returns_frame.loc[window[0]: window[1]]
    for label in sliced.columns:
        row = metrics_module.summarize(sliced[label])
        row["config"] = label
        rows.append(row)
    return pd.DataFrame(rows).set_index("config")


def select(s_metrics: pd.DataFrame) -> list[str]:
    """Frozen shortlist: top-5 S-stage Calmar, at most 2 per timing, benchmark barred."""
    barred = configs_module.config_label(BENCHMARK_CONFIG)
    ranked = s_metrics.drop(index=barred).sort_values("calmar", ascending=False)
    shortlist: list[str] = []
    per_timing: dict[str, int] = {}
    for label in ranked.index:
        timing = label.split("|")[0]
        if per_timing.get(timing, 0) >= MAX_PER_TIMING:
            continue
        per_timing[timing] = per_timing.get(timing, 0) + 1
        shortlist.append(label)
        if len(shortlist) >= SHORTLIST_SIZE:
            break
    return shortlist


def _diagnostics(returns: pd.Series, gate_series: pd.Series | None) -> dict:
    """Yearly returns and crisis-window behavior for one config on V."""
    yearly = {int(year): round(float((1.0 + group).prod() - 1.0), 4)
              for year, group in returns.groupby(returns.index.year)}
    crisis = {}
    for tag, lo, hi in (("covid_2020", "2020-02-01", "2020-04-30"),
                        ("bear_2022", "2022-01-01", "2022-12-31")):
        segment = returns.loc[lo:hi]
        depth, _, _ = metrics_module.max_drawdown(segment)
        crisis[tag] = {"return": round(float((1.0 + segment).prod() - 1.0), 4),
                       "max_dd": round(depth, 4)}
    out = {"yearly": yearly, "crisis": crisis}
    if gate_series is not None:
        changes = int((gate_series != gate_series.shift(1)).sum())
        out["gate_switches"] = changes
    return out


def main() -> int:
    """Run the full frozen protocol and persist every table."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print("building panels ...")
    panels = Panels.build(data_module.TECH18)
    family = price_family(panels)
    family.to_parquet(RESULTS_DIR / "family_daily_returns.parquet")

    s_metrics = _window_metrics(family, S_WINDOW)
    v_metrics = _window_metrics(family, V_WINDOW)
    s_metrics.to_csv(RESULTS_DIR / "family_metrics_S.csv")
    v_metrics.to_csv(RESULTS_DIR / "family_metrics_V.csv")

    shortlist = select(s_metrics)
    benchmark_label = configs_module.config_label(BENCHMARK_CONFIG)

    # SPA on S: whole family against the EW18 buy-and-hold benchmark.
    s_slice = family.loc[S_WINDOW[0]: S_WINDOW[1]]
    spa = rigor_module.spa_test(s_slice.drop(columns=[benchmark_label]),
                                s_slice[benchmark_label])

    # Trial-Sharpe variance across the family on S, for the DSR luck bar.
    s_daily = s_slice.drop(columns=[benchmark_label])
    trial_sharpes = s_daily.mean() / s_daily.std(ddof=1)
    sharpe_variance = float(trial_sharpes.var(ddof=1))

    # Survivorship control: same structures on QQQ alone.
    qqq_panels = Panels.build(("QQQ",), group="us_etf")
    qqq_family = {label: qqq_panels.run(tuple(label.split("|")))
                  for label in shortlist + [benchmark_label]}

    graded = []
    for label in shortlist:
        v_returns = family[label].loc[V_WINDOW[0]: V_WINDOW[1]]
        row = {"config": label, "stage": "V", **metrics_module.summarize(v_returns)}
        row["s_calmar"] = float(s_metrics.loc[label, "calmar"])
        row["s_cagr"] = float(s_metrics.loc[label, "cagr"])
        row["s_max_dd"] = float(s_metrics.loc[label, "max_dd"])
        dsr = rigor_module.deflated_sharpe(v_returns, len(configs_module.FAMILY) - 1,
                                           sharpe_variance)
        row.update({f"dsr_{k}": v for k, v in dsr.items()})
        row["cagr_ci90"] = metrics_module.bootstrap_ci(v_returns, "cagr")
        row["maxdd_ci90"] = metrics_module.bootstrap_ci(v_returns, "max_dd")
        row["target_pass"] = bool(row["cagr"] >= 0.30 and row["max_dd"] <= 0.20)

        gate = label.split("|")[2]
        gate_series = panels.market_mult[gate].loc[V_WINDOW[0]: V_WINDOW[1]] if gate != "none" else None
        row["diagnostics"] = _diagnostics(v_returns, gate_series)

        qqq_v = qqq_family[label].loc[V_WINDOW[0]: V_WINDOW[1]]
        row["qqq_control"] = {k: round(v, 4) if isinstance(v, float) else v
                              for k, v in metrics_module.summarize(qqq_v).items()}
        graded.append(row)

    overfit_rank_corr = float(scipy_stats.spearmanr(
        s_metrics["calmar"].replace([np.inf, -np.inf], np.nan).dropna(),
        v_metrics["calmar"].reindex(s_metrics["calmar"].replace([np.inf, -np.inf], np.nan).dropna().index)
    ).statistic)

    benchmarks = {}
    for tag, series in (("ew18_bh", family[benchmark_label]),
                        ("qqq_bh", qqq_family[benchmark_label])):
        benchmarks[tag] = {stage: {k: round(v, 4) if isinstance(v, float) else v
                                   for k, v in metrics_module.summarize(
                                       series.loc[w[0]:w[1]]).items()}
                           for stage, w in (("S", S_WINDOW), ("V", V_WINDOW))}

    output = {
        "shortlist": shortlist, "graded": graded, "spa_S": spa,
        "trial_sharpe_variance_S": sharpe_variance,
        "overfit_spearman_calmar_S_to_V": overfit_rank_corr,
        "benchmarks": benchmarks,
    }
    (RESULTS_DIR / "search_summary.json").write_text(
        json.dumps(output, indent=2, default=str))

    print(f"\nshortlist (by S Calmar): {shortlist}")
    print(f"SPA on S vs EW18: t={spa['t_spa']:.3f} p={spa['p_value']:.4f} "
          f"(lower {spa['p_lower']:.4f} / upper {spa['p_upper']:.4f}), best={spa['best_config']}")
    print(f"S->V Calmar Spearman: {overfit_rank_corr:.3f}")
    print(f"EW18 B&H V: {benchmarks['ew18_bh']['V']}")
    print(f"QQQ  B&H V: {benchmarks['qqq_bh']['V']}")
    for row in graded:
        print(f"\n{row['config']}: V CAGR {row['cagr']:.2%} (CI90 {row['cagr_ci90'][0]:.2%}"
              f"..{row['cagr_ci90'][1]:.2%}), MaxDD {row['max_dd']:.2%} "
              f"(CI90 {row['maxdd_ci90'][0]:.2%}..{row['maxdd_ci90'][1]:.2%}), "
              f"Sharpe {row['sharpe']:.2f}, DSR {row['dsr_dsr']:.3f}, "
              f"target {'PASS' if row['target_pass'] else 'FAIL'}")
        print(f"  S: CAGR {row['s_cagr']:.2%} MaxDD {row['s_max_dd']:.2%} Calmar {row['s_calmar']:.2f}")
        print(f"  QQQ control V: {row['qqq_control']}")
        print(f"  yearly V: {row['diagnostics']['yearly']}")
        print(f"  crisis: {row['diagnostics']['crisis']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
