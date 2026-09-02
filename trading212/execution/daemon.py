"""Long-running scheduler: start any time, trade every session, unattended.

Responsibility: keep one process alive that knows where "now" sits inside
the exchange timetable and drives the existing one-shot phases at the right
instants -- decide inside the 15:30-15:59 New York window, settle shortly
after the close -- day after day, with no human at the keyboard. Starting
it at 09:50, at midnight or mid-window must all behave identically:
compute the next action, sleep until it is due, do it, repeat.

Arming: the daemon re-reads the venue configuration before every phase, so
flipping execution.dry_run on the dashboard takes effect at the very next
cycle without a restart. A cycle runs armed exactly when dry_run is false
-- the dashboard switch is the single control, per the account owner's
explicit ruling (2026-08-31); there is no per-run flag here.

Out of scope: what a cycle does (session_cycle.py owns decide/settle);
market timetables (instruments.py); the dashboard process that starts and
stops this one (trading212/dashboard/).

Locking, two levels:
    daemon.lock   Held for the whole run; one daemon per environment.
    run_a0.lock   The execution lock, held ONLY around decide/settle, so
                  dashboard ledger operations work while the daemon idles
                  and one-shot CLI runs are excluded only mid-phase.

Public functions:
    run(cfg)                 The daemon loop; returns only on stop signal.
    plan_next(...)           Pure: what to do next and when (unit-tested).
    read_status(state_dir)   Parse the status file, for the dashboard.

Constants:
    SETTLE_DELAY_SEC     float  90. Close to settle-start: fills land within
                                seconds, one poll interval of slack.
    HEARTBEAT_SEC        float  30. Status-file refresh cadence while idle.
    RETRY_SEC            float  60. Backoff after a failed decide attempt,
                                bounded anyway by the 29-minute window.
    SETTLE_RETRY_SEC     float  300. Backoff after a failed settle: it can
                                stay failed for hours (frozen book awaiting
                                a human), and each retry spends metered
                                venue requests.
    CALENDAR_REFRESH_SEC float  21600. Re-fetch the venue timetable every
                                6 hours; it publishes about 6 weeks ahead.

Inputs:
    trading212/config/t212.<env>.yaml (re-read every loop);
    data/t212/execution_state[_<env>]/ (locks, status, halt, ledger).
Outputs:
    daemon_status.json beside the ledger (atomic writes);
    log lines under logs/; desktop notifications on critical events.

Change log:
    2026-08-31  Created: the account owner ruled that watching the clock is
                the machine's job, and the dashboard is the only manual
                control surface.
"""

from __future__ import annotations

__all__ = ["run", "plan_next", "read_status",
           "SETTLE_DELAY_SEC", "HEARTBEAT_SEC", "RETRY_SEC", "SETTLE_RETRY_SEC",
           "CALENDAR_REFRESH_SEC"]

import fcntl
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd

from common.alerts import notify
from common.config import load_config
from common.logging_setup import get_logger
from common.paths import execution_state_dir
from trading212.client import T212Client
from trading212.execution import instruments, session_cycle

log = get_logger("t212.execution")

SETTLE_DELAY_SEC = 90.0
HEARTBEAT_SEC = 30.0
RETRY_SEC = 60.0
SETTLE_RETRY_SEC = 300.0
CALENDAR_REFRESH_SEC = 6 * 3600.0


# ============================================================================
# [1] Pure planning
# ============================================================================

def plan_next(sessions: list, now: pd.Timestamp, decided_session: str | None,
              settled_sessions: set[str], submit_lead_sec: int,
              settle_delay_sec: float = SETTLE_DELAY_SEC) -> dict[str, Any]:
    """What the daemon should do next, and when.

    Pure function of the timetable and the two progress marks (which session
    was last decided, which sessions were settled), so every branch is
    unit-testable without a clock or a venue.

    Returns {action, at, session_date} where action is one of:
        wait_decide   Sleep until `at` (the session's decision key).
        decide        The decision window is open now.
        wait_settle   Sleep until `at` (close + settle delay).
        settle        The settle instant has passed and is not yet done.
        idle          No upcoming session in the timetable; retry at `at`.
    Half-day sessions are skipped entirely: the ruling takes no decision
    without a 15:30 bar, and with no decision there is nothing to settle.
    """
    for sess in sessions:
        sid = str(sess.date_ny)
        if not sess.is_full:
            continue
        settle_at = sess.close_utc + pd.Timedelta(seconds=settle_delay_sec)
        if now >= settle_at:
            if sid in settled_sessions:
                continue
            # A session that ended before the daemon ever saw it needs no
            # settle of its own -- there is nothing of ours in it -- unless
            # it was the decided one, which the decided check below catches.
            if decided_session != sid:
                continue
            return {"action": "settle", "at": now, "session_date": sid}

        key = instruments.decision_key(sess)
        submit_at = sess.close_utc - pd.Timedelta(seconds=submit_lead_sec)
        if decided_session == sid or now > submit_at:
            # Decision handled or missed; what remains is the settle. The
            # now >= settle_at case returned above, so here settle_at is
            # still ahead. A missed window settles too: nothing of ours was
            # submitted, but the reconcile it runs is cheap hygiene.
            if sid in settled_sessions:
                continue
            return {"action": "wait_settle", "at": settle_at,
                    "session_date": sid}
        if now < key:
            return {"action": "wait_decide", "at": key, "session_date": sid}
        return {"action": "decide", "at": now, "session_date": sid}

    horizon = now + pd.Timedelta(seconds=CALENDAR_REFRESH_SEC)
    return {"action": "idle", "at": horizon, "session_date": None}


# ============================================================================
# [2] Status file
# ============================================================================

def _status_path(state_dir: Path) -> Path:
    return state_dir / "daemon_status.json"


def read_status(state_dir: Path) -> dict[str, Any] | None:
    """The last written daemon status, or None when absent or unreadable."""
    path = _status_path(state_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class _Daemon:
    """One daemon run: locks, stop flag, progress marks, status writes."""

    def __init__(self, venue: str, env: str) -> None:
        self.venue = venue
        self.env = env
        self.state_dir = execution_state_dir(venue, env)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.stop = threading.Event()
        self.decided_session: str | None = None
        self.settled_sessions: set[str] = set()
        self.last_decide: dict[str, Any] | None = None
        self.last_settle: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._daemon_lock_handle = None
        self._calendar: list[dict] | None = None
        self._calendar_at = 0.0
        self._notified: set[str] = set()
        self._abort_streak = 0
        self.started_at = pd.Timestamp.now(tz="UTC")

    def _notify_once(self, key: str, title: str, message: str) -> None:
        """Desktop-notify the first occurrence of one failure key only.

        An abort inside the window retries every minute; without this a
        single stuck condition rings the owner's phone thirty times.
        """
        if key in self._notified:
            return
        self._notified.add(key)
        notify(title, message)

    # -- locks ----------------------------------------------------------

    def acquire_daemon_lock(self) -> bool:
        handle = open(self.state_dir / "daemon.lock", "a+", encoding="utf-8")
        # A short retry rides out the dashboard's momentary liveness probes
        # of this same lock; a real second daemon holds it forever and still
        # fails every attempt.
        for attempt in range(5):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == 4:
                    handle.close()
                    return False
                time.sleep(0.4)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        self._daemon_lock_handle = handle  # held for the process lifetime
        return True

    def _hold_execution_lock(self, deadline_sec: float = 60.0):
        """Take run_a0.lock for one phase, waiting out brief holders.

        The dashboard's ledger routes hold it for fractions of a second and
        a one-shot CLI for at most one phase; waiting up to a minute rides
        both out. Returns the open handle, or None when the deadline passes.
        """
        handle = open(self.state_dir / "run_a0.lock", "a+", encoding="utf-8")
        waited = 0.0
        while not self.stop.is_set():
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle.seek(0)
                handle.truncate()
                handle.write(f"pid={os.getpid()} holder=daemon\n")
                handle.flush()
                return handle
            except BlockingIOError:
                if waited >= deadline_sec:
                    handle.close()
                    return None
                self.stop.wait(1.0)
                waited += 1.0
        handle.close()
        return None

    # -- status ---------------------------------------------------------

    def write_status(self, phase: str, detail: str,
                     next_at: pd.Timestamp | None,
                     session_date: str | None, dry_run: bool | None) -> None:
        payload = {
            "pid": os.getpid(), "env": self.env, "phase": phase,
            "detail": detail,
            "session_date": session_date,
            "next_action_utc": str(next_at) if next_at is not None else None,
            "dry_run": dry_run,
            "decided_session": self.decided_session,
            "last_decide": self.last_decide,
            "last_settle": self.last_settle,
            "last_error": self.last_error,
            "updated_at_utc": str(pd.Timestamp.now(tz="UTC")),
        }
        # The rotation counters, read from the cache the decision writes. They
        # are shown, never used to decide: the truth is the session list and
        # the anchor, both recomputed every session (04_execution.md 9).
        payload.update(_rotation_fields(self.state_dir))
        path = _status_path(self.state_dir)
        tmp = path.with_suffix(".writing")
        tmp.write_text(json.dumps(payload, indent=1, default=str),
                       encoding="utf-8")
        tmp.replace(path)

    # -- timetable ------------------------------------------------------

    def sessions(self, cfg: dict[str, Any]) -> list:
        """Cached venue timetable, re-fetched every CALENDAR_REFRESH_SEC."""
        age = time.monotonic() - self._calendar_at
        if self._calendar is None or age > CALENDAR_REFRESH_SEC:
            cycle = session_cycle._Cycle(cfg)
            self._calendar = instruments.refresh_calendar(
                cycle.client, cycle.calendar_cache)
            self._calendar_at = time.monotonic()
            cycle.client.close()
        events = instruments.session_events(
            self._calendar, instruments.US_SCHEDULE_ID_NASDAQ)
        return instruments.sessions(events)

    # -- phases ---------------------------------------------------------

    def run_decide(self, cfg: dict[str, Any], session_date: str) -> None:
        dry_run = bool((cfg.get("execution") or {}).get("dry_run", True))
        armed = not dry_run
        handle = self._hold_execution_lock()
        if handle is None:
            self.last_error = "execution lock unavailable for decide"
            log.error("[daemon] %s", self.last_error)
            return
        try:
            log.info("[daemon] decide starting for %s armed=%s dry_run=%s",
                     session_date, armed, dry_run)
            result = session_cycle.decide(cfg, armed=armed,
                                          stop_check=self.stop.is_set)
        except Exception as exc:
            self.last_error = f"decide raised: {exc!r}"[:300]
            log.critical("[daemon] %s", self.last_error)
            notify(f"{_label(cfg)} daemon: decide failed",
                   f"{session_date}: {exc!r}"[:180])
            return
        finally:
            handle.close()

        self.last_decide = {"session": session_date, "result": result,
                            "at_utc": str(pd.Timestamp.now(tz="UTC"))}
        reason = str(result.get("reason", ""))
        if result.get("aborted"):
            self.last_error = f"decide aborted: {reason}"[:300]
            log.warning("[daemon] %s", self.last_error)
            if "already decided" in reason:
                self.decided_session = session_date
                return
            self._abort_streak += 1
            if armed:
                self._notify_once(f"decide:{session_date}:{reason[:60]}",
                                  f"{_label(cfg)} daemon: decide aborted",
                                  f"{session_date}: {reason[:150]}")
            # The loop retries while the window is open; the streak widens
            # the retry below so a stuck condition does not hammer the
            # venue thirty times in one window.
            return
        self.decided_session = session_date
        self.last_error = None
        self._abort_streak = 0
        submitted = str(result.get("submit", ""))
        log.info("[daemon] decide done for %s: %s", session_date, submitted)
        if armed:
            notify(f"{_label(cfg)} orders submitted",
                   f"{session_date}: {submitted[:150]}")

    def run_settle(self, cfg: dict[str, Any], session_date: str) -> None:
        handle = self._hold_execution_lock()
        if handle is None:
            self.last_error = "execution lock unavailable for settle"
            log.error("[daemon] %s", self.last_error)
            return
        try:
            result = session_cycle.settle(cfg,
                                          stop_check=self.stop.is_set)
        except Exception as exc:
            self.last_error = f"settle raised: {exc!r}"[:300]
            log.critical("[daemon] %s", self.last_error)
            notify(f"{_label(cfg)} daemon: settle failed",
                   f"{session_date}: {exc!r}"[:180])
            return
        finally:
            handle.close()
        self.last_settle = {"session": session_date, "result": result,
                            "at_utc": str(pd.Timestamp.now(tz="UTC"))}
        clean = (not result.get("aborted")
                 and not result.get("still_open")
                 and not result.get("frozen"))
        if not clean:
            reason = str(result.get("reason")
                         or f"still_open={result.get('still_open')} "
                            f"frozen={result.get('frozen')}")[:300]
            self.last_error = f"settle incomplete: {reason}"
            log.error("[daemon] %s", self.last_error)
            self._notify_once(f"settle:{session_date}",
                              f"{_label(cfg)} daemon: settle incomplete",
                              f"{session_date}: {reason[:150]}")
            return
        self.settled_sessions.add(session_date)
        self.last_error = None
        log.info("[daemon] settle done for %s: %s", session_date,
                 result.get("settle"))

    def ledger_needs_settle(self, cfg: dict[str, Any]) -> bool:
        """Whether the book carries loose ends only a settle can resolve.

        Checked continuously, not just at boot: a decide that crashed after
        submitting leaves open orders with NO session marked decided, and a
        schedule keyed on that mark alone would never settle them (found in
        review). An unreadable ledger counts as needing settle -- settle is
        where the failure gets surfaced, retried and alerted, instead of
        being shrugged off here.
        """
        try:
            journal = self.state_dir / "a0_v0_0_1_journal.jsonl"
            if not journal.exists() and not any(
                    self.state_dir.glob("*_journal.jsonl")):
                return False  # no book at all: nothing to settle
            cycle = session_cycle._Cycle(cfg)
            try:
                ledger = cycle.ledger()
                return bool(ledger.open_orders) or ledger.is_frozen
            finally:
                cycle.client.close()
        except Exception as exc:
            log.warning("[daemon] ledger check failed, forcing settle: %r",
                        exc)
            return True


def _rotation_fields(state_dir) -> dict[str, Any]:
    """A1 rotation counters for the status file, or nulls when absent."""
    blank = {"a1_session_index": None, "a1_next_rebalance": None,
             "rank_as_of": None, "rank_stale_sessions": None}
    path = Path(state_dir) / "a1_rebalance_state.json"
    if not path.is_file():
        return blank
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return blank
    return {"a1_session_index": cached.get("session_index"),
            "a1_next_rebalance": cached.get("sessions_until_next"),
            "rank_as_of": cached.get("rank_as_of"),
            "rank_stale_sessions": cached.get("rank_stale_sessions")}


def _label(cfg: dict[str, Any]) -> str:
    """The strategy id, for alert titles.

    Alerts used to say "A0" whatever was running. With two strategies capable
    of holding the same account, an alert that does not name its book cannot
    be acted on.
    """
    strategy = ((cfg.get("execution") or {}).get("strategy") or {})
    name = str(strategy.get("name", "a0"))
    version = str(strategy.get("version", "0.0.1")).replace(".", "_")
    return f"{name}_v{version}"


def _notify_missed_sessions(daemon: "_Daemon", sessions: list,
                            now: pd.Timestamp, lead_sec: int,
                            cfg: dict[str, Any] | None = None) -> None:
    """Tell an armed owner, once per session, that a window went untraded.

    Covers every way a window can slip past on this daemon's watch: the
    machine slept through it (the planner then skips straight to the next
    session, so no per-session branch ever fires), every attempt aborted,
    or the network was down for the whole half hour. Sessions whose window
    predates this daemon's start are not its to report.
    """
    for sess in sessions:
        sid = str(sess.date_ny)
        if not sess.is_full:
            continue
        submit_at = sess.close_utc - pd.Timedelta(seconds=lead_sec)
        if now <= submit_at:
            break  # this window and everything later is still ahead
        if sid == daemon.decided_session or sid in daemon.settled_sessions:
            continue
        if instruments.decision_key(sess) < daemon.started_at:
            continue
        daemon._notify_once(f"missed:{sid}",
                            f"{_label(cfg or {})} daemon: session not decided",
                            f"{sid}: the decision window passed without "
                            f"a submission")


# ============================================================================
# [3] The loop
# ============================================================================

def run(venue: str = "t212") -> int:
    """Run until stopped. Returns an exit code for the CLI."""
    cfg = load_config(venue)
    env = str(cfg["_env"])
    daemon = _Daemon(venue, env)
    if not daemon.acquire_daemon_lock():
        print("another daemon is already running for this environment")
        return 1

    def _stop(signum, frame):  # noqa: ARG001  signal signature
        log.warning("[daemon] stop signal %s received", signum)
        daemon.stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info("[daemon] started env=%s pid=%s", env, os.getpid())
    daemon.write_status("starting", "loading configuration", None, None, None)
    last_needs_check = 0.0

    while not daemon.stop.is_set():
        try:
            cfg = load_config(venue)  # dashboard edits apply next cycle
            dry_run = bool((cfg.get("execution") or {}).get("dry_run", True))
            lead = int((cfg.get("execution") or {})
                       .get("submit_lead_sec", 60))
            sessions = daemon.sessions(cfg)
            now = pd.Timestamp.now(tz="UTC")
            plan = plan_next(sessions, now, daemon.decided_session,
                             daemon.settled_sessions, lead)
            # Loose ends override the schedule: open orders or a frozen
            # book get a settle NOW unless a decide window is due, in which
            # case decide itself refuses until settle has run -- so settle
            # first there too. Checked at most once per minute.
            if plan["action"] in ("wait_decide", "wait_settle", "idle",
                                  "decide") \
                    and time.monotonic() - last_needs_check > 60.0:
                last_needs_check = time.monotonic()
                if daemon.ledger_needs_settle(cfg):
                    daemon.write_status("settling",
                                        "resolving open orders or freeze",
                                        None, plan["session_date"], dry_run)
                    daemon.run_settle(cfg, plan["session_date"]
                                      or "recovery")
                    daemon.settled_sessions.discard(plan["session_date"]
                                                    or "recovery")
                    daemon.stop.wait(SETTLE_RETRY_SEC
                                     if daemon.last_error else 1.0)
                    continue
            action = plan["action"]
            at = plan["at"]
            sid = plan["session_date"]

            if action == "decide":
                daemon.write_status("deciding", "decision window open", at,
                                    sid, dry_run)
                daemon.run_decide(cfg, sid)
                if daemon.decided_session != sid:
                    backoff = min(RETRY_SEC * (2 ** max(
                        0, daemon._abort_streak - 1)), 300.0)
                    daemon.stop.wait(backoff)
                continue
            if action == "settle":
                daemon.write_status("settling", "harvesting and reconciling",
                                    at, sid, dry_run)
                daemon.run_settle(cfg, sid)
                if sid not in daemon.settled_sessions:
                    daemon.stop.wait(SETTLE_RETRY_SEC)
                continue

            if not dry_run:
                _notify_missed_sessions(daemon, sessions, now, lead, cfg)
            detail = {"wait_decide": "sleeping until the decision window",
                      "wait_settle": "awaiting the close",
                      "settle": "harvesting and reconciling",
                      "idle": "no upcoming session in the timetable"}[action]
            daemon.write_status(action, detail, at, sid, dry_run)
            remaining = (at - pd.Timestamp.now(tz="UTC")).total_seconds()
            daemon.stop.wait(min(max(remaining, 1.0), HEARTBEAT_SEC))
        except Exception as exc:
            daemon.last_error = f"loop error: {exc!r}"[:300]
            log.critical("[daemon] %s", daemon.last_error)
            notify(f"{_label(cfg)} daemon error", repr(exc)[:180])
            daemon.write_status("error", daemon.last_error, None, None, None)
            daemon.stop.wait(RETRY_SEC)

    daemon.write_status("stopped", "stop signal handled", None, None, None)
    log.info("[daemon] stopped cleanly")
    return 0
