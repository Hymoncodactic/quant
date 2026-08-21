"""Trade extraction for a single symbol, long only, one position at a time.

Responsibility: walk the signal masks bar by bar, apply the execution timing,
and emit one row per trade. Costs are zero in this study by explicit instruction
(prereg section 2.6), so no cost model is applied anywhere in this file.
Not responsible for: signal rules, benchmarks, reporting.

Execution timing, prereg section 2.5:
    fill "next_open"   signal on the close of bar t, filled at the open of t+1
    fill "same_close"  signal on the close of bar t, filled at that same close

Public functions:
    extract_trades(frame, entry_signal, exit_signal, first_bar, fill)
    extract_trades_spec(frame, entry_signal, exit_spec, first_bar, fill)
    assert_causal_indicators(bars, check_indices, kdj_params, macd_params)
    FILL_MODES

extract_trades is the original mask-exit walker and is frozen: the exit-search
round (prereg 20260820_kdj_exit_search) added extract_trades_spec instead of
touching it, so the byte-identical reproduction of the first round's results
stays checkable.
"""

from __future__ import annotations

__all__ = ["extract_trades", "extract_trades_spec", "assert_causal_indicators",
           "FILL_MODES", "TRADE_COLUMNS"]

import numpy as np
import pandas as pd

FILL_MODES = ("next_open", "same_close")

TRADE_COLUMNS = (
    "symbol", "arm", "signal_date", "entry_date", "exit_signal_date", "exit_date",
    "entry_price", "exit_price", "ret", "hold_bars", "entry_bar", "exit_bar", "closed",
)


def extract_trades(
    frame: pd.DataFrame,
    entry_signal: np.ndarray,
    exit_signal: np.ndarray,
    first_bar: int,
    fill: str = "next_open",
    symbol: str = "",
    arm: str = "",
) -> pd.DataFrame:
    """Walk the signals and build the trade list.

    A position is opened on the first entry signal at or after first_bar, then
    held until the first exit signal strictly after the entry fill bar. Entry
    signals raised while a position is open are ignored, so the symbol never
    holds more than one position.

    Args:
        frame: Bar frame carrying date, open and close columns.
        entry_signal: Boolean mask, entry condition evaluated on each bar's close.
        exit_signal: Boolean mask, exit condition evaluated on each bar's close.
        first_bar: Index of the first bar eligible to raise a signal. Bars before
            it are warm-up or outside the study window.
        fill: One of FILL_MODES. "next_open" is the conservative default.
        symbol: Ticker, copied into the output for later concatenation.
        arm: Arm name, copied into the output.

    Returns:
        Frame with TRADE_COLUMNS, one row per trade, ascending by entry. ret is a
        simple return, exit_price / entry_price - 1, with no costs. hold_bars is
        exit_bar - entry_bar, in trading days. A trailing position with no exit
        signal before the end of the data is emitted with closed=False and NaN
        exit fields, and must be excluded from win-rate denominators.

    Raises:
        ValueError: fill is not in FILL_MODES, or the masks do not match frame.
        AssertionError: An entry or exit would be filled on a bar at or before
            its own signal bar under next_open timing.
    """
    if fill not in FILL_MODES:
        raise ValueError(f"fill must be one of {FILL_MODES}, got {fill!r}")
    if len(entry_signal) != len(frame) or len(exit_signal) != len(frame):
        raise ValueError("signal masks must match the frame length")

    dates = frame["date"].to_numpy()
    opens = frame["open"].to_numpy()
    closes = frame["close"].to_numpy()
    n = len(frame)
    # next_open needs bar t+1 to exist, so the last bar can never be a fill source.
    last_signal_bar = n - 2 if fill == "next_open" else n - 1
    offset = 1 if fill == "next_open" else 0
    prices = opens if fill == "next_open" else closes

    rows = []
    i = max(first_bar, 1)
    while i <= last_signal_bar:
        if not entry_signal[i]:
            i += 1
            continue

        signal_bar = i
        entry_bar = signal_bar + offset
        # Cutoff assertion, backtest-discipline section 1.1: under the conservative
        # timing the fill bar must be strictly later than the bar that produced the
        # signal. Checked per trade rather than by eye.
        if fill == "next_open":
            assert dates[entry_bar] > dates[signal_bar], (
                f"entry fill {dates[entry_bar]} is not after signal {dates[signal_bar]}"
            )

        exit_sig_bar = -1
        s = entry_bar
        while s <= last_signal_bar:
            if exit_signal[s]:
                exit_sig_bar = s
                break
            s += 1

        if exit_sig_bar < 0:
            rows.append({
                "symbol": symbol, "arm": arm,
                "signal_date": dates[signal_bar], "entry_date": dates[entry_bar],
                "exit_signal_date": pd.NaT, "exit_date": pd.NaT,
                "entry_price": prices[entry_bar], "exit_price": np.nan, "ret": np.nan,
                "hold_bars": n - 1 - entry_bar, "entry_bar": entry_bar, "exit_bar": -1,
                "closed": False,
            })
            break

        exit_bar = exit_sig_bar + offset
        if fill == "next_open":
            assert dates[exit_bar] > dates[exit_sig_bar], (
                f"exit fill {dates[exit_bar]} is not after signal {dates[exit_sig_bar]}"
            )
        assert exit_bar > entry_bar, "exit fill must be strictly after the entry fill"

        entry_price = prices[entry_bar]
        exit_price = prices[exit_bar]
        rows.append({
            "symbol": symbol, "arm": arm,
            "signal_date": dates[signal_bar], "entry_date": dates[entry_bar],
            "exit_signal_date": dates[exit_sig_bar], "exit_date": dates[exit_bar],
            "entry_price": entry_price, "exit_price": exit_price,
            "ret": exit_price / entry_price - 1.0,
            "hold_bars": exit_bar - entry_bar, "entry_bar": entry_bar,
            "exit_bar": exit_bar, "closed": True,
        })
        # The exit fill bar may itself raise a fresh entry signal, so scanning
        # resumes there rather than after it.
        i = exit_bar

    if not rows:
        return pd.DataFrame(columns=list(TRADE_COLUMNS))
    return pd.DataFrame(rows)[list(TRADE_COLUMNS)]


def extract_trades_spec(
    frame: pd.DataFrame,
    entry_signal: np.ndarray,
    exit_spec: dict,
    first_bar: int,
    fill: str = "next_open",
    symbol: str = "",
    arm: str = "",
) -> pd.DataFrame:
    """Walk the entry signals against a mask, trailing-stop or fixed-hold exit.

    Generalizes extract_trades to the three exit kinds of the exit-search round
    (exits.build_exit_spec). Position logic is unchanged: long only, one position
    at a time, entry signals during a holding period ignored, scanning resumes at
    the exit fill bar.

    Exit kinds:
        mask   {"kind": "mask", "mask": bool array}. First True at or after the
               entry fill bar closes the position; conservative timing fills at
               the next open, same_close fills on the trigger bar's close.
        trail  {"kind": "trail", "pct": float}. Triggers on the first bar whose
               close is at or below (1 - pct) times the highest close since the
               entry fill bar, that bar's close included. Same fill timing.
        fixed  {"kind": "fixed", "n": int}. Closes at the fill price exactly n
               bars after the entry fill bar, with no trigger bar; a horizon that
               overruns the data leaves the trade open.

    Args:
        frame: Bar frame carrying date, open and close columns.
        entry_signal: Boolean mask, entry condition evaluated on each bar's close.
        exit_spec: One of the three dicts above.
        first_bar: First bar eligible to raise an entry signal.
        fill: One of FILL_MODES.
        symbol: Ticker, copied into the output.
        arm: Arm label, copied into the output.

    Returns:
        Frame with TRADE_COLUMNS, same conventions as extract_trades. For fixed
        exits exit_signal_date equals exit_date because no separate trigger bar
        exists.

    Raises:
        ValueError: Bad fill mode, mask length mismatch, or unknown exit kind.
        AssertionError: A fill would not be strictly later than its trigger.
    """
    if fill not in FILL_MODES:
        raise ValueError(f"fill must be one of {FILL_MODES}, got {fill!r}")
    if len(entry_signal) != len(frame):
        raise ValueError("entry mask must match the frame length")
    kind = exit_spec.get("kind")
    if kind not in ("mask", "trail", "fixed"):
        raise ValueError(f"unknown exit kind {kind!r}")
    if kind == "mask" and len(exit_spec["mask"]) != len(frame):
        raise ValueError("exit mask must match the frame length")

    dates = frame["date"].to_numpy()
    opens = frame["open"].to_numpy()
    closes = frame["close"].to_numpy()
    n = len(frame)
    last_signal_bar = n - 2 if fill == "next_open" else n - 1
    offset = 1 if fill == "next_open" else 0
    prices = opens if fill == "next_open" else closes

    def _find_trigger(entry_bar: int) -> int:
        """Return the exit trigger bar at or after entry_bar, or -1 if none."""
        if kind == "mask":
            mask = exit_spec["mask"]
            s = entry_bar
            while s <= last_signal_bar:
                if mask[s]:
                    return s
                s += 1
            return -1
        pct = exit_spec["pct"]
        peak = closes[entry_bar]
        s = entry_bar
        while s <= last_signal_bar:
            if closes[s] > peak:
                peak = closes[s]
            if closes[s] <= peak * (1.0 - pct):
                return s
            s += 1
        return -1

    rows = []
    i = max(first_bar, 1)
    while i <= last_signal_bar:
        if not entry_signal[i]:
            i += 1
            continue

        signal_bar = i
        entry_bar = signal_bar + offset
        if fill == "next_open":
            assert dates[entry_bar] > dates[signal_bar], (
                f"entry fill {dates[entry_bar]} is not after signal {dates[signal_bar]}"
            )

        if kind == "fixed":
            exit_bar = entry_bar + exit_spec["n"]
            trigger_bar = exit_bar
            open_ended = exit_bar > n - 1
        else:
            trigger_bar = _find_trigger(entry_bar)
            open_ended = trigger_bar < 0
            exit_bar = trigger_bar + offset if not open_ended else -1

        if open_ended:
            rows.append({
                "symbol": symbol, "arm": arm,
                "signal_date": dates[signal_bar], "entry_date": dates[entry_bar],
                "exit_signal_date": pd.NaT, "exit_date": pd.NaT,
                "entry_price": prices[entry_bar], "exit_price": np.nan, "ret": np.nan,
                "hold_bars": n - 1 - entry_bar, "entry_bar": entry_bar, "exit_bar": -1,
                "closed": False,
            })
            break

        if kind != "fixed" and fill == "next_open":
            assert dates[exit_bar] > dates[trigger_bar], (
                f"exit fill {dates[exit_bar]} is not after trigger {dates[trigger_bar]}"
            )
        assert exit_bar > entry_bar, "exit fill must be strictly after the entry fill"

        entry_price = prices[entry_bar]
        exit_price = prices[exit_bar]
        rows.append({
            "symbol": symbol, "arm": arm,
            "signal_date": dates[signal_bar], "entry_date": dates[entry_bar],
            "exit_signal_date": dates[trigger_bar] if kind != "fixed" else dates[exit_bar],
            "exit_date": dates[exit_bar],
            "entry_price": entry_price, "exit_price": exit_price,
            "ret": exit_price / entry_price - 1.0,
            "hold_bars": exit_bar - entry_bar, "entry_bar": entry_bar,
            "exit_bar": exit_bar, "closed": True,
        })
        i = exit_bar

    if not rows:
        return pd.DataFrame(columns=list(TRADE_COLUMNS))
    return pd.DataFrame(rows)[list(TRADE_COLUMNS)]


def assert_causal_indicators(
    bars: pd.DataFrame,
    check_indices: list[int],
    kdj_params: tuple[int, int, int] = (9, 3, 3),
    macd_params: tuple[int, int, int] = (12, 26, 9),
) -> int:
    """Prove that indicator values at bar t use no data after bar t.

    Recomputes the indicators on the prefix bars[:t+1] and requires the values at
    t to be bit-for-bit identical to the values obtained from the full series.
    Every recursion here is causal, so exact equality is the correct expectation;
    any tolerance would let a genuine leak pass.

    Args:
        bars: OHLC frame, ascending by date.
        check_indices: Bar indices to verify, typically every trade's signal bar.
        kdj_params: (n, m1, m2).
        macd_params: (fast, slow, signal).

    Returns:
        Number of indices checked.

    Raises:
        AssertionError: A prefix recomputation differs from the full-series value.
    """
    from . import indicators as indicators_module

    full, _ = indicators_module.add_indicators(bars, kdj_params, macd_params)
    columns = ["k", "d", "j", "dif", "dea", "hist"]
    checked = 0
    for t in check_indices:
        prefix, _ = indicators_module.add_indicators(bars.iloc[: t + 1], kdj_params, macd_params)
        for column in columns:
            expected = full[column].to_numpy()[t]
            actual = prefix[column].to_numpy()[t]
            assert expected == actual, (
                f"{column} at bar {t} changes when later bars are removed: "
                f"{expected!r} with full data, {actual!r} on the prefix"
            )
        checked += 1
    return checked
