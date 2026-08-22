"""Smoke run of the T212 backtest framework against real curated data.

Window and universe are chosen for discrimination, not convenience:
    - 2026-06-01 .. 2026-08-14 crosses the BST regime in which London daily
      bars stamp 23:00 UTC of the previous day (the alignment trap of
      fixplans/framework/02_data_layer.md section 3.1);
    - 2026-07-03 is a US market holiday with the LSE open, so the calendar
      asymmetry path executes;
    - the universe mixes USD (AAPL, CSPX.L), GBP (VUSA.L) and GBp (SGLN.L)
      quote currencies, so every conversion branch runs.

Checks performed (each fails loudly; a passing run prints PASS lines):
    C1 every symbol traded in both tiers
    C2 worst tier costs strictly exceed actual tier costs
    C3 no AAPL fill on the 2026-07-03 US holiday
    C4 London daily fills carry the 23:00-UTC BST stamp
    C5 a USD fill's fx_mid equals the PREVIOUS trading day's GBPUSD close
    C6 two identical worst-tier runs produce identical trades and equity
    C7 same_close mode fills on the decision day at close x (1 + gap), never
       on a later day unless latency/closed-market spilled it

Usage:
    python scripts/20260820_t212_backtest_smoke.py [--data-root PATH]

data-root defaults to the repository's own data/ tree; pass the main working
copy's path when running from a git worktree.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine.types import EngineConfig               # noqa: E402
from backtest.t212.data_source import load_fx                # noqa: E402
from backtest.t212.instruments import exchange_tz             # noqa: E402
from backtest.t212.runner import run_t212_backtest           # noqa: E402

SYMBOLS = ["AAPL", "CSPX.L", "VUSA.L", "SGLN.L"]
START, END = "2026-06-01", "2026-08-14"
US_HOLIDAY = "2026-07-03"
INITIAL_CASH = Decimal("20000")
TARGETS = {"AAPL": Decimal("10"), "CSPX.L": Decimal("3"),
           "VUSA.L": Decimal("20"), "SGLN.L": Decimal("100")}


def fixed_holdings(view, portfolio, params):
    """Hold constant target quantities; the engine buys once and re-tries
    any rejected order on later bars automatically."""
    return {s: q for s, q in TARGETS.items() if view.bar(s) is not None}


def _run(tier: str, data_root: str | None, seed: int = 7,
         fill_timing: str = "next_open"):
    config = EngineConfig(symbols=SYMBOLS, interval="1d", start=START,
                          end=END, initial_cash_gbp=INITIAL_CASH,
                          arm="smoke", fee_tier=tier, seed=seed,
                          strategy_name="smoke_fixed_holdings",
                          fill_timing=fill_timing)
    return run_t212_backtest(config, fixed_holdings, data_root=data_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default=None)
    args = parser.parse_args()

    result_w, metrics_w, paths_w = _run("worst", args.data_root)
    result_a, metrics_a, _ = _run("actual", args.data_root)
    result_w2, _, _ = _run("worst", args.data_root)

    checks: list[tuple[str, bool, str]] = []

    traded_w = set(result_w.trades["symbol"].unique())
    traded_a = set(result_a.trades["symbol"].unique())
    checks.append(("C1 all symbols traded in both tiers",
                   traded_w == set(SYMBOLS) == traded_a,
                   f"worst={sorted(traded_w)} actual={sorted(traded_a)}"))

    cost_w = sum(metrics_w.get("costs_gbp_total", {}).values())
    cost_a = sum(metrics_a.get("costs_gbp_total", {}).values())
    checks.append(("C2 worst-tier costs strictly higher",
                   cost_w > cost_a,
                   f"worst={cost_w:.4f} GBP actual={cost_a:.4f} GBP"))

    aapl = result_w.trades[result_w.trades["symbol"] == "AAPL"]
    aapl_days = set(pd.to_datetime(aapl["ts"])
                    .dt.tz_convert("America/New_York").dt.date.astype(str))
    checks.append(("C3 no AAPL fill on the US holiday",
                   US_HOLIDAY not in aapl_days, f"AAPL fill days={sorted(aapl_days)}"))

    ldn = result_w.trades[result_w.trades["symbol"].str.endswith(".L")]
    ldn_hours = set(pd.to_datetime(ldn["ts"]).dt.hour)
    checks.append(("C4 London daily fills carry the 23:00 UTC BST stamp",
                   ldn_hours == {23}, f"hours={sorted(ldn_hours)}"))

    fx_frame = load_fx("1d", START, END, args.data_root)
    fx_frame["ts"] = pd.to_datetime(fx_frame["ts"], utc=True)
    usd = result_w.trades[result_w.trades["symbol"] == "AAPL"].iloc[0]
    fill_ts = pd.Timestamp(usd["ts"])
    prior = fx_frame[fx_frame["ts"] + pd.Timedelta(days=1) <= fill_ts]
    expected_mid = float(prior["close"].iloc[-1])
    same_day = fx_frame[(fx_frame["ts"] + pd.Timedelta(days=1) > fill_ts)
                        & (fx_frame["ts"] <= fill_ts)]
    lookahead_mid = float(same_day["close"].iloc[-1]) if not same_day.empty else None
    discriminative = lookahead_mid is None or lookahead_mid != expected_mid
    checks.append(("C5 USD fill uses the prior day's GBPUSD close",
                   abs(float(usd["fx_mid"]) - expected_mid) < 1e-12,
                   f"fx_mid={usd['fx_mid']} expected={expected_mid} "
                   f"same-day close={lookahead_mid} "
                   f"(check discriminative: {discriminative})"))

    result_c, _, _ = _run("worst", args.data_root, fill_timing="same_close")
    close_rows = result_c.trades
    at_close = close_rows["at_close"].astype(bool)
    # Compare on the EXCHANGE-LOCAL trading day: London daily bars stamp
    # 23:00 UTC of the previous day during BST (the known alignment trap).
    local_day = [pd.Timestamp(ts).tz_convert(exchange_tz(sym)).date()
                 for ts, sym in zip(close_rows["ts"], close_rows["symbol"])]
    same_day = pd.Series(local_day, index=close_rows.index) \
        == pd.to_datetime(close_rows["submitted_ts"]).dt.date
    checks.append(("C7 same_close fills on the decision day at close",
                   bool(at_close.all()) and bool(same_day[at_close].all()),
                   f"at_close={at_close.tolist()} same_day={same_day.tolist()}"))

    same_trades = result_w.trades.equals(result_w2.trades)
    same_equity = result_w.equity.equals(result_w2.equity)
    checks.append(("C6 identical reruns are identical",
                   same_trades and same_equity,
                   f"trades_equal={same_trades} equity_equal={same_equity}"))

    print("=" * 72)
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}")
    print("=" * 72)
    print(f"worst tier : equity {metrics_w.get('total_return_gbp', 0):+.2f} GBP on "
          f"capital {metrics_w.get('capital_peak_occupied_gbp', 0):.2f}, "
          f"fills={metrics_w.get('fills')}, "
          f"costs={metrics_w.get('costs_gbp_total')}")
    print(f"actual tier: equity {metrics_a.get('total_return_gbp', 0):+.2f} GBP, "
          f"fills={metrics_a.get('fills')}, "
          f"costs={metrics_a.get('costs_gbp_total')}")
    print("results written:", {k: str(v) for k, v in paths_w.items()})
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
