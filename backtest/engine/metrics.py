"""Performance metrics under the project's capital and rate conventions.

Responsibility: turn one run's equity and trades frames into the mandatory
statistics of docs/backtest/framework/05_metrics_reporting.md. Five conventions are
enforced here rather than left to the caller. Capital is the PEAK concurrent
occupancy, cost basis plus reserved cash, never a planned allocation.
Annualized return is total return divided by online days, times the
annualization factor, divided by capital: simple scaling with no compounding.
The annualization factor is an argument, because this module must stay
venue-neutral and confusing 252 with 365 is a named failure mode. The signed
median and the absolute-deviation median are reported as two separate numbers
(CLAUDE.md section 2.3). Every statistic is an in-sample interval statistic and
must be labeled as such by the caller's report rather than silently
universalized. The authoritative headline return and drawdown read the
liquidation-valued equity column when the venue supplies one; the mid-mark
versions remain diagnostics. Floats are acceptable here because metrics are
statistics, not ledger money (quant-code-standards section 5.1).

Out of scope: producing the frames, which belongs to engine.py and ledger.py;
writing them, which belongs to results.py; charting them, which belongs to
report.py.

Public functions:
    compute_metrics(equity, trades, initial_cash_gbp, annualization_days)
                                       All mandatory statistics of one run, as
                                       a JSON-ready dict.
    realized_pnl_per_sell(trades)      Average-cost replay of the fills; one
                                       realized PnL per sell, matching the
                                       ledger's accounting exactly so the two
                                       cannot diverge.
    holding_episodes(trades, final_ts) Per-symbol spans during which a position
                                       was open; an episode still open at the
                                       end of the window is closed at final_ts
                                       and flagged open_at_end rather than
                                       dropped.
    naive_utc(ts)                      Strip the time zone, converting to UTC
                                       first, so daily-mode naive keys and
                                       tz-aware fill timestamps subtract on one
                                       axis.

Constants:
    _SECONDS_PER_DAY   float   86400.0, the divisor turning a timedelta into
                       calendar days for holding and drawdown durations.

Inputs: None.
Outputs: None. Statistics are returned in memory as a dict.

Change log:
    2026-08-22  Header expanded to the six-section spec.
"""

from __future__ import annotations

__all__ = ["compute_metrics", "realized_pnl_per_sell", "holding_episodes",
           "naive_utc"]

import math

import numpy as np
import pandas as pd

_SECONDS_PER_DAY = 86400.0


def naive_utc(ts: pd.Timestamp) -> pd.Timestamp:
    """Strip time zone (converting to UTC first) so daily-mode naive keys and
    tz-aware fill timestamps can be subtracted on one axis."""
    ts = pd.Timestamp(ts)
    return ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo else ts


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


def holding_episodes(trades: pd.DataFrame,
                     final_ts: pd.Timestamp) -> list[dict]:
    """Per-symbol spans during which a position was open.

    An episode starts when a symbol's cumulative filled quantity leaves zero
    and ends when it returns to zero; an episode still open at the end of the
    window is closed at final_ts and flagged open_at_end (censored duration,
    never silently dropped).

    Returns:
        [{symbol, start_ts, end_ts, days, open_at_end}], chronological per
        symbol. days is a float of calendar days.
    """
    episodes: list[dict] = []
    if trades.empty:
        return episodes
    ordered = trades.sort_values(["step", "order_id"])
    for symbol, group in ordered.groupby("symbol", sort=True):
        qty, start = 0.0, None
        for row in group.itertuples(index=False):
            before = qty
            qty += float(row.quantity)
            if before == 0.0 and qty != 0.0:
                start = naive_utc(row.ts)
            elif before != 0.0 and abs(qty) < 1e-12 and start is not None:
                end = naive_utc(row.ts)
                episodes.append({
                    "symbol": symbol, "start_ts": start, "end_ts": end,
                    "days": (end - start).total_seconds() / _SECONDS_PER_DAY,
                    "open_at_end": False})
                qty, start = 0.0, None
        if start is not None:
            end = naive_utc(final_ts)
            episodes.append({
                "symbol": symbol, "start_ts": start, "end_ts": end,
                "days": (end - start).total_seconds() / _SECONDS_PER_DAY,
                "open_at_end": True})
    return episodes


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
    out.update(_ratio_stats(rets, annualization_days))
    out["exposure_time_fraction"] = online_days / len(daily)

    curve = equity["equity_gbp"].to_numpy()
    peak = np.maximum.accumulate(curve)
    dd = curve - peak
    out["max_drawdown_gbp"] = float(-dd.min())
    out["max_drawdown_on_capital"] = float(-dd.min()) / capital
    ann = out["annualized_return_rate"]
    out["calmar"] = (ann / out["max_drawdown_on_capital"]
                     if out["max_drawdown_on_capital"] > 0 else None)
    out["longest_drawdown_days"] = _longest_drawdown_days(equity)
    out.update(_liquidation_stats(equity, initial_cash_gbp, final_equity,
                                  capital))
    out.update(_holding_stats(trades, equity["ts"].iloc[-1]))

    pnls = realized_pnl_per_sell(trades)
    out.update(_trade_stats(pnls))
    if not trades.empty:
        # Turnover convention (ruled in docs/backtest/framework/
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


def _ratio_stats(rets: pd.Series, annualization_days: int) -> dict:
    """Sharpe, Sortino, annualized volatility from the daily return-on-capital
    series over online days. Risk-free rate fixed at 0 (metadata rule:
    docs/backtest/framework/05_metrics_reporting.md section 2)."""
    out: dict = {"sharpe_rf0": None, "sortino_rf0": None,
                 "annualized_volatility_on_capital": None}
    if len(rets) < 2:
        return out
    std = float(rets.std(ddof=1))
    mean = float(rets.mean())
    if std > 0:
        out["sharpe_rf0"] = mean / std * math.sqrt(annualization_days)
        out["annualized_volatility_on_capital"] = std * math.sqrt(
            annualization_days)
    downside = rets[rets < 0]
    if len(downside) >= 2:
        dstd = float(np.sqrt((downside ** 2).mean()))
        if dstd > 0:
            out["sortino_rf0"] = mean / dstd * math.sqrt(annualization_days)
    return out


def _liquidation_stats(equity: pd.DataFrame, initial_cash_gbp: float,
                       final_equity_mid: float, capital: float) -> dict:
    """Liquidation-valued counterparts of return and drawdown.

    equity_liq_gbp marks positions at exit value (bid side, FX fee, sell
    taxes). The AUTHORITATIVE headline numbers are these; the mid-mark
    versions stay as diagnostics (docs/backtest/framework/05_metrics_reporting.md).
    Absent column (no venue liquidation valuer) yields no keys.
    """
    if "equity_liq_gbp" not in equity.columns:
        return {}
    liq = equity["equity_liq_gbp"].to_numpy()
    peak = np.maximum.accumulate(liq)
    dd = liq - peak
    return {
        "final_equity_liquidation_gbp": float(liq[-1]),
        "total_return_liquidation_gbp": float(liq[-1]) - initial_cash_gbp,
        "total_return_liquidation_rate_on_capital":
            (float(liq[-1]) - initial_cash_gbp) / capital,
        "max_drawdown_liq_gbp": float(-dd.min()),
        "max_drawdown_liq_on_capital": float(-dd.min()) / capital,
        "exit_costs_at_end_gbp": final_equity_mid - float(liq[-1]),
    }


def _longest_drawdown_days(equity: pd.DataFrame) -> float | None:
    """Longest peak-to-recovery stretch of the equity curve, calendar days."""
    curve = equity["equity_gbp"].to_numpy()
    ts = [naive_utc(t) for t in equity["ts"]]
    longest, peak_i = 0.0, 0
    for i in range(1, len(curve)):
        if curve[i] >= curve[peak_i]:
            peak_i = i
        else:
            span = (ts[i] - ts[peak_i]).total_seconds() / _SECONDS_PER_DAY
            longest = max(longest, span)
    return longest


def _holding_stats(trades: pd.DataFrame, final_ts: pd.Timestamp) -> dict:
    """Average, median and extreme holding durations across episodes."""
    episodes = holding_episodes(trades, final_ts)
    if not episodes:
        return {"holding_episodes": 0}
    days = np.asarray([e["days"] for e in episodes], dtype=float)
    return {
        "holding_episodes": len(episodes),
        "holding_episodes_open_at_end": int(sum(e["open_at_end"]
                                                for e in episodes)),
        "avg_holding_days": float(days.mean()),
        "median_holding_days": float(np.median(days)),
        "max_holding_days": float(days.max()),
        "min_holding_days": float(days.min()),
    }


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
        "expectancy_gbp": float(arr.mean()),
        "largest_win_gbp": float(arr.max()),
        "largest_loss_gbp": float(arr.min()),
        "max_consecutive_wins": _longest_run(arr > 0),
        "max_consecutive_losses": _longest_run(arr < 0),
        "pnl_median_signed_gbp": float(np.median(arr)),
        "pnl_median_abs_deviation_gbp": float(
            np.median(np.abs(arr - np.median(arr)))),
    }


def _longest_run(mask: np.ndarray) -> int:
    """Length of the longest consecutive True run."""
    longest = current = 0
    for hit in mask:
        current = current + 1 if hit else 0
        longest = max(longest, current)
    return longest
