"""Runner for the KDJ + MACD daily study. Writes results/ and prints a summary.

Responsibility: wire loading, indicators, rules, engine and benchmark together
for every (window, arm, symbol) cell, then persist trades and summaries.
Not responsible for: rule definitions, statistics, judgement.

All parameters below are frozen in
research/prereg/20260820_kdj_macd_daily_prereg.md and must not be tuned against
the output. Costs are zero throughout, by explicit instruction (prereg 2.6).

Usage:
    python -m factor_kdj_macd.run_backtest            run every arm and window
    python -m factor_kdj_macd.run_backtest --arms A-MAIN --windows W1

Public functions:
    run_cell(symbol, arm, window)     One symbol, one arm, one window
    run_arm(arm, window)              All symbols for one arm and window
    main(argv)                        Entry point
"""

from __future__ import annotations

__all__ = ["run_cell", "run_arm", "main", "SYMBOLS", "WINDOWS", "ARM_SPECS", "WARMUP_BARS"]

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from . import benchmark as benchmark_module
from . import data as data_module
from . import engine as engine_module
from . import indicators as indicators_module
from . import rules as rules_module

RESULTS_DIR = Path(__file__).resolve().parent / "results"

SYMBOLS = ("NVDA", "AAPL", "MSFT", "META", "AMD")

# Window name -> (inclusive first signal date or None for the full history,
# inclusive last date). Both windows are reported; picking the better one after
# the fact is forbidden by prereg section 2.7.
WINDOWS: dict[str, tuple[str | None, str]] = {
    "W1": ("2016-01-01", "2026-08-19"),
    "W2": (None, "2026-08-19"),
}

# Bars consumed by the indicator recursions before any signal is allowed.
# Justification for 200 is in prereg section 2.2.
WARMUP_BARS = 200

KDJ_PARAMS = (9, 3, 3)
MACD_PARAMS = (12, 26, 9)

# Arm name -> (rule keyword arguments, shift indicators one bar earlier, fill mode).
#
# A-MAIN is the pre-registered rule. It turns out to be empty on this universe,
# so the look-ahead control and the close-fill sensitivity are also run on the
# two decomposition arms; without that the discriminating-power requirement of
# backtest-discipline section 1.2 could not be met at all. Logged as an addition
# in prereg section 6.
_MAIN = {"macd_mode": "both", "k_max": rules_module.K_MAX}
_NOMACD = {"macd_mode": "none", "k_max": rules_module.K_MAX}
_NOK30 = {"macd_mode": "both", "k_max": None}

ARM_SPECS: dict[str, tuple[dict, bool, str]] = {
    "A-MAIN": (_MAIN, False, "next_open"),
    "A-LOOKAHEAD": (_MAIN, True, "next_open"),
    "A-CLOSE-FILL": (_MAIN, False, "same_close"),
    "A-NOMACD": (_NOMACD, False, "next_open"),
    "A-NOMACD-LOOKAHEAD": (_NOMACD, True, "next_open"),
    "A-NOMACD-CLOSE-FILL": (_NOMACD, False, "same_close"),
    "A-NOK30": (_NOK30, False, "next_open"),
    "A-NOK30-LOOKAHEAD": (_NOK30, True, "next_open"),
    "A-NOK30-CLOSE-FILL": (_NOK30, False, "same_close"),
}


def run_spec(
    symbol: str,
    window: str,
    rule_kwargs: dict,
    shift: bool = False,
    fill: str = "next_open",
    arm: str = "",
) -> tuple[pd.DataFrame, dict]:
    """Run one symbol under an explicit rule specification.

    Indicators are always computed on the symbol's full available history and the
    window is applied afterwards, so a window starting in 2016 still gets fully
    converged recursions rather than a truncated warm-up.

    Args:
        symbol: Ticker present under data/t212/curated/us_equity.
        window: Key of WINDOWS.
        rule_kwargs: Keyword arguments for rules.entry_exit.
        shift: Move indicators one bar earlier, building the look-ahead control.
        fill: One of engine.FILL_MODES.
        arm: Label copied into the trade rows.

    Returns:
        Tuple (trades, diagnostics). trades carries engine.TRADE_COLUMNS plus the
        benchmark columns. diagnostics records the bar counts, the date range
        actually used and the KDJ flat-range counter.

    Raises:
        KeyError: Unknown window.
    """
    start, end = WINDOWS[window]

    bars = data_module.load_daily(symbol, start=None, end=end)
    frame, flat_count = indicators_module.add_indicators(bars, KDJ_PARAMS, MACD_PARAMS)

    signal_frame = rules_module.shift_forward(frame) if shift else frame
    entry_signal, exit_signal = rules_module.entry_exit(signal_frame, **rule_kwargs)

    first_bar = WARMUP_BARS
    if start is not None:
        in_window = np.flatnonzero(frame["date"].to_numpy() >= np.datetime64(start))
        if in_window.size == 0:
            first_bar = len(frame)
        else:
            first_bar = max(first_bar, int(in_window[0]))

    trades = engine_module.extract_trades(
        frame, entry_signal, exit_signal, first_bar, fill=fill, symbol=symbol, arm=arm
    )

    offset = 1 if fill == "next_open" else 0
    prices = frame["open"].to_numpy() if fill == "next_open" else frame["close"].to_numpy()
    trades = benchmark_module.benchmark_trades(
        prices, trades, first_bar + offset, len(frame) - 1
    )

    diagnostics = {
        "symbol": symbol, "arm": arm, "window": window,
        "bars_total": len(frame), "first_signal_bar": first_bar,
        "eligible_bars": max(0, len(frame) - first_bar),
        "first_date": str(frame["date"].iloc[0].date()) if len(frame) else None,
        "window_first_date": str(frame["date"].iloc[first_bar].date()) if first_bar < len(frame) else None,
        "last_date": str(frame["date"].iloc[-1].date()) if len(frame) else None,
        "kdj_flat_range_bars": flat_count,
    }
    return trades, diagnostics


def run_cell(symbol: str, arm: str, window: str) -> tuple[pd.DataFrame, dict]:
    """Run one symbol under one named arm and one window.

    Args:
        symbol: Ticker present under data/t212/curated/us_equity.
        arm: Key of ARM_SPECS.
        window: Key of WINDOWS.

    Returns:
        Same as run_spec.

    Raises:
        KeyError: Unknown arm or window.
    """
    rule_kwargs, shift, fill = ARM_SPECS[arm]
    return run_spec(symbol, window, rule_kwargs, shift=shift, fill=fill, arm=arm)


def _summarize(trades: pd.DataFrame, label: dict) -> dict:
    """Reduce a trade frame to the reported statistics.

    Args:
        trades: Trade frame carrying ret, closed and base_rate.
        label: Fields copied verbatim into the result, such as arm and window.

    Returns:
        Dict with trade counts, win rate, matched-horizon baseline, the edge in
        percentage points and the Poisson-binomial test output. Win rate is NaN
        when there are no closed trades.
    """
    closed = trades[trades["closed"]] if len(trades) else trades
    result = dict(label)
    result["n_closed"] = int(len(closed))
    # astype(bool) first: concatenating an empty trade frame coerces the closed
    # column to object dtype, and ~ on Python bools yields -2/-1, not negation.
    result["n_open"] = int(len(trades) - trades["closed"].astype(bool).sum()) if len(trades) else 0

    if result["n_closed"] == 0:
        result.update({"win_rate": float("nan"), "base_rate": float("nan"),
                       "edge_pp": float("nan"), "z": float("nan"), "p_value": float("nan"),
                       "mean_ret": float("nan"), "median_ret": float("nan"),
                       "mean_hold_bars": float("nan"), "median_hold_bars": float("nan"),
                       "profit_factor": float("nan")})
        return result

    wins_mask = closed["ret"] > 0.0
    wins = int(wins_mask.sum())
    test = benchmark_module.poisson_binomial_test(wins, closed["base_rate"].to_numpy())
    gains = closed.loc[wins_mask, "ret"].sum()
    losses = -closed.loc[~wins_mask, "ret"].sum()

    result.update({
        "win_rate": wins / result["n_closed"],
        "base_rate": float(np.nanmean(closed["base_rate"].to_numpy())),
        "z": test["z"], "p_value": test["p_value"],
        "expected_wins": test["expected_wins"],
        "wins": wins,
        "mean_ret": float(closed["ret"].mean()),
        "median_ret": float(closed["ret"].median()),
        "mean_hold_bars": float(closed["hold_bars"].mean()),
        "median_hold_bars": float(closed["hold_bars"].median()),
        "profit_factor": float(gains / losses) if losses > 0 else float("inf"),
    })
    result["edge_pp"] = 100.0 * (result["win_rate"] - result["base_rate"])
    return result


def run_arm(arm: str, window: str) -> tuple[pd.DataFrame, list[dict], list[dict]]:
    """Run every symbol for one arm and window.

    Args:
        arm: Key of ARM_SPECS.
        window: Key of WINDOWS.

    Returns:
        Tuple (trades, per_symbol_summaries, diagnostics).
    """
    frames, per_symbol, diagnostics = [], [], []
    for symbol in SYMBOLS:
        trades, diagnostic = run_cell(symbol, arm, window)
        frames.append(trades)
        diagnostics.append(diagnostic)
        per_symbol.append(_summarize(trades, {"arm": arm, "window": window, "symbol": symbol}))
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return combined, per_symbol, diagnostics


def main(argv: list[str] | None = None) -> int:
    """Run the requested arms and windows and persist the results.

    Args:
        argv: Command-line arguments, defaults to sys.argv[1:].

    Returns:
        Process exit code, 0 on success.
    """
    parser = argparse.ArgumentParser(description="KDJ entry + MACD filter, US tech daily, long only")
    parser.add_argument("--arms", nargs="*", default=list(ARM_SPECS), choices=list(ARM_SPECS))
    parser.add_argument("--windows", nargs="*", default=list(WINDOWS), choices=list(WINDOWS))
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    overall, per_symbol_rows, diagnostic_rows = [], [], []

    for window in args.windows:
        for arm in args.arms:
            trades, per_symbol, diagnostics = run_arm(arm, window)
            trades.to_csv(RESULTS_DIR / f"trades_{arm}_{window}.csv", index=False)
            per_symbol_rows.extend(per_symbol)
            diagnostic_rows.extend(diagnostics)
            row = _summarize(trades, {"arm": arm, "window": window, "symbol": "ALL"})
            # Sign test across symbols: how many of the five beat their own
            # matched-horizon baseline. Guards against one symbol carrying the
            # pooled number, which the pooled p-value cannot show.
            edges = [s["edge_pp"] for s in per_symbol if s["n_closed"] > 0]
            row["symbols_with_positive_edge"] = int(sum(1 for e in edges if e > 0))
            row["symbols_scored"] = len(edges)
            overall.append(row)

    summary = pd.DataFrame(overall)
    per_symbol_frame = pd.DataFrame(per_symbol_rows)
    summary.to_csv(RESULTS_DIR / "summary_overall.csv", index=False)
    per_symbol_frame.to_csv(RESULTS_DIR / "summary_per_symbol.csv", index=False)
    (RESULTS_DIR / "diagnostics.json").write_text(json.dumps(diagnostic_rows, indent=2))

    columns = ["window", "arm", "n_closed", "n_open", "win_rate", "base_rate", "edge_pp",
               "z", "p_value", "mean_ret", "median_ret", "mean_hold_bars",
               "symbols_with_positive_edge", "symbols_scored"]
    print(summary[columns].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
