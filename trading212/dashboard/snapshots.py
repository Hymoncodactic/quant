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
    mark_gap(venue, reason)                 Record a sampling discontinuity.
    sample_files(venue)                     Existing per-day sample files.

Constants:
    SNAPSHOT_NAME  str  "live_snapshot.json".
    GAP_SECONDS    int  90. A spacing wider than this between consecutive
                        samples counts as a gap when charting, so a stopped
                        dashboard leaves a visible break rather than a
                        straight line through hours nobody observed. Chosen
                        just above the one-minute freshness ceiling the user
                        set for the live data.

Inputs:
    data/t212/dashboard/live_snapshot.json
    data/t212/dashboard/samples/YYYY-MM-DD.jsonl
Outputs:
    the same two paths

Change log:
    2026-08-22  Created.
"""

from __future__ import annotations

__all__ = ["write_snapshot", "read_snapshot", "append_sample", "read_samples",
           "mark_gap", "sample_files", "SNAPSHOT_NAME", "GAP_SECONDS"]

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.paths import dashboard_state_dir

SNAPSHOT_NAME = "live_snapshot.json"
GAP_SECONDS = 90


def _root(venue: str) -> Path:
    path = dashboard_state_dir(venue)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _samples_dir(venue: str) -> Path:
    path = _root(venue) / "samples"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_snapshot(venue: str, payload: dict[str, Any]) -> None:
    """Replace the latest snapshot atomically.

    The reader may run at any instant, so the file is never truncated in
    place: a partial read would show the browser a half-written account.
    """
    target = _root(venue) / SNAPSHOT_NAME
    tmp = target.with_suffix(".writing")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def read_snapshot(venue: str) -> dict[str, Any] | None:
    """Read the latest snapshot; None when nothing has been sampled yet."""
    target = _root(venue) / SNAPSHOT_NAME
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def append_sample(venue: str, sample: dict[str, Any]) -> None:
    """Append one tick to today's sample file, flushed to disk."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = _samples_dir(venue) / f"{day}.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def mark_gap(venue: str, reason: str) -> None:
    """Record that sampling stopped or resumed, so charts can show a break."""
    append_sample(venue, {"ts": datetime.now(timezone.utc).isoformat(),
                          "gap": True, "reason": reason})


def sample_files(venue: str) -> list[Path]:
    """Every per-day sample file, oldest first."""
    return sorted(_samples_dir(venue).glob("*.jsonl"))


def read_samples(venue: str, days: int = 7,
                 max_points: int = 1500) -> list[dict[str, Any]]:
    """Return recent samples, evenly downsampled to at most max_points.

    Downsampling happens here rather than in the browser so the response
    stays small no matter how long the dashboard has been running. Gap
    markers are always kept: they are what stops a chart from drawing a
    straight line across hours nobody observed.
    """
    files = sample_files(venue)[-days:]
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
    stride = max(1, len(normal) // max(1, max_points - len(gaps)))
    kept = normal[::stride]
    if normal and kept[-1] is not normal[-1]:
        kept.append(normal[-1])
    merged = kept + gaps
    merged.sort(key=lambda r: r.get("ts", ""))
    return merged
