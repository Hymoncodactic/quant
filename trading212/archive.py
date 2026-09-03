"""Bookkeeping archive: keep every record the venue will give us.

Responsibility: pull the account's own history from Trading 212 and append
it, unaltered, to append-only files under trading212/records/. Whatever the
API returns is what gets written -- no field is dropped, renamed or rounded,
because a record that has been "tidied" cannot later answer a question its
tidier did not anticipate.

Deduplication is by the venue's own identifiers, so a harvest can run as
often as it likes without growing the files. Each stream keeps a small index
of the identifiers already written; the walk stops as soon as it meets one,
which is what makes an incremental harvest cheap enough to run on a timer
against endpoints metered at six requests a minute.

Out of scope: deciding anything, which belongs to
trading212/execution/session_cycle.py; the strategy's own book, which
belongs to trading212/execution/shadow_ledger.py and answers a different
question (what does the STRATEGY own, versus what has the ACCOUNT done);
charting, which belongs to trading212/dashboard/.

Public functions:
    harvest_orders(client, root)        Historical orders with fills and taxes.
    harvest_transactions(client, root)  Cash movements.
    harvest_dividends(client, root)     Dividend payments.
    snapshot_positions(client, root)    One positions reading, raw.
    snapshot_account(client, root)      One account summary reading, raw.
    record_signals(root, payload)       One decision's targets and outcome.
    harvest_all(client, root, which)    Run several of the above, tolerantly.
    stream_path(root, name)             Path of one stream's file.
    read_stream(root, name, limit)      Recent rows of one stream.
    stream_stats(root)                  Row counts and last write per stream.

Constants:
    STREAMS       tuple  The append-only streams and the field each is keyed
                         by. "orders" is keyed by the FILL id rather than the
                         order id: one order can fill many times, and each
                         fill is a separate record with its own taxes.
    HISTORY_PAGES int    40. Upper bound on pages walked in one harvest, so a
                         first run against a long history cannot block the
                         caller indefinitely. Reaching it is logged.

Inputs:
    GET /api/v0/equity/history/orders, /history/transactions,
        /history/dividends, /equity/positions, /equity/account/summary
Outputs:
    trading212/records/orders.jsonl
    trading212/records/transactions.jsonl
    trading212/records/dividends.jsonl
    trading212/records/positions.jsonl
    trading212/records/account_summary.jsonl
    trading212/records/signals.jsonl
    trading212/records/a1_plan.jsonl
    trading212/records/b0_allocation.jsonl

Change log:
    2026-08-23  Created. The account owner asked for every record the API
                exposes to be kept, in full, beside the venue's code.
    2026-09-03  Two decision-side streams for B0: a1_plan (one row per A1
                rotation) and b0_allocation (one row per decided session).
                Landed ahead of the rest of B0 so the dashboard's
                /api/records whitelist accepts them before the execution
                layer starts writing them.
"""

from __future__ import annotations

__all__ = ["harvest_orders", "harvest_transactions", "harvest_dividends",
           "record_a1_plan", "record_b0_allocation",
           "snapshot_positions", "snapshot_account", "record_signals",
           "harvest_all", "stream_path", "read_stream", "stream_stats",
           "STREAMS", "HISTORY_PAGES"]

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from common.logging_setup import get_logger
from common.paths import records_dir

log = get_logger("t212.archive")

HISTORY_PAGES = 40

# stream name -> how a row is identified for deduplication. A stream whose
# key is None is a time series of readings, where every reading is new.
STREAMS: tuple[tuple[str, str | None], ...] = (
    ("orders", "fill_id"),
    ("transactions", "reference"),
    ("dividends", "reference"),
    ("positions", None),
    ("account_summary", None),
    ("signals", None),
    # B0 adds two decision-side streams. a1_plan is keyed by the rebalance
    # date because a rotation is decided once and must not double-record if
    # the session is replayed; b0_allocation is keyed by the decision date for
    # the same reason. Both are written by the execution layer, never
    # harvested from the venue (fixplans/t212/b0/00_coordination.md 5.1).
    ("a1_plan", "rebalance_date"),
    ("b0_allocation", "decision_date"),
)


# ============================================================================
# [1] Files
# ============================================================================

def stream_path(root: Path | None, name: str) -> Path:
    """Path of one stream's append-only file."""
    base = Path(root) if root is not None else records_dir("t212")
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{name}.jsonl"


def _append(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Append rows as JSON Lines, flushed to disk. Returns how many."""
    written = 0
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
        if written:
            handle.flush()
            os.fsync(handle.fileno())
    return written


def _known_keys(path: Path, key: str) -> set:
    """Identifiers already written to one stream."""
    if not path.exists():
        return set()
    seen = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue                    # a torn last line after a kill
            if key in row:
                seen.add(row[key])
    return seen


def read_stream(root: Path | None, name: str,
                limit: int = 200) -> list[dict[str, Any]]:
    """The most recent rows of one stream, newest first."""
    path = stream_path(root, name)
    if not path.exists():
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:][::-1]


def stream_stats(root: Path | None = None) -> dict[str, Any]:
    """Row count, byte size and last write time of every stream."""
    out: dict[str, Any] = {}
    for name, _key in STREAMS:
        path = stream_path(root, name)
        if not path.exists():
            out[name] = {"rows": 0, "bytes": 0, "last_write_utc": None}
            continue
        rows = sum(1 for line in path.open("r", encoding="utf-8")
                   if line.strip())
        stat = path.stat()
        out[name] = {"rows": rows, "bytes": stat.st_size,
                     "last_write_utc": datetime.fromtimestamp(
                         stat.st_mtime, timezone.utc).isoformat()}
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# [2] History streams
# ============================================================================

def _harvest_paged(client, root: Path | None, name: str, key: str,
                   walk: Callable[[], Iterable[dict[str, Any]]],
                   extract: Callable[[dict[str, Any]], list[dict[str, Any]]]
                   ) -> int:
    """Walk a history endpoint newest-first, stopping at the first known row.

    The venue returns newest first, so meeting a row already on disk means
    everything beyond it is on disk too. That turns a routine harvest into a
    single page rather than a full history walk.
    """
    path = stream_path(root, name)
    known = _known_keys(path, key)
    fresh: list[dict[str, Any]] = []
    stop = False
    for item in walk():
        for row in extract(item):
            if row[key] in known:
                stop = True
                break
            row["harvested_at_utc"] = _now()
            fresh.append(row)
        if stop:
            break
    fresh.reverse()                          # write oldest first
    written = _append(path, fresh)
    if written:
        log.info("[archive] %s: %d new rows", name, written)
    return written


def harvest_orders(client, root: Path | None = None) -> int:
    """Historical orders, one row per FILL, with the venue's own fields kept.

    One order can fill several times and each fill carries its own price,
    timestamp and itemized taxes, so the fill is the record worth keeping.
    The parent order travels with it rather than in a second file, so a row
    answers questions on its own.
    """
    def extract(item: dict[str, Any]) -> list[dict[str, Any]]:
        order = item.get("order") or {}
        fill = item.get("fill") or {}
        if fill.get("id") is None:
            return []
        return [{"fill_id": fill["id"], "order_id": order.get("id"),
                 "ticker": order.get("ticker"), "side": order.get("side"),
                 "order": order, "fill": fill}]

    return _harvest_paged(client, root, "orders", "fill_id",
                          lambda: client.iter_history_orders(
                              max_pages=HISTORY_PAGES),
                          extract)


def harvest_transactions(client, root: Path | None = None) -> int:
    """Cash movements: deposits, withdrawals, fees, interest."""
    def walk():
        page = client.history_transactions()
        for _ in range(HISTORY_PAGES):
            for item in page.get("items", []):
                yield item
            nxt = page.get("nextPagePath")
            if not nxt:
                return
            # This endpoint returns the query string alone; see
            # T212Client.follow_page for the measurement.
            page = client.follow_page(
                "history_orders", nxt,
                base_path="/api/v0/equity/history/transactions")

    def extract(item):
        ref = item.get("reference")
        return [] if ref is None else [{"reference": ref, "record": item}]

    return _harvest_paged(client, root, "transactions", "reference",
                          walk, extract)


def harvest_dividends(client, root: Path | None = None) -> int:
    """Dividend payments, with the per-share and account-currency amounts."""
    def walk():
        page = client.history_dividends()
        for _ in range(HISTORY_PAGES):
            for item in page.get("items", []):
                yield item
            nxt = page.get("nextPagePath")
            if not nxt:
                return
            page = client.follow_page(
                "history_orders", nxt,
                base_path="/api/v0/equity/history/dividends")

    def extract(item):
        ref = item.get("reference")
        return [] if ref is None else [{"reference": ref, "record": item}]

    return _harvest_paged(client, root, "dividends", "reference",
                          walk, extract)


# ============================================================================
# [3] Point-in-time readings
# ============================================================================

def snapshot_positions(client, root: Path | None = None) -> int:
    """Append one reading of every open position, exactly as returned."""
    positions = client.positions()
    return _append(stream_path(root, "positions"),
                   [{"at_utc": _now(), "positions": positions}])


def snapshot_account(client, root: Path | None = None) -> int:
    """Append one reading of the account summary, exactly as returned."""
    return _append(stream_path(root, "account_summary"),
                   [{"at_utc": _now(), "summary": client.account_summary()}])


def record_signals(root: Path | None, payload: dict[str, Any]) -> int:
    """Append one decision: what the strategy wanted and what happened.

    Written by the execution layer at every decision, including the ones
    that placed no order. A day with no orders is itself a fact about the
    strategy, and only this record preserves it.
    """
    row = {"at_utc": _now(), **payload}
    return _append(stream_path(root, "signals"), [row])


def record_a1_plan(root: Path | None, payload: dict[str, Any]) -> int:
    """Append one A1 rotation: the book that was decided and what changed.

    Keyed by rebalance_date: replaying the identical decision is a no-op, and
    a decision retaken on better data supersedes the earlier row.
    This stream is the ONLY memory of the previous book: the buffer band is
    defined against it, and positions are not a substitute because a rejected
    order leaves them disagreeing with the plan.
    """
    row = {"at_utc": _now(), **payload}
    return _append_keyed(stream_path(root, "a1_plan"), [row], "rebalance_date")


def record_b0_allocation(root: Path | None, payload: dict[str, Any]) -> int:
    """Append one session's capital split between the A0 and A1 legs."""
    row = {"at_utc": _now(), **payload}
    return _append_keyed(stream_path(root, "b0_allocation"), [row],
                         "decision_date")


def _append_keyed(path: Path, rows: list[dict[str, Any]], key: str) -> int:
    """Append a row unless an identical one for the same key is already newest.

    Supersede, not first-write-wins. These streams record a DECISION, and a
    decision can legitimately be retaken within the same session: the first
    attempt may abort before submitting and a later attempt may reach a
    different answer on refreshed data. First-write-wins froze the earlier,
    worse-informed row forever and left no way to correct it.

    Replay safety is kept by comparing content: re-recording the identical
    decision is a no-op, so an idempotent retry still does not duplicate. The
    file stays append-only, so the supersede history is auditable; every
    reader takes the NEWEST row for a key (read_stream returns newest first).
    """
    newest = _newest_by_key(path, key)
    fresh = []
    for row in rows:
        previous = newest.get(row.get(key))
        if previous is not None and _same_decision(previous, row):
            continue
        fresh.append(row)
    return _append(path, fresh)


def _same_decision(previous: dict[str, Any], row: dict[str, Any]) -> bool:
    """Whether two rows record the same decision, ignoring the write stamp."""
    drop = {"at_utc"}
    return {k: v for k, v in previous.items() if k not in drop} \
        == {k: v for k, v in row.items() if k not in drop}


def _newest_by_key(path: Path, key: str) -> dict[Any, dict[str, Any]]:
    """The most recent row per key value in one stream."""
    out: dict[Any, dict[str, Any]] = {}
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if key in row:
                out[row[key]] = row
    return out


def harvest_all(client, root: Path | None = None,
                which: Iterable[str] | None = None) -> dict[str, Any]:
    """Run several harvests, letting each fail on its own.

    One unreachable endpoint must not cost the others their run, so every
    stream is attempted and its outcome reported separately.
    """
    jobs = {
        "orders": lambda: harvest_orders(client, root),
        "transactions": lambda: harvest_transactions(client, root),
        "dividends": lambda: harvest_dividends(client, root),
        "positions": lambda: snapshot_positions(client, root),
        "account_summary": lambda: snapshot_account(client, root),
    }
    names = list(which) if which is not None else list(jobs)
    out: dict[str, Any] = {}
    for name in names:
        job = jobs.get(name)
        if job is None:
            out[name] = {"ok": False, "reason": "unknown_stream"}
            continue
        try:
            out[name] = {"ok": True, "written": job()}
        except Exception as exc:
            out[name] = {"ok": False, "reason": repr(exc)[:200]}
            log.warning("[archive] %s failed: %r", name, exc)
    return out
