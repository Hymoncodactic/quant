"""Executable acceptance checks for the KDJ + MACD study.

Every check is built so that a plausible implementation error makes it fail;
backtest-discipline section 6 forbids acceptance samples that would pass either
way, and the discriminating construction is stated in each check's docstring.

Usage:
    python -m factor_kdj_macd.acceptance

Exit code 0 when every check passes, 1 otherwise.

Checks:
    check_kdj_against_naive          KDJ vs an independent loop implementation
    check_macd_against_naive         EMA recursion vs an independent loop
    check_crossover_edges            Crossovers at exact equality
    check_fill_timing                next_open fills on the following bar's open
    check_prefix_technique_bites     Prefix recomputation catches a non-causal series
    check_indicators_are_causal      Real trades' signal bars survive prefix recompute
    check_lookahead_arm_beats_main   The deliberate look-ahead control does better
    check_horizon_win_rate           Benchmark on a hand-checkable series
    check_flat_return_counts_as_loss Zero return is not a win
    check_trades_never_overlap       One position at a time, fills strictly ordered
    check_no_signal_before_warmup    Warm-up bars raise no signals
    check_spec_matches_legacy        Spec walker reproduces extract_trades exactly
    check_trail_exit                 Trailing exit triggers on the right bar, causally
    check_fixed_exit                 Fixed-hold exit fills exactly n bars later
    check_k_band_entry               Band bounds are inclusive-low, exclusive-high
    check_search_window_sealed       Search-period loads carry no post-2017 bar
"""

from __future__ import annotations

__all__ = ["main", "CHECKS"]

import numpy as np
import pandas as pd

from . import benchmark as benchmark_module
from . import data as data_module
from . import engine as engine_module
from . import indicators as indicators_module
from . import exits as exits_module
from . import rules as rules_module
from . import run_backtest as runner


def _synthetic_bars(n: int = 400, seed: int = 7) -> pd.DataFrame:
    """Build a reproducible OHLC frame with a genuine intraday range.

    The range matters: a fixture where open equals close cannot distinguish a
    next-open fill from a same-close fill.
    """
    generator = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(generator.normal(0.0005, 0.02, n)))
    spread = np.abs(generator.normal(0.0, 0.01, n)) + 0.005
    return pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n, freq="B"),
        "open": close * (1.0 + generator.normal(0.0, 0.006, n)),
        "high": close * (1.0 + spread),
        "low": close * (1.0 - spread),
        "close": close,
        "volume": np.full(n, 1e6),
    })


def check_kdj_against_naive() -> str:
    """KDJ must match a separately written loop implementation.

    The tolerance is 1e-12 relative rather than exact equality because the two
    implementations use algebraically equal but differently associated arithmetic
    (alpha*rsv against rsv/m1); the observed gap is one unit in the last place.
    Bit-for-bit equality is required only where the same code path is rerun, as
    in engine.assert_causal_indicators.

    Discriminating: the same comparison is repeated with m1 changed from 3 to 4,
    which moves K by several points, so a tolerance this tight cannot mask a real
    difference in the recursion.
    """
    bars = _synthetic_bars()
    high, low, close = bars["high"].to_numpy(), bars["low"].to_numpy(), bars["close"].to_numpy()

    def naive(n: int, m1: int, m2: int) -> np.ndarray:
        k_out = np.empty(len(close))
        k_prev = d_prev = 50.0
        for i in range(len(close)):
            lo = low[max(0, i - n + 1): i + 1].min()
            hi = high[max(0, i - n + 1): i + 1].max()
            rsv = 50.0 if hi <= lo else (close[i] - lo) / (hi - lo) * 100.0
            k_prev = (1.0 - 1.0 / m1) * k_prev + rsv / m1
            d_prev = (1.0 - 1.0 / m2) * d_prev + k_prev / m2
            k_out[i] = k_prev
        return k_out

    k, _, _, _ = indicators_module.kdj(high, low, close, 9, 3, 3)
    assert np.allclose(k, naive(9, 3, 3), rtol=1e-12, atol=0), (
        "KDJ K differs from the independent implementation by more than float association"
    )
    assert np.abs(k - naive(9, 4, 3)).max() > 1.0, (
        "m1 barely moves K, so the tolerance would mask a real error"
    )
    return "KDJ matches an independent loop to 1e-12 relative; m1=4 moves K by more than 1 point"


def check_macd_against_naive() -> str:
    """DIF and DEA must match an explicit EMA loop seeded at the first observation.

    Discriminating: pandas ewm with adjust=True is also computed and must differ,
    so the check pins down the seeding convention rather than just the span.
    """
    bars = _synthetic_bars()
    close = bars["close"].to_numpy()

    def naive_ema(series: np.ndarray, span: int) -> np.ndarray:
        alpha = 2.0 / (span + 1.0)
        out = np.empty(len(series))
        value = series[0]
        for i, x in enumerate(series):
            value = x if i == 0 else alpha * x + (1.0 - alpha) * value
            out[i] = value
        return out

    dif, dea, hist = indicators_module.macd(close, 12, 26, 9)
    expected_dif = naive_ema(close, 12) - naive_ema(close, 26)
    assert np.allclose(dif, expected_dif, rtol=0, atol=1e-12), "DIF differs from the explicit EMA loop"
    assert np.allclose(dea, naive_ema(expected_dif, 9), rtol=0, atol=1e-12), "DEA differs"
    assert np.allclose(hist, 2.0 * (dif - dea)), "histogram is not 2*(DIF-DEA)"
    adjusted = (pd.Series(close).ewm(span=12, adjust=True).mean()
                - pd.Series(close).ewm(span=26, adjust=True).mean()).to_numpy()
    assert not np.allclose(dif, adjusted), "adjust=True gives the same answer, seeding is untested"
    return "DIF/DEA match an explicit seeded EMA loop and differ from the adjust=True variant"


def check_crossover_edges() -> str:
    """Equality bars must not create or duplicate a crossover.

    Fixture: K/D of (1,2), (3,3), (4,3), (2,3). A rising cross may only fire at
    index 2 and a falling cross only at index 3. An implementation using strict
    ">" on the previous bar would fire at index 1 as well.
    """
    fast = np.array([1.0, 3.0, 4.0, 2.0])
    slow = np.array([2.0, 3.0, 3.0, 3.0])
    up = rules_module.cross_up(fast, slow)
    down = rules_module.cross_down(fast, slow)
    assert up.tolist() == [False, False, True, False], f"cross_up fired at {np.flatnonzero(up).tolist()}"
    assert down.tolist() == [False, False, False, True], f"cross_down fired at {np.flatnonzero(down).tolist()}"
    return "crossovers fire once each and treat equality as not-yet-crossed"


def check_fill_timing() -> str:
    """A signal on bar t must fill at the open of bar t+1, never at bar t's close.

    Discriminating: the fixture's open on the fill bar is asserted to differ from
    the close on the signal bar, so a same-bar fill would produce a different
    price and fail.
    """
    bars = _synthetic_bars(60)
    entry = np.zeros(len(bars), dtype=bool)
    exit_signal = np.zeros(len(bars), dtype=bool)
    entry[10] = True
    exit_signal[20] = True

    trades = engine_module.extract_trades(bars, entry, exit_signal, first_bar=5, fill="next_open")
    assert len(trades) == 1, f"expected one trade, got {len(trades)}"
    row = trades.iloc[0]
    assert bars["open"].iloc[11] != bars["close"].iloc[10], "fixture cannot separate the two fill modes"
    assert row["entry_date"] == bars["date"].iloc[11], "entry did not fill on the next bar"
    assert row["entry_price"] == bars["open"].iloc[11], "entry did not fill at the next open"
    assert row["exit_date"] == bars["date"].iloc[21], "exit did not fill on the next bar"
    assert row["exit_price"] == bars["open"].iloc[21], "exit did not fill at the next open"
    assert row["hold_bars"] == 10, f"hold_bars should be 10, got {row['hold_bars']}"

    closed = engine_module.extract_trades(bars, entry, exit_signal, first_bar=5, fill="same_close")
    assert closed.iloc[0]["entry_price"] == bars["close"].iloc[10], "same_close did not fill at the signal close"
    return "next_open fills at t+1 open, same_close fills at t close, on a fixture that separates them"


def check_prefix_technique_bites() -> str:
    """The prefix-recomputation technique must reject a deliberately non-causal series.

    A centred rolling mean uses future bars by construction. Recomputing it on a
    prefix therefore changes the last value. If this check ever passed, the
    causality check applied to the real indicators would prove nothing.
    """
    bars = _synthetic_bars(200)
    full = bars["close"].rolling(9, center=True, min_periods=1).mean().to_numpy()
    index = 150
    prefix = bars["close"].iloc[: index + 1].rolling(9, center=True, min_periods=1).mean().to_numpy()
    assert full[index] != prefix[index], "the prefix technique failed to detect a centred window"
    return "prefix recomputation detects a known forward-looking series"


def check_indicators_are_causal(sample: int = 40) -> str:
    """Every sampled signal bar's indicators must be unchanged by deleting later bars.

    Exact equality is required, not a tolerance: the recursions are causal, so any
    difference at all is a leak.
    """
    trades = pd.read_csv(runner.RESULTS_DIR / "trades_A-NOK30_W1.csv")
    checked = 0
    for symbol, group in trades.groupby("symbol"):
        bars = data_module.load_daily(symbol, end=runner.WINDOWS["W1"][1])
        dates = bars["date"].to_numpy()
        signal_dates = pd.to_datetime(group["signal_date"]).to_numpy()
        indices = [int(np.flatnonzero(dates == day)[0]) for day in signal_dates]
        step = max(1, len(indices) // max(1, sample // len(trades["symbol"].unique())))
        checked += engine_module.assert_causal_indicators(bars, indices[::step])
    return f"{checked} signal bars survive prefix recomputation bit for bit"


def check_lookahead_arm_beats_main() -> str:
    """The deliberate look-ahead control must clearly beat the clean arm.

    backtest-discipline section 1.2(b): if shifting the indicators one bar earlier
    barely changes the result, the look-ahead check has no discriminating power
    and cannot certify the clean arm.
    """
    summary = pd.read_csv(runner.RESULTS_DIR / "summary_overall.csv")
    rows = summary[summary["symbol"] == "ALL"] if "symbol" in summary else summary
    for window in runner.WINDOWS:
        for base, peeking in (("A-NOMACD", "A-NOMACD-LOOKAHEAD"), ("A-NOK30", "A-NOK30-LOOKAHEAD")):
            clean = rows[(rows["window"] == window) & (rows["arm"] == base)]["win_rate"].iloc[0]
            leaked = rows[(rows["window"] == window) & (rows["arm"] == peeking)]["win_rate"].iloc[0]
            assert leaked - clean > 0.10, (
                f"{window} {peeking} win rate {leaked:.4f} is not clearly above {base} {clean:.4f}; "
                "the look-ahead check has no discriminating power"
            )
    return "look-ahead arms beat their clean counterparts by more than 10 points in every window"


def check_horizon_win_rate() -> str:
    """The matched-horizon benchmark must count windows the way the docstring says.

    Fixture prices [1, 2, 3, 2, 1] with hold_bars=2 give windows 1->3, 2->2, 3->1,
    so exactly one of three is a win and the flat window counts as a loss.
    """
    prices = np.array([1.0, 2.0, 3.0, 2.0, 1.0])
    rate, count = benchmark_module.horizon_win_rate(prices, 0, 4, 2)
    assert count == 3, f"expected 3 windows, got {count}"
    assert abs(rate - 1.0 / 3.0) < 1e-12, f"expected 1/3, got {rate}"
    return "matched-horizon benchmark counts overlapping windows and treats a flat window as a loss"


def check_flat_return_counts_as_loss() -> str:
    """A trade that returns exactly zero must not be counted as a win."""
    bars = _synthetic_bars(60)
    bars.loc[21, "open"] = bars.loc[11, "open"]
    entry = np.zeros(len(bars), dtype=bool)
    exit_signal = np.zeros(len(bars), dtype=bool)
    entry[10] = True
    exit_signal[20] = True
    trades = engine_module.extract_trades(bars, entry, exit_signal, first_bar=5, fill="next_open")
    trades["base_rate"] = 0.5
    trades["closed"] = True
    summary = runner._summarize(trades, {})
    assert trades.iloc[0]["ret"] == 0.0, "fixture did not produce a flat trade"
    assert summary["win_rate"] == 0.0, f"a flat trade was scored as a win, win_rate={summary['win_rate']}"
    return "a zero return is scored as a loss"


def check_trades_never_overlap() -> str:
    """Within one symbol, each entry fill must come strictly after the previous exit fill."""
    violations = 0
    for arm in ("A-MAIN", "A-NOMACD", "A-NOK30"):
        for window in runner.WINDOWS:
            trades = pd.read_csv(runner.RESULTS_DIR / f"trades_{arm}_{window}.csv")
            if trades.empty:
                continue
            for _, group in trades.groupby("symbol"):
                group = group.sort_values("entry_bar")
                previous_exit = group["exit_bar"].shift(1)
                overlap = group["entry_bar"] <= previous_exit
                violations += int(overlap.sum())
    assert violations == 0, f"{violations} trades opened at or before the previous exit fill"
    return "no symbol ever holds two positions at once"


def check_no_signal_before_warmup() -> str:
    """No trade may be entered on a bar inside the warm-up window."""
    for arm in ("A-NOMACD", "A-NOK30"):
        trades = pd.read_csv(runner.RESULTS_DIR / f"trades_{arm}_W2.csv")
        if trades.empty:
            continue
        earliest = int(trades["entry_bar"].min())
        assert earliest > runner.WARMUP_BARS, (
            f"{arm} entered at bar {earliest}, inside the {runner.WARMUP_BARS}-bar warm-up"
        )
    return f"no entry occurs at or before bar {runner.WARMUP_BARS}"


def check_spec_matches_legacy() -> str:
    """extract_trades_spec with a death-cross mask must reproduce extract_trades.

    Run on real NVDA data over the W1 window so the comparison covers hundreds of
    signal bars, not a toy fixture. Trade-for-trade equality is required.
    """
    bars = data_module.load_daily("NVDA", end=runner.WINDOWS["W1"][1])
    frame, _ = indicators_module.add_indicators(bars, runner.KDJ_PARAMS, runner.MACD_PARAMS)
    entry, exit_mask = rules_module.entry_exit(frame, macd_mode="none", k_max=None)
    legacy = engine_module.extract_trades(frame, entry, exit_mask, 200, fill="next_open")
    spec = exits_module.build_exit_spec("death_cross", frame)
    generalized = engine_module.extract_trades_spec(frame, entry, spec, 200, fill="next_open")
    assert len(legacy) == len(generalized), f"{len(legacy)} vs {len(generalized)} trades"
    for column in ("entry_bar", "exit_bar", "entry_price", "exit_price", "ret"):
        assert legacy[column].equals(generalized[column]), f"column {column} differs"
    return f"spec walker reproduces extract_trades on {len(legacy)} real NVDA trades"


def check_trail_exit() -> str:
    """The trailing exit must trigger on the first close at or below peak*(1-pct).

    Fixture closes after entry: 100, 110, 121, 116, 108.8, ... with a 10 percent
    trail. 116 is a 4.1 percent drawdown from the 121 peak (no trigger); 108.8 is
    a 10.08 percent drawdown (trigger). Discriminating in two ways: a peak that
    wrongly included future bars (the whole series' maximum, 130 planted later)
    would trigger earlier, and a strictly-below comparison would pass 108.9 which
    sits exactly at the boundary in a second fixture.
    """
    closes = [100.0, 100.0, 110.0, 121.0, 116.0, 108.8, 90.0, 130.0, 129.0, 128.0]
    n = len(closes)
    bars = pd.DataFrame({
        "date": pd.date_range("2021-01-01", periods=n, freq="B"),
        "open": [c - 0.5 for c in closes], "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes], "close": closes, "volume": [1e6] * n,
    })
    # Bar 0 can never signal (a crossover needs a previous bar), so the fixture
    # signals on bar 1 and fills on bar 2, whose close of 110 starts the peak.
    entry = np.zeros(n, dtype=bool)
    entry[1] = True
    trades = engine_module.extract_trades_spec(
        bars, entry, {"kind": "trail", "pct": 0.10}, first_bar=1, fill="next_open"
    )
    assert len(trades) >= 1, "no trade produced"
    row = trades.iloc[0]
    assert row["exit_signal_date"] == bars["date"].iloc[5], (
        f"trigger at {row['exit_signal_date']}, expected bar 5; a future-peak "
        "implementation (130 at bar 7) would have triggered at bar 4 already"
    )
    assert row["exit_date"] == bars["date"].iloc[6], "fill is not the next open"

    boundary = closes.copy()
    boundary[5] = 121.0 * 0.9
    bars2 = bars.copy()
    bars2["close"] = boundary
    trades2 = engine_module.extract_trades_spec(
        bars2, entry, {"kind": "trail", "pct": 0.10}, first_bar=1, fill="next_open"
    )
    assert trades2.iloc[0]["exit_signal_date"] == bars2["date"].iloc[5], (
        "a close exactly at peak*(1-pct) must trigger (condition is <=)"
    )
    return "trail triggers on the first at-or-below-threshold close using only the past peak"


def check_fixed_exit() -> str:
    """A fixed n-bar exit must fill exactly n bars after the entry fill.

    Also checks the overrun case: a horizon crossing the end of data leaves the
    trade open rather than inventing a fill.
    """
    bars = _synthetic_bars(30)
    entry = np.zeros(30, dtype=bool)
    entry[10] = True
    trades = engine_module.extract_trades_spec(
        bars, entry, {"kind": "fixed", "n": 5}, first_bar=0, fill="next_open"
    )
    row = trades.iloc[0]
    assert row["entry_date"] == bars["date"].iloc[11]
    assert row["exit_date"] == bars["date"].iloc[16], "exit is not entry fill + 5 bars"
    assert row["hold_bars"] == 5
    assert row["exit_price"] == bars["open"].iloc[16]

    entry2 = np.zeros(30, dtype=bool)
    entry2[26] = True
    trades2 = engine_module.extract_trades_spec(
        bars, entry2, {"kind": "fixed", "n": 5}, first_bar=0, fill="next_open"
    )
    assert len(trades2) == 1 and not trades2.iloc[0]["closed"], (
        "an overrunning fixed horizon must stay open, not fabricate a fill"
    )
    return "fixed exit fills exactly n bars later; overruns stay open"


def check_k_band_entry() -> str:
    """The K band must be inclusive at the low bound and exclusive at the high bound.

    Fixture: three crossover bars with K exactly 20, 50 and 35 under a (20, 50)
    band. 20 must pass, 50 must be rejected, 35 must pass. An implementation with
    either bound flipped fails on the exact-boundary bars.
    """
    frame = pd.DataFrame({
        "k": [10.0, 20.0, 10.0, 50.0, 10.0, 35.0],
        "d": [15.0, 15.0, 15.0, 15.0, 15.0, 15.0],
        "dif": [1.0] * 6, "dea": [0.5] * 6, "hist": [1.0] * 6,
    })
    entry, _ = rules_module.entry_exit(frame, macd_mode="none", k_max=50.0, k_min=20.0)
    assert entry.tolist() == [False, True, False, False, False, True], (
        f"band gating wrong: {entry.tolist()}, K=20 must pass, K=50 must not"
    )
    return "K band is inclusive-low, exclusive-high, verified on exact boundary values"


def check_search_window_sealed() -> str:
    """A search-period load must contain nothing after 2017-12-31.

    The two-stage design collapses if validation-era bars leak into the search
    stage through the loader, so the seal is checked on data, not on intent.
    """
    bars = data_module.load_daily("NVDA", end="2017-12-31")
    last = bars["date"].max()
    assert last <= pd.Timestamp("2017-12-31"), f"search frame reaches {last}"
    assert last >= pd.Timestamp("2017-12-20"), f"search frame ends suspiciously early at {last}"
    return f"search-period frame ends {last.date()}, no validation-era bar present"


CHECKS = (
    check_kdj_against_naive,
    check_macd_against_naive,
    check_crossover_edges,
    check_fill_timing,
    check_prefix_technique_bites,
    check_indicators_are_causal,
    check_lookahead_arm_beats_main,
    check_horizon_win_rate,
    check_flat_return_counts_as_loss,
    check_trades_never_overlap,
    check_no_signal_before_warmup,
    check_spec_matches_legacy,
    check_trail_exit,
    check_fixed_exit,
    check_k_band_entry,
    check_search_window_sealed,
)


def main() -> int:
    """Run every check and report. Returns 0 when all pass, 1 otherwise."""
    failures = 0
    for check in CHECKS:
        try:
            detail = check()
        except AssertionError as error:
            failures += 1
            print(f"FAIL {check.__name__}: {error}")
        except Exception as error:
            failures += 1
            print(f"ERROR {check.__name__}: {type(error).__name__}: {error}")
        else:
            print(f"PASS {check.__name__}: {detail}")
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
