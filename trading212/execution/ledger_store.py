"""Ledger persistence: file layout, load-time integrity, atomic writes.

Responsibility: everything about HOW the shadow ledger's journal and
snapshot touch the disk -- paths, the write-ahead append (fsync before the
snapshot moves), the atomic snapshot replace, and the load-time integrity
rules that decide whether a book on disk may be trusted.
Not responsible for: any bookkeeping semantics (shadow_ledger.py owns what
events mean).

Integrity rules enforced at load (borrowed from the QMT reference ledger):
    - Journal present but snapshot missing or unreadable: REFUSE. Rebuilding
      from an empty base would erase real exposure silently (its
      V6.8.21-FR4); the journal holds every event needed for manual repair.
    - Snapshot identity (strategy id, schema version) must match.
    - Journal tail ahead of the snapshot's idempotency table: REFUSE. The
      appender fsyncs the journal line BEFORE replacing the snapshot and the
      process is single-threaded, so at most the LAST journal event can be
      missing; loading past it would silently drop that event (for example
      an ambiguity freeze).

Public classes:
    LedgerFrozenError                     Book refuses to load or mutate

Public functions:
    journal_path(state_dir, strategy_id)
    snapshot_path(state_dir, strategy_id)
    read_snapshot(state_dir, strategy_id, schema_version)
    append_event(state_dir, strategy_id, record)
    write_snapshot(state_dir, strategy_id, snap)
"""

from __future__ import annotations

__all__ = ["LedgerFrozenError", "journal_path", "snapshot_path",
           "read_snapshot", "append_event", "write_snapshot"]

import json
import os
from pathlib import Path
from typing import Any


class LedgerFrozenError(RuntimeError):
    """The book cannot accept new exposure until a human repairs it."""


def journal_path(state_dir: Path, strategy_id: str) -> Path:
    return state_dir / f"{strategy_id}_journal.jsonl"


def snapshot_path(state_dir: Path, strategy_id: str) -> Path:
    return state_dir / f"{strategy_id}_snapshot.json"


def read_snapshot(state_dir: Path, strategy_id: str,
                  schema_version: int) -> dict[str, Any]:
    """Load and validate one book's snapshot per the integrity rules above.

    Raises:
        FileNotFoundError: Neither file exists (init_fresh is the remedy).
        LedgerFrozenError: Any integrity rule fails.
    """
    journal = journal_path(state_dir, strategy_id)
    snapshot = snapshot_path(state_dir, strategy_id)
    if not snapshot.exists() and not journal.exists():
        raise FileNotFoundError(
            f"no ledger for {strategy_id} under {state_dir}; run the "
            f"init command with an explicit cash allocation")
    if not snapshot.exists():
        raise LedgerFrozenError(
            f"journal {journal} exists but snapshot {snapshot} is missing; "
            f"refusing to rebuild from an empty base -- restore the snapshot "
            f"from the journal, then retry")
    try:
        snap = json.loads(snapshot.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerFrozenError(f"snapshot {snapshot} unreadable: {exc}") from exc
    if snap.get("schema_version") != schema_version or \
            snap.get("strategy_id") != strategy_id:
        raise LedgerFrozenError(
            f"snapshot {snapshot} identity mismatch: "
            f"{snap.get('strategy_id')!r} v{snap.get('schema_version')!r}")
    _assert_journal_not_ahead(journal, snap)
    return snap


def append_event(state_dir: Path, strategy_id: str,
                 record: dict[str, Any]) -> None:
    """Append one journal line and fsync it before the caller moves the
    snapshot; the fsync ordering is what makes a crash detectable."""
    state_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with open(journal_path(state_dir, strategy_id), "a",
              encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_snapshot(state_dir: Path, strategy_id: str,
                   snap: dict[str, Any]) -> None:
    """Atomically replace the snapshot; the good copy is never pre-deleted."""
    target = snapshot_path(state_dir, strategy_id)
    tmp = target.with_suffix(".writing")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(target)


def _assert_journal_not_ahead(journal: Path, snap: dict[str, Any]) -> None:
    """Refuse a snapshot whose journal tail it has never seen."""
    if not journal.exists():
        return
    last_line = ""
    with open(journal, "rb") as handle:
        for raw in handle:
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                last_line = text
    if not last_line:
        return
    try:
        event_id = json.loads(last_line).get("event_id")
    except json.JSONDecodeError as exc:
        raise LedgerFrozenError(
            f"journal {journal} tail is not valid JSON ({exc}); the last "
            f"write was torn -- inspect and repair manually") from exc
    if event_id and event_id not in snap.get("applied_event_ids", {}):
        raise LedgerFrozenError(
            f"journal event {event_id!r} is ahead of the snapshot (crash "
            f"between journal append and snapshot replace); re-apply it "
            f"manually, then reload")
