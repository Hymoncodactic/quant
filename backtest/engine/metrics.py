"""Performance metrics under the project's capital and rate conventions.

Responsibility: turn one run's equity and trades frames into the mandatory
statistics of fixplans/framework/05_metrics_reporting.md.
Not responsible for: producing the frames (engine) or writing them (results).

Conventions enforced here:
    - capital = PEAK concurrent occupancy (cost basis + reserved cash), never
      a planned allocation;
    - annualized return = total return / online days * factor / capital,
      simple scaling, no compounding;
    - the annualization factor is an argument -- this module must stay
      venue-neutral (252 vs 365 confusion is a named failure mode);
    - signed median and absolute-deviation median are separate numbers
      (CLAUDE.md section 2.3);
    - every statistic is an in-sample interval statistic and is labeled so by
      the caller's report, not silently universalized.

Floats are acceptable here: metrics are statistics, not ledger money
(quant-code-standards section 5.1).

Public functions:
    compute_metrics(equity, trades, initial_cash, annualization_days)
    realized_pnl_per_sell(trades)      Average-cost realized PnL replay
"""

from __future__ import annotations

__all__ = ["compute_metrics", "realized_pnl_per_sell"]

import math

import numpy as np
import pandas as pd


def realized_pnl_per_sell(trades: pd.DataFrame) -> list[float]:
    """Replay fills under average-cost accounting; one realized PnL per sell.

    Buys accumulate cost basis (their full GBP outlay, fees included); each
    sell realizes proceeds minus the basis share it releases. Matches the
    ledger's accounting exactly, so metrics and ledger cannot diverge.
    """
    basis: dict[str, tuple[float, float]] = {}  # symbol -> (qty, cost)
    out: list[float] = []
    if trades.empty:
        return out
    # order_id breaks step ties: fills sharing a bar must replay in the
    # broker's booking order, and a bare single-key quicksort is unstable.
    for row in trades.sort_values(["step", "order_id"]).itertuples(index=False):
        qty, cash = float(row.quantity), float(row.cash_delta_gbp)
        held_qty, held_cost = basis.get(row.symbol, (0.0, 0.0))
        if qty > 0:
            basis[row.symbol] = (held_qty + qty, held_cost - cash)
            continue
        sold = -qty
        if held_qty <= 0:
            raise ValueError(f"sell of {row.symbol} without basis")
        released = held_cost * sold / held_qty
        out.append(cash - released)
        basis[row.symbol] = (held_qty - sold, held_cost - released)
    return out


def compute_metrics(equity: pd.DataFrame, trades: pd.DataFrame,
                    initial_cash_gbp: float,
                    annualization_days: int) -> dict:
    """All mandatory statistics for one run. Returns a JSON-ready dict."""
    if equity.empty:
        return {"error": "empty equity record"}
    capital = float(equity["occupied_gbp"].max())
    final_equity = float(equity["equity_gbp"].iloc[-1])
    total_return = final_equity - initial_cash_gbp

    daily = _daily_equity(equity)
    online_mask = daily["occupied_gbp"] > 0
    online_days = int(online_mask.sum())

    out: dict = {
        "capital_peak_occupied_gbp": capital,
        "total_return_gbp": total_return,
        "online_trading_days": online_days,
        "annualization_days": annualization_days,
        "fills": int(len(trades)),
    }
    if capital <= 0 or online_days == 0:
        out["note"] = "no capital ever occupied; rate metrics undefined"
        return out

    out["total_return_rate_on_capital"] = total_return / capital
    out["annualized_return_rate"] = (total_return / online_days
                                     * annualization_days / capital)

    # Daily rate on capital over ONLINE days only -- the same day base as the
    # annualized return, so the two rate metrics cannot silently diverge by
    # the span/online ratio. A diff across an offline gap aggregates the
    # whole gap into one observation; acceptable because capital was zero in
    # between.
    span = daily.loc[online_mask]
    rets = span["equity_gbp"].diff().dropna() / capital
    if len(rets) >= 2 and float(rets.std(ddof=1)) > 0:
        out["sharpe_rf0"] = (float(rets.mean()) / float(rets.std(ddof=1))
                             * math.sqrt(annualization_days))
    else:
        out["sharpe_rf0"] = None

    curve = equity["equity_gbp"].to_numpy()
    peak = np.maximum.accumulate(curve)
    dd = curve - peak
    out["max_drawdown_gbp"] = float(-dd.min())
    out["max_drawdown_on_capital"] = float(-dd.min()) / capital
    ann = out["annualized_return_rate"]
    out["calmar"] = (ann / out["max_drawdown_on_capital"]
                     if out["max_drawdown_on_capital"] > 0 else None)

    pnls = realized_pnl_per_sell(trades)
    out.update(_trade_stats(pnls))
    if not trades.empty:
        # Turnover convention (ruled in fixplans/framework/
        # 05_metrics_reporting.md section 2): BOTH legs, cost-inclusive --
        # the absolute GBP cash moved per fill. A buy-and-sell round trip of
        # one position therefore counts roughly twice its value.
        notional = trades["cash_delta_gbp"].abs().sum()
        out["turnover_both_legs_on_capital"] = float(notional) / capital
        cost_cols = [c for c in trades.columns if c.startswith("cost_")]
        out["costs_gbp_total"] = {c[5:]: float(trades[c].sum())
                                  for c in cost_cols}
        # Per-deployment occupancy: each buy's full GBP outlay. When the peak
        # equals the largest single outlay, deployments never overlapped and
        # the report must say so (plan 05 section 1.2).
        buys = trades.loc[trades["quantity"] > 0, "cash_delta_gbp"]
        if not buys.empty:
            outlays = -buys
            out["single_outlay_max_gbp"] = float(outlays.max())
            out["single_outlay_mean_gbp"] = float(outlays.mean())
            out["positions_never_overlapped"] = bool(
                abs(capital - float(outlays.max())) < 1e-9)
    return out


def _daily_equity(equity: pd.DataFrame) -> pd.DataFrame:
    """Last record per calendar date, indexed by date."""
    frame = equity.copy()
    ts = pd.to_datetime(frame["ts"])
    frame["date"] = ts.dt.date
    return frame.groupby("date").last()


def _trade_stats(pnls: list[float]) -> dict:
    """Closed-trade statistics; signed and absolute medians kept apart."""
    if not pnls:
        return {"closed_trades": 0}
    arr = np.asarray(pnls, dtype=float)
    wins, losses = arr[arr > 0], arr[arr < 0]
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    return {
        "closed_trades": int(arr.size),
        "win_rate": float((arr > 0).mean()),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_win_over_avg_loss": (float(wins.mean()) / float(-losses.mean())
                                  if wins.size and losses.size else None),
        "pnl_median_signed_gbp": float(np.median(arr)),
        "pnl_median_abs_deviation_gbp": float(
            np.median(np.abs(arr - np.median(arr)))),
    }
