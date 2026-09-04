"""Read-side cumulative profit and loss for dashboard history rows.

Responsibility: derive net allocated capital from the active shadow-ledger
lineage and add cumulative PnL to already-marked equity rows. Capital added or
removed is excluded from performance, and BOOK_ADOPTED continues the source
book's capital history instead of resetting PnL to zero.

Out of scope: pricing positions, which belongs to collector.py; mutating or
validating the shadow ledger, which belongs to trading212/execution/; strategy
leg attribution, which belongs to the B0 allocation records.

Public functions:
    add_cumulative_pnl(rows, state_dir, strategy_id)  Enrich chart rows and
                                                       return caliber metadata.

Constants: None.

Inputs:
    data/t212/execution_state[_paper]/*_journal.jsonl[.retired-*].
Outputs:
    None. Input rows and ledger files are never mutated.

Change log:
    2026-09-04  Created for the cumulative-PnL dashboard chart.
"""

from __future__ import annotations

__all__ = ["add_cumulative_pnl"]

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading212.execution.ledger_store import journal_path


class _CapitalHistoryError(ValueError):
    """The active book's net invested capital cannot be proved."""


def add_cumulative_pnl(
        rows: list[dict[str, Any]], state_dir: Path | None,
        strategy_id: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Add cumulative PnL in GBP, net of documented capital flows.

    Each priced row is calculated as marked strategy equity minus the sum of
    INIT and ALLOCATION_CHANGED amounts effective at that row's timestamp.
    BOOK_ADOPTED follows the source book's latest retired journal before the
    adoption event, preserving the strategy's economic starting point.

    Args:
        rows: Oldest-first chart rows. ``equity_gbp`` is GBP marked equity.
        state_dir: Environment-specific shadow-ledger directory.
        strategy_id: Active strategy book identifier.

    Returns:
        A copied row list with ``cumulative_pnl_gbp`` floats or nulls, plus
        metadata describing whether the capital history was available.
    """
    copied = [dict(row) for row in rows]
    if state_dir is None or not strategy_id:
        return _unavailable(copied, "ledger_identity_unavailable")
    try:
        active = journal_path(Path(state_dir), strategy_id)
        events = _capital_events(active, Path(state_dir), set())
    except (OSError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
        return _unavailable(copied, type(exc).__name__)
    if not events:
        return _unavailable(copied, "capital_events_unavailable")

    events.sort(key=lambda item: item[0])
    for row in copied:
        pnl_ts = row.pop("_pnl_ts", None) or row.get("ts")
        equity = row.get("equity_gbp")
        cash = row.get("cash_gbp")
        holdings = row.get("holdings_gbp")
        if (row.get("gap") or equity is None or cash is None
                or holdings is None or not pnl_ts):
            row["cumulative_pnl_gbp"] = None
            continue
        try:
            at = _timestamp(pnl_ts)
            effective = [delta for event_at, delta in events
                         if event_at <= at]
            if not effective:
                row["cumulative_pnl_gbp"] = None
                continue
            value = Decimal(str(equity)) - sum(effective, Decimal("0"))
            row["cumulative_pnl_gbp"] = float(value)
        except (ValueError, InvalidOperation):
            row["cumulative_pnl_gbp"] = None

    net_allocated = sum((delta for _at, delta in events), Decimal("0"))
    return copied, {
        "ok": True,
        "basis": "marked_equity_minus_net_allocated_capital",
        "capital_start_utc": events[0][0].isoformat(),
        "capital_events": len(events),
        "net_allocated_gbp": float(net_allocated),
    }


def _unavailable(rows: list[dict[str, Any]],
                 problem: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return explicit null PnL instead of guessing a missing capital basis."""
    for row in rows:
        row.pop("_pnl_ts", None)
        row["cumulative_pnl_gbp"] = None
    return rows, {"ok": False, "problem": problem}


def _capital_events(path: Path, state_dir: Path,
                    seen: set[Path]) -> list[tuple[datetime, Decimal]]:
    """Resolve one journal and its adopted ancestors into capital deltas."""
    resolved = path.resolve()
    if resolved in seen:
        raise _CapitalHistoryError("book adoption lineage contains a cycle")
    if not path.exists():
        raise _CapitalHistoryError("active journal is missing")
    seen = set(seen)
    seen.add(resolved)
    records = _read_journal(path)
    starters = [row for row in records
                if row.get("event_type") in ("INIT", "BOOK_ADOPTED")]
    if len(starters) != 1:
        raise _CapitalHistoryError("journal needs exactly one start event")

    start = starters[0]
    start_at = _timestamp(start["ts_utc"])
    out: list[tuple[datetime, Decimal]] = []
    if start["event_type"] == "INIT":
        amount = (start.get("payload") or {}).get("allocated_cash_gbp")
        out.append((start_at, Decimal(str(amount))))
    else:
        source_id = str((start.get("payload") or {})
                        .get("from_strategy_id") or "")
        if not source_id:
            raise _CapitalHistoryError("BOOK_ADOPTED has no source strategy")
        source = _source_journal(state_dir, source_id, start_at)
        out.extend(_capital_events(source, state_dir, seen))

    for record in records:
        if record.get("event_type") != "ALLOCATION_CHANGED":
            continue
        payload = record.get("payload") or {}
        out.append((_timestamp(record["ts_utc"]),
                    Decimal(str(payload.get("delta_gbp")))))
    return out


def _source_journal(state_dir: Path, strategy_id: str,
                    before: datetime) -> Path:
    """Choose the latest source journal whose last event predates adoption."""
    base = journal_path(state_dir, strategy_id)
    candidates = ([base] if base.exists() else []) + sorted(
        state_dir.glob(f"{base.name}.retired-*"))
    eligible: list[tuple[datetime, Path]] = []
    for candidate in candidates:
        records = _read_journal(candidate)
        if not records:
            continue
        last_at = max(_timestamp(row["ts_utc"]) for row in records)
        if last_at <= before:
            eligible.append((last_at, candidate))
    if not eligible:
        raise _CapitalHistoryError("adopted source journal is unavailable")
    return max(eligible, key=lambda item: item[0])[1]


def _read_journal(path: Path) -> list[dict[str, Any]]:
    """Read a complete JSONL journal; malformed records are not ignored."""
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def _timestamp(value: Any) -> datetime:
    """Parse a ledger or sample timestamp and normalize it to UTC."""
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
