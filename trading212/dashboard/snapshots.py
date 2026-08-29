"""Dashboard persistence: the latest snapshot and the append-only samples.

Responsibility: write and read the two things the dashboard charts -- one
"latest" snapshot replaced atomically on every tick, and one append-only
JSON Lines file per calendar day holding the tick history. Reading is
downsampled on the way out, so a browser never receives more points than a
chart can show.

Sampling is deliberately lossy across downtime: nothing samples while the
dashboard is stopped, so those seconds simply do not exist. What survives a
stop is the STRATEGY state, because that lives in the execution ledger and
the venue's bills, not here. rebuild_gap_marker() records where a gap begins
so the chart can draw it instead of interpolating across it.

Out of scope: producing the numbers, which belongs to collector.py; the
execution ledger itself, which belongs to
trading212/execution/shadow_ledger.py.

Public functions:
    write_snapshot(venue, payload)          Replace the latest snapshot.
    read_snapshot(venue)                    Read it, or None when absent.
    append_sample(venue, sample)            Append one tick to today's file.
    read_samples(venue, days, max_points)   Downsampled tick history.
    update_rollup(venue, sample)            Fold a tick into today's daily row.
    read_rollup(venue)                      Every daily row, oldest first.
    mark_gap(venue, reason)                 Record a sampling discontinuity.
    sample_files(venue, env)                     Existing per-day sample files.

Constants:
    SNAPSHOT_NAME  str  "live_snapshot.json".
    GAP_SECONDS    int  90. A spacing wider than this between consecutive
                        samples counts as a gap when charting, so a stopped
                        dashboard leaves a visible break rather than a
                        straight line through hours nobody observed. Chosen
                        just above the one-minute freshness ceiling the user
                        set for the live data.

Inputs:
    trading212/records/equity/live_snapshot.json
    trading212/records/equity/samples/YYYY-MM-DD.jsonl
    trading212/records/equity/daily.jsonl
Outputs:
    the same three paths

Change log:
    2026-08-22  Created.
    2026-08-23  Moved under trading212/records/ at the account owner's
                request, and gained the daily rollup that makes a
                multi-year range readable without touching the ticks.
"""

from __future__ import annotations

__all__ = ["write_snapshot", "read_snapshot", "append_sample", "read_samples",
           "update_rollup", "read_rollup", "mark_gap", "sample_files",
           "SNAPSHOT_NAME", "GAP_SECONDS"]

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.paths import records_dir

SNAPSHOT_NAME = "live_snapshot.json"
GAP_SECONDS = 90


def _root(venue: str, env: str = "live") -> Path:
    path = records_dir(venue, env) / "equity"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _samples_dir(venue: str, env: str = "live") -> Path:
    path = _root(venue, env) / "samples"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_snapshot(venue: str, payload: dict[str, Any], env: str = "live") -> None:
    """Replace the latest snapshot atomically.

    The reader may run at any instant, so the file is never truncated in
    place: a partial read would show the browser a half-written account.
    """
    target = _root(venue, env) / SNAPSHOT_NAME
    tmp = target.with_suffix(".writing")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def read_snapshot(venue: str, env: str = "live") -> dict[str, Any] | None:
    """Read the latest snapshot; None when nothing has been sampled yet."""
    target = _root(venue, env) / SNAPSHOT_NAME
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def append_sample(venue: str, sample: dict[str, Any], env: str = "live") -> None:
    """Append one tick to today's sample file, flushed to disk."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = _samples_dir(venue, env) / f"{day}.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _rollup_path(venue: str, env: str = "live") -> Path:
    return _root(venue, env) / "daily.jsonl"


def update_rollup(venue: str, sample: dict[str, Any], env: str = "live") -> None:
    """Fold one tick into today's daily row.

    A per-tick series answers "what happened this afternoon" and is hopeless
    at "what happened over ten years": at one sample every few seconds a
    decade is tens of millions of points, which no browser will draw and no
    reader wants. The daily row is the same series at the resolution a long
    range is actually read at, so a long range costs one small file instead
    of a decade of ticks.

    The file is rewritten in place because only its last row changes; it
    holds one row per day, so even a decade of it is a few hundred
    kilobytes.
    """
    equity = sample.get("equity_gbp")
    if equity is None:
        return                               # nothing priced yet; no row
    day = str(sample.get("ts", ""))[:10]
    if not day:
        return
    path = _rollup_path(venue, env)
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    row = rows[-1] if rows and rows[-1].get("day") == day else None
    if row is None:
        row = {"day": day, "open": equity, "high": equity, "low": equity,
               "close": equity, "ticks": 0}
        rows.append(row)
    row["high"] = max(row["high"], equity)
    row["low"] = min(row["low"], equity)
    row["close"] = equity
    row["ticks"] = row.get("ticks", 0) + 1
    for field in ("cash_gbp", "holdings_gbp", "account_total"):
        if sample.get(field) is not None:
            row[field] = sample[field]
    row["last_ts"] = sample.get("ts")
    tmp = path.with_suffix(".writing")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
                   + "\n", encoding="utf-8")
    tmp.replace(path)


def read_rollup(venue: str, env: str = "live") -> list[dict[str, Any]]:
    """Every daily row, oldest first."""
    path = _rollup_path(venue, env)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def mark_gap(venue: str, reason: str, env: str = "live") -> None:
    """Record that sampling stopped or resumed, so charts can show a break."""
    append_sample(venue, {"ts": datetime.now(timezone.utc).isoformat(),
                          "gap": True, "reason": reason})


def sample_files(venue: str, env: str = "live") -> list[Path]:
    """Every per-day sample file, oldest first."""
    return sorted(_samples_dir(venue, env).glob("*.jsonl"))


def read_samples(venue: str, days: int = 7, env: str = "live",
                 max_points: int = 1500) -> list[dict[str, Any]]:
    """Return recent samples, evenly downsampled to at most max_points.

    Downsampling happens here rather than in the browser so the response
    stays small no matter how long the dashboard has been running. Gap
    markers are always kept: they are what stops a chart from drawing a
    straight line across hours nobody observed.
    """
    files = sample_files(venue, env)[-days:]
    rows: list[dict[str, Any]] = []
    for path in files:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn last line after a hard kill
        except OSError:
            continue
    if len(rows) <= max_points:
        return rows
    gaps = [r for r in rows if r.get("gap")]
    normal = [r for r in rows if not r.get("gap")]
    budget = max(1, max_points - len(gaps))
    stride = max(1, -(-len(normal) // budget))     # ceiling; see api._downsample
    kept = normal[::stride]
    if normal and kept[-1] is not normal[-1]:
        kept.append(normal[-1])
    merged = kept + gaps
    merged.sort(key=lambda r: r.get("ts", ""))
    return merged
