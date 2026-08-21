"""Per-run chart: equity curve with open-position interval shading.

Responsibility: one self-contained HTML chart next to the result files --
net-value curve on top, per-symbol holding intervals underneath, with the
in-market stretches shaded on the curve. Quick visual inspection only; the
formal report layer is the /html-report skill and reads the RESULT FILES,
never this chart.
Not responsible for: computing anything (metrics.py) or determinism -- the
chart embeds the plotting library and is excluded from the byte-identity
guarantee that covers trades/equity/meta.

Public functions:
    in_market_spans(equity)              Consecutive occupied>0 stretches
    write_chart(result, title, path)     Write the HTML chart, returns path
"""

from __future__ import annotations

__all__ = ["in_market_spans", "write_chart"]

from pathlib import Path

import pandas as pd

from backtest.engine.engine import RunResult
from backtest.engine.metrics import holding_episodes, naive_utc


def in_market_spans(equity: pd.DataFrame) -> list[tuple[pd.Timestamp,
                                                         pd.Timestamp]]:
    """Consecutive stretches of the equity record where capital is occupied.

    Returns [(start_ts, end_ts)] in naive-UTC timestamps, ready for shading.
    """
    spans: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    start = prev = None
    for row in equity.itertuples(index=False):
        ts = naive_utc(row.ts)
        if row.occupied_gbp > 0:
            if start is None:
                start = ts
            prev = ts
        elif start is not None:
            spans.append((start, prev))
            start = prev = None
    if start is not None:
        spans.append((start, prev))
    return spans


def write_chart(result: RunResult, title: str, path: Path | str) -> Path:
    """Write the equity + holding-intervals chart for one finished run."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    equity = result.equity
    if equity.empty:
        raise ValueError("cannot chart an empty equity record")
    ts_axis = [naive_utc(t) for t in equity["ts"]]
    episodes = holding_episodes(result.trades, equity["ts"].iloc[-1])
    symbols = sorted({e["symbol"] for e in episodes})

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.72, 0.28], vertical_spacing=0.06,
                        subplot_titles=("Equity (GBP)",
                                        "Open-position intervals"))
    fig.add_trace(go.Scatter(x=ts_axis, y=equity["equity_gbp"],
                             name="equity_mid_gbp", mode="lines",
                             line={"width": 1.6}), row=1, col=1)
    if "equity_liq_gbp" in equity.columns:
        fig.add_trace(go.Scatter(x=ts_axis, y=equity["equity_liq_gbp"],
                                 name="equity_liquidation_gbp", mode="lines",
                                 line={"width": 1.2, "dash": "dash"}),
                      row=1, col=1)
    fig.add_trace(go.Scatter(x=ts_axis, y=equity["occupied_gbp"],
                             name="occupied_gbp", mode="lines",
                             line={"width": 1.0, "dash": "dot"}), row=1, col=1)
    for x0, x1 in in_market_spans(equity):
        fig.add_vrect(x0=x0, x1=x1, fillcolor="green", opacity=0.08,
                      line_width=0, row=1, col=1)
    for episode in episodes:
        fig.add_trace(go.Scatter(
            x=[episode["start_ts"], episode["end_ts"]],
            y=[episode["symbol"], episode["symbol"]],
            mode="lines",
            line={"width": 8,
                  "dash": "dot" if episode["open_at_end"] else "solid"},
            showlegend=False,
            hovertext=f"{episode['symbol']} {episode['days']:.1f}d"
                      + (" (open at end)" if episode["open_at_end"] else ""),
        ), row=2, col=1)
    fig.update_yaxes(categoryorder="array", categoryarray=symbols,
                     row=2, col=1)
    fig.update_layout(title=title, template="plotly_white",
                      legend={"orientation": "h"}, height=640)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(path, include_plotlyjs=True, full_html=True,
                   div_id="backtest_chart")
    return path
