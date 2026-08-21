"""Event study of the entry signal alone, with the exit rule removed.

Purpose: the pooled arms lose to the matched-horizon baseline, but that number
mixes two things, entry quality and exit quality. This module measures the entry
in isolation: every entry signal is followed for a fixed number of trading days
and scored against the same-length unconditional baseline on the same symbol.

Overlapping events are kept, so this is an event study rather than a tradeable
strategy: two signals eight days apart both count at a twenty-day horizon.

Status: exploratory, added after the pre-registered arms had already run
(backtest-discipline section 4.2).

Usage:
    python -m factor_kdj_macd.diagnose_entry

Public functions:
    event_study(window, rule_kwargs, horizons)   One entry spec, all horizons
    main()                                       Every spec, writes results/
"""

from __future__ import annotations

__all__ = ["event_study", "main", "HORIZONS", "ENTRY_SPECS"]

import numpy as np
import pandas as pd

from . import benchmark as benchmark_module
from . import data as data_module
from . import indicators as indicators_module
from . import rules as rules_module
from . import run_backtest as runner

# Forward holding lengths in trading days.
HORIZONS = (1, 3, 5, 10, 20, 60)

# Label -> rules.entry_exit keyword arguments. A-MAIN is absent because its entry
# condition never fires; the two nearest non-empty relaxations stand in for it.
ENTRY_SPECS: dict[str, dict] = {
    "kdj_cross_only": {"macd_mode": "none", "k_max": None},
    "kdj_k_lt_30": {"macd_mode": "none", "k_max": rules_module.K_MAX},
    "kdj_macd_both": {"macd_mode": "both", "k_max": None},
    "kdj_k_lt_30_dif_gt_dea": {"macd_mode": "dif_gt_dea", "k_max": rules_module.K_MAX},
    "kdj_k_lt_50_macd_both": {"macd_mode": "both", "k_max": 50.0},
}


def event_study(window: str, rule_kwargs: dict, horizons: tuple[int, ...] = HORIZONS) -> pd.DataFrame:
    """Score one entry specification at several fixed horizons.

    Entries are filled at the open of the bar after the signal, matching the
    conservative timing of the main arms. The comparison baseline is the same
    matched-horizon unconditional win rate used everywhere else.

    Args:
        window: Key of runner.WINDOWS.
        rule_kwargs: Keyword arguments for rules.entry_exit.
        horizons: Forward holding lengths in trading days.

    Returns:
        One row per horizon with n_events, win_rate, base_rate, edge_pp, the
        Poisson-binomial p-value, and the mean and median forward return.
    """
    start, end = runner.WINDOWS[window]
    per_horizon: dict[int, list[tuple[float, float]]] = {h: [] for h in horizons}

    for symbol in runner.SYMBOLS:
        bars = data_module.load_daily(symbol, end=end)
        frame, _ = indicators_module.add_indicators(bars, runner.KDJ_PARAMS, runner.MACD_PARAMS)
        entry_signal, _ = rules_module.entry_exit(frame, **rule_kwargs)

        first_bar = runner.WARMUP_BARS
        if start is not None:
            in_window = np.flatnonzero(frame["date"].to_numpy() >= np.datetime64(start))
            first_bar = max(first_bar, int(in_window[0])) if in_window.size else len(frame)

        opens = frame["open"].to_numpy()
        last_bar = len(frame) - 1
        # Signals fill on the next bar, so the entry bar is the signal bar plus one.
        entry_bars = np.flatnonzero(entry_signal)
        entry_bars = entry_bars[(entry_bars >= first_bar) & (entry_bars < last_bar)] + 1

        for horizon in horizons:
            usable = entry_bars[entry_bars + horizon <= last_bar]
            if usable.size == 0:
                continue
            returns = opens[usable + horizon] / opens[usable] - 1.0
            base_rate, _ = benchmark_module.horizon_win_rate(
                opens, first_bar + 1, last_bar, horizon
            )
            per_horizon[horizon].extend((float(r), base_rate) for r in returns)

    rows = []
    for horizon in horizons:
        events = per_horizon[horizon]
        if not events:
            continue
        returns = np.array([e[0] for e in events])
        baselines = np.array([e[1] for e in events])
        wins = int((returns > 0.0).sum())
        test = benchmark_module.poisson_binomial_test(wins, baselines)
        rows.append({
            "window": window, "horizon_bars": horizon, "n_events": len(events),
            "win_rate": wins / len(events), "base_rate": float(baselines.mean()),
            "edge_pp": 100.0 * (wins / len(events) - baselines.mean()),
            "p_value": test["p_value"],
            "mean_ret": float(returns.mean()), "median_ret": float(np.median(returns)),
        })
    return pd.DataFrame(rows)


def main() -> int:
    """Run every entry specification over every window and write results/."""
    frames = []
    for label, rule_kwargs in ENTRY_SPECS.items():
        for window in runner.WINDOWS:
            study = event_study(window, rule_kwargs)
            study.insert(0, "entry_spec", label)
            frames.append(study)
    table = pd.concat(frames, ignore_index=True)
    runner.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    table.to_csv(runner.RESULTS_DIR / "exploratory_entry_event_study.csv", index=False)
    print(table.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
