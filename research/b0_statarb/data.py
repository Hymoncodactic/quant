"""Load the B0 universe's daily closes as one aligned panel.

Responsibility: turn the stored per-ticker daily partitions into a single
date-by-ticker frame of adjusted closes, plus the GICS map used to restrict
pairing to one sub-industry. Not responsible for fetching (that is
scripts/20260823_ingest_b0_universe.py) or for any statistic.

Public functions:
    load_universe()   Frozen universe as {ticker: (sector, sub_industry)}.
    load_closes()     Aligned adjusted-close panel, tickers as columns.
    liquid_subset()   Tickers passing the history and dollar-volume floors.

Constants:
    MIN_HISTORY_DAYS   int  Bars a name must have to enter the study, 1260 (~5y).
    MIN_DOLLAR_VOLUME  float  Median daily dollar volume floor, 5e6 USD.

Inputs:  data/reference/b0_universe_20260823.json,
         data/t212/curated/us_equity/<ticker>/1d/*.parquet
Outputs: none (returns frames)

Change log:
    2026-08-23  Created for the B0 statistical-arbitrage study.
"""

from __future__ import annotations

__all__ = ["load_universe", "load_closes", "liquid_subset",
           "MIN_HISTORY_DAYS", "MIN_DOLLAR_VOLUME"]

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
UNIVERSE_JSON = ROOT / "data" / "reference" / "b0_universe_20260823.json"
CURATED = ROOT / "data" / "t212" / "curated" / "us_equity"
NY = "America/New_York"

MIN_HISTORY_DAYS = 1260
MIN_DOLLAR_VOLUME = 5e6


def load_universe() -> dict[str, tuple[str, str]]:
    """Frozen universe as {ticker: (gics_sector, gics_sub_industry)}."""
    payload = json.loads(UNIVERSE_JSON.read_text())
    return {m["ticker"]: (m["gics_sector"], m["gics_sub_industry"])
            for m in payload["members"]}


def _read_one(ticker: str) -> pd.DataFrame | None:
    folder = CURATED / ticker / "1d"
    parts = sorted(folder.glob("*.parquet")) if folder.is_dir() else []
    if not parts:
        return None
    frame = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    frame["date"] = pd.to_datetime(frame["ts"], utc=True).dt.tz_convert(NY).dt.date
    return frame.sort_values("date").drop_duplicates("date", keep="last")


def load_closes(tickers: list[str] | None = None
                ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aligned close and dollar-volume panels, indexed by exchange-local date."""
    universe = load_universe()
    names = tickers if tickers is not None else sorted(universe)
    closes, dollars = {}, {}
    for ticker in names:
        frame = _read_one(ticker)
        if frame is None:
            continue
        series = frame.set_index("date")
        closes[ticker] = series["close"]
        dollars[ticker] = series["close"] * series["volume"]
    close_panel = pd.DataFrame(closes).sort_index()
    dollar_panel = pd.DataFrame(dollars).reindex(close_panel.index)
    return close_panel, dollar_panel


def liquid_subset(closes: pd.DataFrame, dollars: pd.DataFrame,
                  min_history: int = MIN_HISTORY_DAYS,
                  min_dollar: float = MIN_DOLLAR_VOLUME) -> list[str]:
    """Names with enough history and enough median dollar volume.

    Both floors are liquidity screens rather than performance screens, so they
    are applied once to the whole panel and never re-derived per window.
    """
    keep = []
    for ticker in closes.columns:
        series = closes[ticker].dropna()
        if len(series) < min_history:
            continue
        if float(dollars[ticker].dropna().median() or 0.0) < min_dollar:
            continue
        keep.append(ticker)
    return sorted(keep)
