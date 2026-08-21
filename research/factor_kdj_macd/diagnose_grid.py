"""Exploratory relaxation grid over the K bound and the MACD filter mode.

Purpose: the pre-registered rule (K < 30 at the KDJ golden cross, plus DIF > DEA
and DIF > 0) produces zero trades on this universe. This grid measures how many
signals each neighbouring specification produces, so the specification that
matches an actual hand calculation can be identified by its signal count rather
than guessed.

Status: exploratory, added after the pre-registered arms had already run. Nothing
here may be adopted on the strength of its win rate. backtest-discipline section
4.2 requires such additions to be logged and downgraded, and section 3.2 flags
picking the best cell of a grid as a red flag. The grid is a diagnostic, not a
search for a winner.

Usage:
    python -m factor_kdj_macd.diagnose_grid

Public functions:
    run_grid(window)   Every (k_max, macd_mode) cell for one window
    main()             Both windows, writes results/exploratory_grid.csv
"""

from __future__ import annotations

__all__ = ["run_grid", "main", "K_BOUNDS", "MACD_MODES_TRIED"]

import pandas as pd

from . import rules as rules_module
from . import run_backtest as runner

# None means the K bound is dropped entirely.
K_BOUNDS: tuple[float | None, ...] = (None, 80.0, 50.0, 30.0, 20.0)

MACD_MODES_TRIED: tuple[str, ...] = (
    "none", "dif_gt_dea", "dif_gt_zero", "both", "hist_rising", "recent_cross",
)


def run_grid(window: str) -> pd.DataFrame:
    """Run every (k_max, macd_mode) combination for one window.

    Args:
        window: Key of runner.WINDOWS.

    Returns:
        One row per cell with the pooled statistics from runner._summarize plus
        k_max and macd_mode. Cells with no trades keep NaN statistics rather than
        being dropped, so an empty specification stays visible.
    """
    rows = []
    for k_max in K_BOUNDS:
        for macd_mode in MACD_MODES_TRIED:
            frames = []
            for symbol in runner.SYMBOLS:
                trades, _ = runner.run_spec(
                    symbol, window,
                    {"macd_mode": macd_mode, "k_max": k_max},
                    shift=False, fill="next_open",
                    arm=f"k_max={k_max}|macd={macd_mode}",
                )
                frames.append(trades)
            pooled = pd.concat(frames, ignore_index=True)
            row = runner._summarize(pooled, {
                "window": window,
                "k_max": "none" if k_max is None else f"{k_max:g}",
                "macd_mode": macd_mode,
            })
            rows.append(row)
    return pd.DataFrame(rows)


def main() -> int:
    """Run the grid over every window and write results/exploratory_grid.csv."""
    grids = [run_grid(window) for window in runner.WINDOWS]
    grid = pd.concat(grids, ignore_index=True)
    runner.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    grid.to_csv(runner.RESULTS_DIR / "exploratory_grid.csv", index=False)

    columns = ["window", "k_max", "macd_mode", "n_closed", "win_rate", "base_rate",
               "edge_pp", "p_value", "mean_ret", "median_ret", "mean_hold_bars"]
    print(grid[columns].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
