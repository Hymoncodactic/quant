"""Pre-market pass: the A1 ranking table for one already-closed US session.

Responsibility: after a US session closes, rank the whole B0 candidate pool
for that session and store the result, so the next session's decision can read
a table instead of loading a 1,500-name panel. The decision window is roughly
half an hour wide and a full-pool pass takes minutes; that is the entire
reason this file exists (fixplans/t212/b0/03_data_pipeline.md section 4).

Every admission rule and the score itself come from
trading212/strategy/a1_v0_0_1.py rank_table. Nothing is re-implemented here: a
second copy of the admission thresholds is exactly the defect that would let
the live book and the backtest drift apart without either failing.

The candidate pool is read with the spelling the pool file carries, and a
member whose price directory does not exist is SKIPPED and reported. Two
members are affected today, BRK.B and BF.B, whose bars are stored under the
hyphen spelling; the research panel dropped them the same way, so the live
ranking sees exactly the 1,498 names every recorded A1 and B0 number was
computed on. Adding them back is a caliber change and needs a backtest, not
an edit here.

Out of scope: fetching bars (trading212/ingest/yahoo_bars.py), the admission
and ranking rules (trading212/strategy/a1_v0_0_1.py), when to run
(scripts/update_data.py), and reading the table back at decision time
(trading212/execution/market_data.py load_b0_injection).

Public functions:
    load_panels(members, start)       Close and volume panels from the lake.
    verified_tickers(symbols)         symbol -> venue ticker, from seam S5.
    latest_complete_session(closes)   Newest session the pool actually covers.
    build_table(session, params)      The ranking frame for one session.
    write_table(session, frame)       Atomic write to the session's parquet.
    run(session, params, force)       Build and write; returns a summary.

Constants:
    PANEL_START   str  "2010-01-04". The first row of the panel. It is a
                       CALIBER, not a convenience: admission E3 counts stored
                       bars from the panel's own start, so moving this date
                       moves which names are admitted, and every recorded A1
                       number was computed from here.
    RANK_COLUMNS  tuple  The frozen column order written to parquet; the
                       dashboard keys its wording on it, so it is a contract
                       (fixplans/t212/b0/00_coordination.md section 5.2).
    MIN_SESSION_COVERAGE float  0.95. A session is rankable only when at least
                       this share of the pool has a bar on it. Measured
                       2026-09-03: the newest row in the lake, 2026-09-01,
                       carried bars for 17 of 1,498 names because the daily
                       pass had not reached them yet, and ranking it admitted
                       15 names. A table like that is not a stale table, it is
                       a wrong one, and a decision reading it would rotate the
                       entire book into those fifteen. Refusing to write it
                       leaves the previous session's table in place, which the
                       decision layer already knows how to age
                       (03_data_pipeline.md section 4.3).

Inputs:
    data/reference/b0_universe_1500_20260823.json
    data/reference/t212_universe_ticker_map_<date>.json
    data/t212/curated/us_equity/<symbol>/1d/*.parquet
    trading212/config/strategies/a1_v0_0_1.yaml
Outputs:
    data/t212/curated/a1/rank/<YYYY-MM-DD>.parquet

Change log:
    2026-09-03  Created as the fourth pass of the daily update.
"""

from __future__ import annotations

__all__ = ["load_panels", "verified_tickers", "build_table", "write_table",
           "run", "latest_complete_session", "PANEL_START", "RANK_COLUMNS",
           "MIN_SESSION_COVERAGE"]

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from common.logging_setup import get_logger
from common.paths import (DIR_REFERENCE, a1_rank_path, config_dir,
                          equity_interval_dir)
from trading212.execution.instruments import ticker_map_for
from trading212.execution.strategy_loader import load_module

log = get_logger("t212.ingest")

PANEL_START = "2010-01-04"
MIN_SESSION_COVERAGE = 0.95
_TZ_NEW_YORK = "America/New_York"
_GROUP = "us_equity"

_a1 = load_module("a1", "0.0.1")
RANK_COLUMNS = _a1.RANK_COLUMNS + ("panel_as_of", "generated_at_utc",
                                   "code_version")


def _params() -> dict:
    """A1's parameters, read once from the file the strategy never reads."""
    path = config_dir("t212") / "strategies" / "a1_v0_0_1.yaml"
    params = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(params, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return params


def _universe_members(params: dict) -> list[str]:
    """The candidate pool, in the pool file's own spelling."""
    path = Path(params.get("universe_file")
                or DIR_REFERENCE / "b0_universe_1500_20260823.json")
    if not path.is_absolute():
        path = Path(str(path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [str(m["ticker"]) for m in payload["members"]]


def _load_frame(symbol: str) -> pd.DataFrame | None:
    """One symbol's daily bars, indexed by exchange-local date."""
    folder = equity_interval_dir(_GROUP, symbol, "1d")
    parts = sorted(folder.glob("*.parquet")) if folder.is_dir() else []
    if not parts:
        return None
    frame = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    frame["date"] = pd.to_datetime(frame["ts"], utc=True) \
        .dt.tz_convert(_TZ_NEW_YORK).dt.date
    frame = frame.sort_values("date").drop_duplicates("date", keep="last")
    frame = frame[frame["date"] >= pd.Timestamp(PANEL_START).date()]
    return frame if len(frame) else None


def load_panels(members: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, list]:
    """(closes, volumes, skipped) for the members that have stored bars."""
    closes, volumes, skipped = {}, {}, []
    for symbol in members:
        frame = _load_frame(symbol)
        if frame is None:
            skipped.append(symbol)
            continue
        series = frame.set_index("date")
        closes[symbol] = series["close"]
        volumes[symbol] = series["volume"]
    close_panel = pd.DataFrame(closes).sort_index()
    return close_panel, pd.DataFrame(volumes).reindex(close_panel.index), skipped


def verified_tickers(symbols: list[str]) -> dict[str, str]:
    """Seam S5, as admission E5 consumes it: symbol -> venue ticker."""
    return ticker_map_for(symbols)


def latest_complete_session(closes: pd.DataFrame):
    """The newest panel row whose coverage clears MIN_SESSION_COVERAGE."""
    covered = closes.notna().sum(axis=1) / max(closes.shape[1], 1)
    ok = covered[covered >= MIN_SESSION_COVERAGE]
    if ok.empty:
        raise ValueError(
            f"no session in the panel reaches {MIN_SESSION_COVERAGE:.0%} "
            f"coverage; the daily bars are not usable")
    return ok.index[-1]


def build_table(session, params: dict | None = None) -> pd.DataFrame:
    """The ranking frame for one already-closed session.

    The panel is cut at `session` before ranking, so a later row cannot reach
    the result even if the lake already holds tomorrow's bar.
    """
    params = dict(params or _params())
    members = _universe_members(params)
    closes, volumes, skipped = load_panels(members)
    if skipped:
        log.info("[a1_rank] %d pool members have no stored bars: %s",
                 len(skipped), skipped[:10])
    day = pd.Timestamp(str(session)).date()
    if day not in set(closes.index):
        raise KeyError(f"{day} is not a session in the stored panel "
                       f"({closes.index[0]}..{closes.index[-1]}); refresh the "
                       f"daily bars first")
    present = int(closes.loc[day].notna().sum())
    coverage = present / max(closes.shape[1], 1)
    if coverage < MIN_SESSION_COVERAGE:
        raise ValueError(
            f"{day} has bars for {present} of {closes.shape[1]} pool members "
            f"({coverage:.1%}, floor {MIN_SESSION_COVERAGE:.0%}); the daily "
            f"pass has not finished this session. Refusing to rank it: the "
            f"table would admit only the names that happen to be refreshed.")
    params["verified_tickers"] = verified_tickers(list(closes.columns))
    frame = _a1.rank_table(closes, volumes, day, params)
    frame["panel_as_of"] = day.isoformat()
    frame["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    frame["code_version"] = f"{_a1.STRATEGY_NAME}_v" \
        + _a1.STRATEGY_VERSION.replace(".", "_")
    return frame[list(RANK_COLUMNS)]


def write_table(session, frame: pd.DataFrame) -> Path:
    """Write one session's table atomically; a re-run replaces it in place."""
    path = a1_rank_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".writing")
    frame.to_parquet(tmp, index=False)
    tmp.replace(path)
    return path


def run(session=None, params: dict | None = None,
        force: bool = False) -> dict:
    """Build and store the ranking table for one session.

    Args:
        session: The already-closed session to rank. Defaults to the newest
            session present in the stored panel, which after a post-close
            update is the session that just finished.
        force: Rebuild even when the file already exists.
    """
    params = dict(params or _params())
    if session is None:
        members = _universe_members(params)
        closes, _volumes, _skipped = load_panels(members)
        session = latest_complete_session(closes)
    path = a1_rank_path(session)
    if path.is_file() and not force:
        log.info("[a1_rank] %s already exists; skipping", path.name)
        return {"session": str(session), "path": str(path), "written": False,
                "rows": 0, "eligible": 0}
    frame = build_table(session, params)
    write_table(session, frame)
    eligible = int(frame["eligible"].sum())
    log.info("[a1_rank] %s: %d candidates, %d eligible, %d ranked",
             session, len(frame), eligible, int(frame["rank"].notna().sum()))
    return {"session": str(session), "path": str(path), "written": True,
            "rows": len(frame), "eligible": eligible,
            "ranked": int(frame["rank"].notna().sum())}
