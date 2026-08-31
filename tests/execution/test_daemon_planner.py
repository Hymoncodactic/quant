"""Tests for the daemon's pure scheduler, branch by branch.

Responsibility: pin plan_next against a synthetic two-day timetable so
every action the loop can take (wait_decide, decide, wait_settle, settle,
idle, half-day skip, restart recovery) is asserted without a clock, a
venue, or a running process.

Out of scope: the loop itself and its locks (exercised by the live drills
in tests/live/01_session_test_plan.md T7).

Change log:
    2026-08-31  Created with the daemon.
"""

from __future__ import annotations

import pandas as pd

from trading212.execution.daemon import SETTLE_DELAY_SEC, plan_next
from trading212.execution.instruments import Session

LEAD = 60


def _session(date_str: str, full: bool = True) -> Session:
    """is_full is derived from the close hour: 20:00Z is 16:00 EDT (full),
    17:00Z is a 13:00 early close (half day)."""
    close = "20:00:00" if full else "17:00:00"
    return Session(date_ny=pd.Timestamp(date_str).date(),
                   open_utc=pd.Timestamp(f"{date_str} 13:30:00+00:00"),
                   close_utc=pd.Timestamp(f"{date_str} {close}+00:00"))


TIMETABLE = [_session("2026-08-31"), _session("2026-09-01")]


def _plan(now_str: str, decided=None, settled=frozenset()):
    return plan_next(TIMETABLE, pd.Timestamp(now_str), decided,
                     set(settled), LEAD)


def test_before_the_window_waits_until_the_decision_key():
    plan = _plan("2026-08-31 14:00:00+00:00")
    assert plan["action"] == "wait_decide"
    assert plan["at"] == pd.Timestamp("2026-08-31 19:30:00+00:00")
    assert plan["session_date"] == "2026-08-31"


def test_inside_the_window_decides_now():
    plan = _plan("2026-08-31 19:45:00+00:00")
    assert plan["action"] == "decide"


def test_at_the_key_instant_decides():
    assert _plan("2026-08-31 19:30:00+00:00")["action"] == "decide"


def test_after_deciding_waits_for_the_close():
    plan = _plan("2026-08-31 19:46:00+00:00", decided="2026-08-31")
    assert plan["action"] == "wait_settle"
    assert plan["at"] == pd.Timestamp("2026-08-31 20:00:00+00:00") \
        + pd.Timedelta(seconds=SETTLE_DELAY_SEC)


def test_missed_window_still_settles_for_hygiene():
    plan = _plan("2026-08-31 19:59:30+00:00")
    assert plan["action"] == "wait_settle"
    assert plan["session_date"] == "2026-08-31"


def test_after_the_delay_settles_the_decided_session():
    plan = _plan("2026-08-31 20:05:00+00:00", decided="2026-08-31")
    assert plan["action"] == "settle"
    assert plan["session_date"] == "2026-08-31"


def test_settled_session_rolls_to_the_next_day():
    plan = _plan("2026-08-31 20:30:00+00:00", decided="2026-08-31",
                 settled={"2026-08-31"})
    assert plan["action"] == "wait_decide"
    assert plan["session_date"] == "2026-09-01"


def test_restart_long_after_an_undecided_close_skips_that_session():
    """A session that ended before the daemon existed holds nothing of ours;
    it must not trigger a late settle storm on boot."""
    plan = _plan("2026-08-31 22:00:00+00:00")
    assert plan["action"] == "wait_decide"
    assert plan["session_date"] == "2026-09-01"


def test_restart_long_after_a_DECIDED_close_still_settles_it():
    plan = _plan("2026-08-31 22:00:00+00:00", decided="2026-08-31")
    assert plan["action"] == "settle"
    assert plan["session_date"] == "2026-08-31"


def test_half_day_is_skipped_entirely():
    table = [_session("2026-08-31", full=False), _session("2026-09-01")]
    plan = plan_next(table, pd.Timestamp("2026-08-31 14:00:00+00:00"),
                     None, set(), LEAD)
    assert plan["session_date"] == "2026-09-01"


def test_exhausted_timetable_idles_with_a_retry_horizon():
    plan = _plan("2026-09-02 23:00:00+00:00", decided="2026-09-01",
                 settled={"2026-08-31", "2026-09-01"})
    assert plan["action"] == "idle"
    assert plan["at"] > pd.Timestamp("2026-09-02 23:00:00+00:00")


# ============================================================================
# Review-round regressions (2026-08-31): phase outcomes and stop awareness
# ============================================================================

def test_wait_for_submit_aborts_when_stop_is_requested(tmp_path):
    from trading212.execution.session_cycle import _wait_for_submit_instant
    now = pd.Timestamp.now(tz="UTC")
    reason = _wait_for_submit_instant(
        now + pd.Timedelta(seconds=120), now + pd.Timedelta(seconds=180),
        grace_sec=30, max_wait_sec=2400, halt_path=tmp_path / "halt",
        stop_check=lambda: True)
    assert reason is not None and "stop requested" in reason


def test_settle_poll_returns_still_open_on_stop(monkeypatch):
    from decimal import Decimal
    from trading212.execution import order_monitor

    class _Ledger:
        open_orders = {"111": {"symbol": "NVDA"}}

    class _Client:
        def pending_orders(self):
            return [{"id": 111}]

    report = order_monitor.poll_until_settled(
        _Client(), _Ledger(), expected_ccy="GBP", max_wait_sec=600,
        poll_sec=5, stop_check=lambda: True)
    assert report.still_open == [111]


def _daemon(tmp_path, monkeypatch):
    from trading212.execution import daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "execution_state_dir",
                        lambda venue, env="live": tmp_path)
    monkeypatch.setattr(daemon_mod, "notify",
                        lambda *a, **k: True)
    d = daemon_mod._Daemon("t212", "paper")
    return daemon_mod, d


def test_aborted_settle_is_not_marked_settled(tmp_path, monkeypatch):
    daemon_mod, d = _daemon(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_mod.session_cycle, "settle",
                        lambda cfg, stop_check=None:
                        {"aborted": True, "reason": "ledger unavailable"})
    monkeypatch.setattr(d, "_hold_execution_lock",
                        lambda deadline_sec=60.0: open(tmp_path / "x", "a+"))
    d.run_settle({"execution": {}}, "2026-08-31")
    assert "2026-08-31" not in d.settled_sessions
    assert "settle incomplete" in (d.last_error or "")


def test_settle_with_orders_still_open_is_not_marked_settled(tmp_path,
                                                             monkeypatch):
    daemon_mod, d = _daemon(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_mod.session_cycle, "settle",
                        lambda cfg, stop_check=None:
                        {"settle": "x", "still_open": [111], "frozen": False,
                         "reconcile_ok": True})
    monkeypatch.setattr(d, "_hold_execution_lock",
                        lambda deadline_sec=60.0: open(tmp_path / "x", "a+"))
    d.run_settle({"execution": {}}, "2026-08-31")
    assert "2026-08-31" not in d.settled_sessions


def test_clean_settle_is_marked_settled(tmp_path, monkeypatch):
    daemon_mod, d = _daemon(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_mod.session_cycle, "settle",
                        lambda cfg, stop_check=None:
                        {"settle": "settled=[] still_open=[] problems=none",
                         "still_open": [], "frozen": False,
                         "reconcile_ok": True, "reconcile": "CLEAN"})
    monkeypatch.setattr(d, "_hold_execution_lock",
                        lambda deadline_sec=60.0: open(tmp_path / "x", "a+"))
    d.run_settle({"execution": {}}, "2026-08-31")
    assert "2026-08-31" in d.settled_sessions
    assert d.last_error is None


def test_notify_once_deduplicates(tmp_path, monkeypatch):
    daemon_mod, d = _daemon(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(daemon_mod, "notify",
                        lambda title, msg: calls.append(title) or True)
    for _ in range(5):
        d._notify_once("k1", "t", "m")
    assert len(calls) == 1
