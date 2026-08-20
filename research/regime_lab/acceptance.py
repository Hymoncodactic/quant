"""Executable acceptance checks for the regime study.

Every check is constructed so a plausible implementation error fails it
(backtest-discipline section 6). Run before any search result is trusted.

Usage:
    python -m regime_lab.acceptance
"""

from __future__ import annotations

__all__ = ["main", "CHECKS"]

import numpy as np
import pandas as pd

from . import data as data_module
from . import metrics as metrics_module
from . import rigor as rigor_module
from . import vol as vol_module
from .engine import Panels


def check_yang_zhang_against_naive() -> str:
    """Yang-Zhang must match an independent loop; window change must matter.

    Tolerance 1e-10 relative: same formula, different association only.
    """
    rng = np.random.default_rng(11)
    n = 300
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    bars = pd.DataFrame({
        "open": close * np.exp(rng.normal(0, 0.005, n)),
        "high": close * np.exp(np.abs(rng.normal(0, 0.01, n)) + 0.002),
        "low": close * np.exp(-np.abs(rng.normal(0, 0.01, n)) - 0.002),
        "close": close,
    })
    window = 20

    def naive(w: int) -> np.ndarray:
        o = np.log(bars["open"].to_numpy()[1:] / close[:-1])
        c = np.log(close[1:] / bars["open"].to_numpy()[1:])
        u = np.log(bars["high"].to_numpy()[1:] / bars["open"].to_numpy()[1:])
        d = np.log(bars["low"].to_numpy()[1:] / bars["open"].to_numpy()[1:])
        rs = u * (u - c) + d * (d - c)
        k = 0.34 / (1.34 + (w + 1) / (w - 1))
        out = np.full(n, np.nan)
        for i in range(w, n):
            so = o[i - w: i].var(ddof=1)
            sc = c[i - w: i].var(ddof=1)
            srs = rs[i - w: i].mean()
            out[i] = np.sqrt(max(so + k * sc + (1 - k) * srs, 0.0) * 252.0)
        return out

    ours = vol_module.yang_zhang(bars, window).to_numpy()
    theirs = naive(window)
    both = ~np.isnan(ours) & ~np.isnan(theirs)
    assert both.sum() > 200, "no overlap to compare"
    assert np.allclose(ours[both], theirs[both], rtol=1e-10), "YZ differs from the loop"
    assert np.nanmax(np.abs(ours - vol_module.yang_zhang(bars, 40).to_numpy())) > 1e-3, (
        "window has no effect; comparison is not discriminating")
    return "Yang-Zhang matches an independent loop; the window parameter bites"


def check_engine_fixture() -> str:
    """The portfolio arithmetic must reproduce a hand-computed two-symbol day.

    Uses a monkeypatched two-symbol Panels with known prices; validates the cc,
    on and id streams and the equal-slot averaging including a NaN symbol.
    """
    index = pd.date_range("2024-01-01", periods=4, freq="B")
    close = pd.DataFrame({"A": [100.0, 110.0, 99.0, 105.0],
                          "B": [50.0, np.nan, 52.0, 51.0]}, index=index)
    open_ = pd.DataFrame({"A": [99.0, 108.0, 100.0, 101.0],
                          "B": [50.5, np.nan, 51.0, 52.0]}, index=index)
    prev = close.shift(1)
    streams = {"cc": close / prev - 1.0, "on": open_ / prev - 1.0, "id": close / open_ - 1.0}
    ones = pd.DataFrame(1.0, index=index, columns=["A", "B"])
    panels = Panels(streams=streams, signals={"always": ones},
                    vol_scalars={"off": ones},
                    market_mult={"none": pd.Series(1.0, index=index)},
                    trend_mult={"off": pd.Series(1.0, index=index)},
                    available=close.notna(), index=index)
    r = panels.run(("cc", "always", "none", "off", "off"))
    assert abs(r.iloc[1] - 0.10) < 1e-12, f"day 2 should be A only: 10%, got {r.iloc[1]}"
    # B's day-3 cc return spans its missing day-2 close and is NaN by design:
    # the engine excludes the slot rather than bridging across the gap, so
    # day 3 is A alone at -10%.
    assert abs(r.iloc[2] - (99.0 / 110.0 - 1.0)) < 1e-12, (
        "day 3 must hold A alone; a gap-bridging implementation would average in B")
    r_id = panels.run(("id", "always", "none", "off", "off"))
    expected_id = ((105.0 / 101.0 - 1.0) + (51.0 / 52.0 - 1.0)) / 2.0
    assert abs(r_id.iloc[3] - expected_id) < 1e-12, "intraday stream wrong"
    return "two-symbol fixture reproduces cc/on/id arithmetic including the NaN day"


def check_signal_shift_is_lookahead_proof() -> str:
    """Removing the one-day signal lag must materially help - proving the lag matters.

    A same-day ma200 signal on the cc stream peeks at the close it trades on.
    If unshifted and shifted versions performed alike, the timing convention
    would be untestable. Requires the cheat's ann_simple to beat the clean one
    by more than 5 percentage points annualized on the tech universe.
    """
    panels = Panels.build(data_module.TECH18)
    clean = panels.run(("cc", "ma200", "none", "off", "off"))

    cheat_signals = {name: frame.shift(-1) for name, frame in panels.signals.items()}
    cheat = Panels(streams=panels.streams, signals=cheat_signals,
                   vol_scalars=panels.vol_scalars, market_mult=panels.market_mult,
                   trend_mult=panels.trend_mult, available=panels.available,
                   index=panels.index)
    cheat_r = cheat.run(("cc", "ma200", "none", "off", "off"))

    window = slice("2005-01-01", "2017-12-31")
    gain = (cheat_r.loc[window].mean() - clean.loc[window].mean()) * 252.0
    assert gain > 0.05, f"unshifted signal gains only {gain:.2%} - lag check not discriminating"
    return f"peeking one day ahead adds {gain:.1%} annualized - the shift convention is load-bearing"


def check_signals_causal_by_prefix() -> str:
    """Signal and regime values at day t must not change when later data is removed."""
    panels = Panels.build(("NVDA", "AAPL"))
    cutoff = panels.index[-500]
    prefix_close = {}
    for symbol in ("NVDA", "AAPL"):
        bars = data_module.load_daily(symbol)
        prefix_close[symbol] = bars[bars["date"] <= cutoff]
    full_sig = panels.signals["tsmom252"].loc[cutoff]
    full_gate = panels.market_mult["p70x0"].loc[cutoff]

    import unittest.mock as mock
    original = data_module.load_daily

    def truncated(symbol, group="us_equity", start=None, end=None):
        frame = original(symbol, group, start, end)
        return frame[frame["date"] <= cutoff].reset_index(drop=True)

    with mock.patch.object(data_module, "load_daily", side_effect=truncated):
        prefix_panels = Panels.build(("NVDA", "AAPL"))
    assert prefix_panels.signals["tsmom252"].loc[cutoff].equals(full_sig), (
        "tsmom252 at the cutoff changes when the future is deleted")
    assert prefix_panels.market_mult["p70x0"].loc[cutoff] == full_gate, (
        "the vol-regime gate at the cutoff changes when the future is deleted")
    return "signals and the regime gate at t are bit-identical on the truncated dataset"


def check_max_drawdown_fixture() -> str:
    """MaxDD must be 50% for a +100% then -50% then flat curve."""
    r = pd.Series([1.0, -0.5, 0.0, 0.0])
    depth, _, _ = metrics_module.max_drawdown(r)
    assert abs(depth - 0.5) < 1e-12, f"expected 0.5, got {depth}"
    r2 = pd.Series([0.1, 0.1, 0.1])
    depth2, _, _ = metrics_module.max_drawdown(r2)
    assert depth2 == 0.0, "monotone-up curve must have zero drawdown"
    return "drawdown fixture: 0.5 on the crash curve, 0 on the monotone curve"


def check_spa_calibration() -> str:
    """SPA size and power over repeated draws, not one lucky seed.

    Size: across 20 independent skill-free families (20 configs vs a noise
    benchmark), the rejection rate at the 10 percent level must stay below 30
    percent - a correctly sized test averages 10. Power: a +40bp/day edge
    injected into one config must yield p < 0.01 in every draw.
    """
    n, k, draws = 1200, 20, 20
    rejections = 0
    power_failures = 0
    for d in range(draws):
        rng = np.random.default_rng(100 + d)
        bench = pd.Series(rng.normal(0.0004, 0.012, n))
        family = pd.DataFrame(rng.normal(0.0004, 0.012, (n, k)))
        null_p = rigor_module.spa_test(family, bench, n_boot=400, seed=d)["p_value"]
        rejections += null_p < 0.10
        family[0] = bench.to_numpy() + rng.normal(0.004, 0.012, n)
        alt_p = rigor_module.spa_test(family, bench, n_boot=400, seed=d)["p_value"]
        power_failures += alt_p >= 0.01
    assert rejections <= 6, f"SPA over-rejects the null: {rejections}/{draws} at the 10% level"
    assert power_failures == 0, f"SPA missed an injected edge in {power_failures}/{draws} draws"
    return f"SPA size {rejections}/{draws} rejections at 10%; injected edge caught in all {draws} draws"


def check_dsr_calibration() -> str:
    """DSR must stay low for the best of N skill-free trials and high for real skill."""
    rng = np.random.default_rng(3)
    n, k = 1500, 450
    trials = rng.normal(0.0, 0.012, (n, k))
    sharpes = trials.mean(axis=0) / trials.std(axis=0, ddof=1)
    best = int(np.argmax(sharpes))
    lucky = rigor_module.deflated_sharpe(pd.Series(trials[:, best]), k, float(sharpes.var(ddof=1)))
    assert lucky["dsr"] < 0.90, f"DSR fails to deflate a lucky best-of-{k} (dsr={lucky['dsr']})"
    # Daily SR ~0.167 (annualized ~2.6) against the best-of-450 luck bar
    # (~0.088 daily) should be decisively above it; ~0.0015 would sit at the
    # threshold by construction and make the check flaky.
    skilled = rigor_module.deflated_sharpe(
        pd.Series(rng.normal(0.002, 0.012, n)), k, float(sharpes.var(ddof=1)))
    assert skilled["dsr"] > 0.95, f"DSR rejects genuine skill (dsr={skilled['dsr']})"
    return f"DSR calibrated: lucky best-of-450 dsr={lucky['dsr']:.3f}, real skill dsr={skilled['dsr']:.3f}"


CHECKS = (
    check_yang_zhang_against_naive,
    check_engine_fixture,
    check_signal_shift_is_lookahead_proof,
    check_signals_causal_by_prefix,
    check_max_drawdown_fixture,
    check_spa_calibration,
    check_dsr_calibration,
)


def main() -> int:
    """Run every check; exit 0 only if all pass."""
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
