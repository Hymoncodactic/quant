"""Attribution and tail diagnostics for the k_over_80 exit winners.

Purpose, prereg addendum section 9: the K>=80 exit is an endogenous stopping
time (it waits for local strength before selling), which the matched-horizon
benchmark cannot price. Three diagnostics separate what the exit contributes
from what the KDJ entry contributes and expose the costs the win rate hides:

    1. Calendar control: uninformed entries every 21st bar (offsets 0/7/14),
       same K>=80 exit, same stages. If its edge is within 5pp of the strategy
       cells' edge, the edge belongs to the exit.
    2. Tail table: return quantiles, max adverse excursion (MAE, lowest low
       between entry and exit against the entry fill), hold quantiles, and the
       open-trade census the win-rate denominator excludes.
    3. Return baseline: mean trade return against the unconditional same-length
       mean, because an edge in win rate need not be an edge in return.

Usage:
    python -m factor_kdj_macd.diagnose_exit_attribution

Public functions:
    calendar_control(stage, spacing, offsets)   Uninformed-entry control
    tail_table(cell)                            Tails, MAE and open trades
    main()                                      All three, writes results/
"""

from __future__ import annotations

__all__ = ["calendar_control", "tail_table", "main", "TARGET_CELLS", "SPACING", "OFFSETS"]

import numpy as np
import pandas as pd

from . import benchmark as benchmark_module
from . import engine as engine_module
from . import exits as exits_module
from . import run_backtest as runner
from . import search as search_module

# The two cells that survived validation, prereg 5.1 screen + 5.2 criteria.
TARGET_CELLS = (
    ("b_50_80", "recent_cross", "k_over_80"),
    ("b_20_80", "both", "k_over_80"),
)

SPACING = 21
OFFSETS = (0, 7, 14)


def calendar_control(stage: str, spacing: int = SPACING, offsets: tuple[int, ...] = OFFSETS) -> dict:
    """Uninformed entries on a fixed calendar grid, K>=80 exit, one stage.

    Entries fire every `spacing` bars starting at warm-up plus each offset; the
    three offset schedules are pooled so the result does not hinge on one phase.
    One position at a time per symbol, exactly like the strategy cells.

    Args:
        stage: Key of search_module.STAGES.
        spacing: Bars between scheduled entry signals.
        offsets: Schedule phases, pooled.

    Returns:
        Summary dict from runner._summarize plus the label fields.
    """
    symbols, start, end = search_module.STAGES[stage]
    frames = []
    for offset in offsets:
        for symbol in symbols:
            frame = search_module._frame(symbol, end)
            first_bar = runner.WARMUP_BARS
            in_window = np.flatnonzero(frame["date"].to_numpy() >= np.datetime64(start))
            first_bar = max(first_bar, int(in_window[0])) if in_window.size else len(frame)

            entry = np.zeros(len(frame), dtype=bool)
            entry[first_bar + offset::spacing] = True
            exit_spec = exits_module.build_exit_spec("k_over_80", frame)
            trades = engine_module.extract_trades_spec(
                frame, entry, exit_spec, first_bar, fill="next_open",
                symbol=symbol, arm=f"calendar_{offset}|k_over_80",
            )
            opens = frame["open"].to_numpy()
            frames.append(benchmark_module.benchmark_trades(opens, trades, first_bar + 1, len(frame) - 1))
    pooled = pd.concat(frames, ignore_index=True)
    return runner._summarize(pooled, {"stage": stage, "band": "CALENDAR",
                                      "macd_mode": "none", "exit": "k_over_80"})


def tail_table(cell: tuple[str, str, str]) -> dict:
    """Return-tail, MAE, hold and open-trade census for one cell over V1+V2.

    MAE is the lowest low between the entry fill bar and the exit fill bar,
    against the entry fill price: the worst mark-to-market the position saw.

    Args:
        cell: (band, macd_mode, exit).

    Returns:
        Dict of quantiles and the open-trade census (count plus each open
        trade's unrealized return at the last close).
    """
    returns, maes, holds = [], [], []
    open_census = []
    for stage in ("V1", "V2"):
        symbols, _, end = search_module.STAGES[stage]
        for symbol in symbols:
            trades = search_module.run_cell_spec(symbol, cell, stage)
            frame = search_module._frame(symbol, end)
            lows = frame["low"].to_numpy()
            closes = frame["close"].to_numpy()
            for _, row in trades.iterrows():
                if not row["closed"]:
                    unrealized = closes[-1] / row["entry_price"] - 1.0
                    open_census.append({"symbol": symbol, "entry": str(pd.Timestamp(row["entry_date"]).date()),
                                        "unrealized_ret": round(float(unrealized), 4),
                                        "bars_held": int(row["hold_bars"])})
                    continue
                entry_bar, exit_bar = int(row["entry_bar"]), int(row["exit_bar"])
                mae = lows[entry_bar: exit_bar + 1].min() / row["entry_price"] - 1.0
                returns.append(float(row["ret"]))
                maes.append(float(mae))
                holds.append(int(row["hold_bars"]))

    returns_arr, maes_arr, holds_arr = map(np.array, (returns, maes, holds))
    quantile = lambda a, q: float(np.quantile(a, q))
    return {
        "cell": "|".join(cell), "n_closed": len(returns_arr), "n_open": len(open_census),
        "ret_min": quantile(returns_arr, 0), "ret_p1": quantile(returns_arr, 0.01),
        "ret_p5": quantile(returns_arr, 0.05), "ret_p25": quantile(returns_arr, 0.25),
        "ret_median": quantile(returns_arr, 0.50), "ret_max": quantile(returns_arr, 1.0),
        "mae_median": quantile(maes_arr, 0.50), "mae_p5": quantile(maes_arr, 0.05),
        "mae_min": quantile(maes_arr, 0),
        "hold_median": quantile(holds_arr, 0.50), "hold_p90": quantile(holds_arr, 0.90),
        "hold_max": int(holds_arr.max()),
        "open_trades": open_census,
    }


def _return_baseline(cell: tuple[str, str, str]) -> dict:
    """Mean trade return against the unconditional same-length mean, V1+V2.

    Baseline windows start inside the stage's own window (first_bar + 1, the
    same convention as the win-rate benchmark), never in the search era.
    """
    strategy, baseline = [], []
    for stage in ("V1", "V2"):
        symbols, start, end = search_module.STAGES[stage]
        for symbol in symbols:
            trades = search_module.run_cell_spec(symbol, cell, stage)
            closed = trades[trades["closed"]]
            if not len(closed):
                continue
            frame = search_module._frame(symbol, end)
            opens = frame["open"].to_numpy()
            last = len(frame) - 1
            first_bar = runner.WARMUP_BARS
            in_window = np.flatnonzero(frame["date"].to_numpy() >= np.datetime64(start))
            first_bar = max(first_bar, int(in_window[0])) if in_window.size else len(frame)
            for _, row in closed.iterrows():
                hold = int(row["hold_bars"])
                starts = np.arange(first_bar + 1, last - hold + 1)
                if starts.size == 0:
                    continue
                strategy.append(float(row["ret"]))
                baseline.append(float((opens[starts + hold] / opens[starts] - 1.0).mean()))
    return {"cell": "|".join(cell),
            "mean_ret": float(np.mean(strategy)),
            "matched_mean_baseline": float(np.mean(baseline)),
            "excess_mean_ret": float(np.mean(strategy) - np.mean(baseline))}


def main() -> int:
    """Run the calendar control, tail tables and return baselines; persist."""
    control_rows = [calendar_control(stage) for stage in ("S", "V1", "V2")]
    control = pd.DataFrame(control_rows)
    control.to_csv(runner.RESULTS_DIR / "diag_calendar_control.csv", index=False)
    print("=== calendar control: uninformed entry every 21 bars, K>=80 exit ===")
    print(control[["stage", "n_closed", "n_open", "win_rate", "base_rate", "edge_pp",
                   "p_value", "mean_ret", "median_ret", "mean_hold_bars"]]
          .to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    print("\n=== tails, MAE, open trades (V1+V2) ===")
    tails = []
    for cell in TARGET_CELLS:
        table = tail_table(cell)
        tails.append(table)
        open_trades = table.pop("open_trades")
        for key, value in table.items():
            print(f"  {key}: {value}")
        print(f"  open trades: {open_trades}")
        print()
    pd.DataFrame(tails).to_csv(runner.RESULTS_DIR / "diag_tails.csv", index=False)

    print("=== mean return vs matched-length unconditional mean (V1+V2) ===")
    baselines = [_return_baseline(cell) for cell in TARGET_CELLS]
    frame = pd.DataFrame(baselines)
    frame.to_csv(runner.RESULTS_DIR / "diag_return_baseline.csv", index=False)
    print(frame.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
