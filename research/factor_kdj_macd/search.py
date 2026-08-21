"""Two-stage exit-rule search: screen 378 cells in-sample, validate the shortlist.

Responsibility: execute the frozen protocol of
research/prereg/20260820_kdj_exit_search_prereg.md — run the full grid on the
search stage, apply the frozen screen mechanically, then run only the surviving
cells (plus the two user-named cells) on the two validation stages. Not
responsible for: rule definitions (rules.py, exits.py), fills (engine.py),
statistics (benchmark.py), judgement (the ruling document).

Stages, prereg section 4:
    S    core-5 symbols, 2000-01-01 .. 2017-12-31   full 378-cell grid
    V1   core-5 symbols, 2018-01-01 .. 2026-08-19   shortlist only
    V2   fresh-13 symbols, same period              shortlist only

The S stage loads bars with end=2017-12-31, so validation-era prices cannot
enter the search statistics through any code path.

Usage:
    python -m factor_kdj_macd.search             both stages, in order
    python -m factor_kdj_macd.search --stage S   grid + shortlist only

Public functions:
    run_cell_spec(symbol, cell, stage)   One symbol, one cell, one stage
    run_grid()                           All 378 cells on S, returns the table
    screen(grid)                         Frozen screen + shortlist, prereg 5.1
    validate(shortlist)                  Shortlist on V1 and V2
    main(argv)                           Entry point, writes results/
"""

from __future__ import annotations

__all__ = ["run_cell_spec", "run_grid", "screen", "validate", "main",
           "K_BANDS", "MACD_MODES_SEARCHED", "STAGES", "CORE5", "FRESH13",
           "NAMED_CELLS", "SCREEN_RULES"]

import argparse
import itertools
import json
import sys

import numpy as np
import pandas as pd

from . import benchmark as benchmark_module
from . import data as data_module
from . import engine as engine_module
from . import exits as exits_module
from . import indicators as indicators_module
from . import rules as rules_module
from . import run_backtest as runner

RESULTS_DIR = runner.RESULTS_DIR

CORE5 = ("NVDA", "AAPL", "MSFT", "META", "AMD")
FRESH13 = ("AMAT", "AMZN", "AVGO", "DELL", "GOOGL", "INTC", "LRCX",
           "MRVL", "MU", "ORCL", "PLTR", "TSLA", "TSM")

# Stage -> (symbols, first signal date, data end date). Prereg section 4.
STAGES: dict[str, tuple[tuple[str, ...], str, str]] = {
    "S": (CORE5, "2000-01-01", "2017-12-31"),
    "V1": (CORE5, "2018-01-01", "2026-08-19"),
    "V2": (FRESH13, "2018-01-01", "2026-08-19"),
}

# Band label -> (k_min, k_max), inclusive low, exclusive high. Prereg 3.1.
K_BANDS: dict[str, tuple[float | None, float | None]] = {
    "b_00_20": (None, 20.0),
    "b_20_50": (20.0, 50.0),
    "b_50_80": (50.0, 80.0),
    "b_20_80": (20.0, 80.0),
    "b_00_80": (None, 80.0),
    "b_any": (None, None),
}

MACD_MODES_SEARCHED = ("none", "dif_gt_dea", "dif_gt_zero", "both",
                       "hist_rising", "recent_cross", "dif_rising")

# The two cells the user named, always validated regardless of screening and
# graded separately. Prereg 5.1.
NAMED_CELLS = (
    ("b_20_80", "none", "death_cross"),
    ("b_20_80", "both", "death_cross"),
)

# Frozen screen, prereg 5.1. Applied mechanically in screen().
SCREEN_RULES = {
    "min_closed": 60,
    "min_edge_pp": 3.0,
    "max_p": 0.01,
    "min_positive_symbols": 4,
    "shortlist_size": 5,
    "max_per_family": 2,
}

# Cache: (symbol, end) -> indicator frame. Indicators are causal, so a frame cut
# at 2017 equals the prefix of the full frame; caching per end date just avoids
# reloading parquet partitions hundreds of times.
_FRAMES: dict[tuple[str, str], pd.DataFrame] = {}


def _frame(symbol: str, end: str) -> pd.DataFrame:
    """Load and cache the indicator frame for one symbol up to one end date."""
    key = (symbol, end)
    if key not in _FRAMES:
        bars = data_module.load_daily(symbol, end=end)
        frame, _ = indicators_module.add_indicators(bars, runner.KDJ_PARAMS, runner.MACD_PARAMS)
        _FRAMES[key] = frame
    return _FRAMES[key]


def run_cell_spec(
    symbol: str,
    cell: tuple[str, str, str],
    stage: str,
    shift: bool = False,
) -> pd.DataFrame:
    """Run one (band, macd_mode, exit) cell for one symbol on one stage.

    Args:
        symbol: Ticker.
        cell: (band label, MACD mode, exit name).
        stage: Key of STAGES.
        shift: Move indicators one bar earlier (the look-ahead control).

    Returns:
        Trade frame with benchmark columns attached, arm labeled band|macd|exit.
    """
    band, macd_mode, exit_name = cell
    _, start, end = STAGES[stage]
    frame = _frame(symbol, end)

    signal_frame = rules_module.shift_forward(frame) if shift else frame
    k_min, k_max = K_BANDS[band]
    entry_signal, _ = rules_module.entry_exit(
        signal_frame, macd_mode=macd_mode, k_max=k_max, k_min=k_min
    )
    exit_spec = exits_module.build_exit_spec(exit_name, signal_frame)

    first_bar = runner.WARMUP_BARS
    in_window = np.flatnonzero(frame["date"].to_numpy() >= np.datetime64(start))
    first_bar = max(first_bar, int(in_window[0])) if in_window.size else len(frame)

    arm = f"{band}|{macd_mode}|{exit_name}" + ("|LOOKAHEAD" if shift else "")
    trades = engine_module.extract_trades_spec(
        frame, entry_signal, exit_spec, first_bar, fill="next_open", symbol=symbol, arm=arm
    )
    opens = frame["open"].to_numpy()
    return benchmark_module.benchmark_trades(opens, trades, first_bar + 1, len(frame) - 1)


def _cell_summary(cell: tuple[str, str, str], stage: str, shift: bool = False) -> tuple[dict, pd.DataFrame]:
    """Pool one cell across the stage's symbols and reduce to the reported stats."""
    symbols, _, _ = STAGES[stage]
    frames = [run_cell_spec(symbol, cell, stage, shift=shift) for symbol in symbols]
    pooled = pd.concat(frames, ignore_index=True)
    band, macd_mode, exit_name = cell
    row = runner._summarize(pooled, {
        "stage": stage, "band": band, "macd_mode": macd_mode, "exit": exit_name,
        "lookahead": shift,
    })
    per_symbol_edges = []
    for symbol_frame in frames:
        closed = symbol_frame[symbol_frame["closed"]] if len(symbol_frame) else symbol_frame
        if len(closed) >= 10:
            base = float(np.nanmean(closed["base_rate"].to_numpy()))
            per_symbol_edges.append(float((closed["ret"] > 0).mean()) - base)
    row["symbols_scored"] = len(per_symbol_edges)
    row["symbols_with_positive_edge"] = int(sum(1 for e in per_symbol_edges if e > 0))
    return row, pooled


def run_grid() -> pd.DataFrame:
    """Run every cell of the frozen grid on the search stage."""
    cells = list(itertools.product(K_BANDS, MACD_MODES_SEARCHED, exits_module.EXIT_NAMES))
    rows = []
    for index, cell in enumerate(cells):
        row, _ = _cell_summary(cell, "S")
        rows.append(row)
        if (index + 1) % 63 == 0:
            print(f"  grid {index + 1}/{len(cells)}", file=sys.stderr)
    return pd.DataFrame(rows)


def screen(grid: pd.DataFrame) -> list[dict]:
    """Apply the frozen screen and family cap. Prereg 5.1, no discretion.

    Args:
        grid: Output of run_grid.

    Returns:
        Shortlist entries, each {"cell": (band, macd, exit), "reason": ...},
        screened winners first (by descending edge), then the named cells.
    """
    rules = SCREEN_RULES
    passing = grid[
        (grid["n_closed"] >= rules["min_closed"])
        & (grid["edge_pp"] >= rules["min_edge_pp"])
        & (grid["p_value"] < rules["max_p"])
        & (grid["symbols_with_positive_edge"] >= rules["min_positive_symbols"])
    ].sort_values("edge_pp", ascending=False)

    shortlist: list[dict] = []
    family_counts: dict[str, int] = {}
    for _, row in passing.iterrows():
        family = exits_module.EXIT_FAMILY[row["exit"]]
        if family_counts.get(family, 0) >= rules["max_per_family"]:
            continue
        family_counts[family] = family_counts.get(family, 0) + 1
        shortlist.append({"cell": (row["band"], row["macd_mode"], row["exit"]),
                          "reason": "screened", "search_edge_pp": float(row["edge_pp"])})
        if len(shortlist) >= rules["shortlist_size"]:
            break

    for cell in NAMED_CELLS:
        if not any(entry["cell"] == cell for entry in shortlist):
            match = grid[(grid["band"] == cell[0]) & (grid["macd_mode"] == cell[1])
                         & (grid["exit"] == cell[2])]
            edge = float(match["edge_pp"].iloc[0]) if len(match) else float("nan")
            shortlist.append({"cell": cell, "reason": "user_named", "search_edge_pp": edge})
    return shortlist


def validate(shortlist: list[dict]) -> pd.DataFrame:
    """Run every shortlisted cell on both validation stages plus diagnostics.

    For each cell: V1 and V2 summaries, a pooled V1+V2 test, the S-stage
    look-ahead control, and entry-year edge buckets on the pooled validation
    trades. Grading itself happens in the ruling, not here.
    """
    rows = []
    for entry in shortlist:
        cell = tuple(entry["cell"])
        pooled_frames = []
        for stage in ("V1", "V2"):
            row, pooled = _cell_summary(cell, stage)
            row["reason"] = entry["reason"]
            row["search_edge_pp"] = entry["search_edge_pp"]
            rows.append(row)
            pooled_frames.append(pooled)

        both = pd.concat(pooled_frames, ignore_index=True)
        row = runner._summarize(both, {
            "stage": "V1+V2", "band": cell[0], "macd_mode": cell[1], "exit": cell[2],
            "lookahead": False,
        })
        row["reason"] = entry["reason"]
        row["search_edge_pp"] = entry["search_edge_pp"]
        rows.append(row)

        control, _ = _cell_summary(cell, "S", shift=True)
        control["reason"] = "lookahead_control"
        control["search_edge_pp"] = entry["search_edge_pp"]
        rows.append(control)

        closed = both[both["closed"]].copy()
        if len(closed):
            closed["entry_year"] = pd.to_datetime(closed["entry_date"]).dt.year
            buckets = []
            for year, group in closed.groupby("entry_year"):
                base = float(np.nanmean(group["base_rate"].to_numpy()))
                buckets.append({"year": int(year), "n": int(len(group)),
                                "edge_pp": round(100.0 * (float((group["ret"] > 0).mean()) - base), 2)})
            entry["yearly_edge"] = buckets
        cell_tag = "_".join(cell)
        both.to_csv(RESULTS_DIR / f"trades_search_{cell_tag}_V.csv", index=False)
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    """Run the two-stage protocol and persist every table."""
    parser = argparse.ArgumentParser(description="KDJ band x MACD mode x exit-rule search")
    parser.add_argument("--stage", choices=["S", "all"], default="all")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    grid = run_grid()
    grid.to_csv(RESULTS_DIR / "search_grid_S.csv", index=False)
    shortlist = screen(grid)
    print(f"\nscreen: {sum(1 for e in shortlist if e['reason'] == 'screened')} cells pass, "
          f"{len(shortlist)} total with named cells")
    for entry in shortlist:
        print(f"  {entry['reason']:>10s}  {'|'.join(entry['cell'])}  "
              f"S-edge {entry['search_edge_pp']:+.2f}pp")

    if args.stage == "S":
        (RESULTS_DIR / "search_shortlist.json").write_text(json.dumps(shortlist, indent=2))
        return 0

    validation = validate(shortlist)
    validation.to_csv(RESULTS_DIR / "search_validation.csv", index=False)
    (RESULTS_DIR / "search_shortlist.json").write_text(json.dumps(shortlist, indent=2))

    columns = ["stage", "band", "macd_mode", "exit", "reason", "n_closed", "win_rate",
               "base_rate", "edge_pp", "p_value", "mean_ret", "median_ret",
               "mean_hold_bars", "symbols_with_positive_edge", "symbols_scored"]
    print()
    print(validation[columns].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
