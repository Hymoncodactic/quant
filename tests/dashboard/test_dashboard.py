"""Dashboard behavior: settings, samples, manual-order refusals, HTTP surface.

Responsibility: prove the parts of the dashboard that can be wrong quietly.
The settings gate must refuse an incomplete configuration the same way the
risk gate would; the sample store must keep gaps visible rather than drawing
through them; a manual order must refuse every unconfirmed path; and the
write routes must reject a request that does not carry this run's token.

Out of scope: the strategy and execution layers, covered by
tests/execution/; charting, which is browser-side.

Public functions: None. Pytest collects the test functions directly.

Constants:
    GOOD  dict  A complete, valid settings payload.

Inputs: None. Filesystem writes are redirected to a temporary directory.
Outputs: None.

Change log:
    2026-08-22  Created with the dashboard.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from trading212.dashboard import settings, snapshots

GOOD = {"max_order_notional_gbp": 45, "max_gross_notional_gbp": 500,
        "max_daily_orders": 40, "min_order_value_gbp": 1, "fee_buffer": 0.005,
        "submit_lead_sec": 60, "dry_run": True}


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

def test_blank_settings_report_every_field():
    problems = settings.validate({})
    assert {p["field"] for p in problems} == {f[0] for f in settings.FIELDS}
    assert all(p["code"] in ("missing", "not_a_switch") for p in problems)


def test_complete_settings_have_no_problems():
    assert settings.validate(GOOD) == []


def test_zero_limit_is_a_problem_not_a_value():
    """Zero means the risk gate refuses everything, so it is never 'set'."""
    problems = settings.validate({**GOOD, "max_daily_orders": 0})
    assert {"field": "max_daily_orders", "code": "must_be_positive"} in problems


def test_absurd_fee_buffer_is_caught():
    problems = settings.validate({**GOOD, "fee_buffer": 0.5})
    assert {"field": "fee_buffer", "code": "fee_buffer_too_large"} in problems


def test_text_in_a_number_field_is_caught():
    problems = settings.validate({**GOOD, "min_order_value_gbp": "abc"})
    assert {"field": "min_order_value_gbp", "code": "not_a_number"} in problems


def test_describe_reports_not_ready_without_a_book():
    cfg = {"_env": "live", "live": True, "risk": dict(
        max_order_notional_gbp=45, max_gross_notional_gbp=500,
        max_daily_orders=40, min_order_value_gbp=1, fee_buffer=0.005),
        "execution": {"submit_lead_sec": 60, "dry_run": True}}
    filled = settings.describe(cfg, ledger_ready=True)
    assert filled["missing"] == [] and filled["ready"] is True
    unbooked = settings.describe(cfg, ledger_ready=False)
    assert unbooked["missing"] == [] and unbooked["ready"] is False


def test_apply_refuses_to_write_an_invalid_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_path", lambda env: tmp_path / "x.yaml")
    with pytest.raises(ValueError):
        settings.apply("live", {**GOOD, "max_daily_orders": -1})
    assert not (tmp_path / "x.yaml").exists()


# ----------------------------------------------------------------------
# Sample store
# ----------------------------------------------------------------------

@pytest.fixture()
def sample_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("trading212.dashboard.snapshots.dashboard_state_dir",
                        lambda venue: tmp_path / venue)
    return tmp_path


def test_snapshot_roundtrip(sample_dir):
    snapshots.write_snapshot("t212", {"a": 1})
    assert snapshots.read_snapshot("t212") == {"a": 1}


def test_missing_snapshot_reads_as_none(sample_dir):
    assert snapshots.read_snapshot("t212") is None


def test_downsampling_keeps_every_gap_marker(sample_dir):
    """A gap is what stops a chart drawing through unobserved hours, so it
    must survive downsampling even when ordinary points do not."""
    for i in range(500):
        snapshots.append_sample("t212", {"ts": f"2026-08-22T00:{i:03d}", "v": i})
    snapshots.mark_gap("t212", "collector stopped")
    for i in range(500):
        snapshots.append_sample("t212", {"ts": f"2026-08-22T01:{i:03d}", "v": i})
    rows = snapshots.read_samples("t212", days=7, max_points=100)
    assert len(rows) <= 120
    assert sum(1 for r in rows if r.get("gap")) == 1


def test_a_torn_last_line_does_not_break_reading(sample_dir):
    snapshots.append_sample("t212", {"ts": "2026-08-22T00:00", "v": 1})
    path = snapshots.sample_files("t212")[0]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"ts": "2026-08-22T00:01", "v":')
    rows = snapshots.read_samples("t212")
    assert len(rows) == 1


# ----------------------------------------------------------------------
# Allocation versus the account
# ----------------------------------------------------------------------

def _snapshot(free):
    return {"account": {"ok": True, "summary": {"cash": {"availableToTrade": free}}}}


def test_allocation_over_account_free_cash_is_flagged():
    """Creating a book moves no money, so nothing else would catch this."""
    from trading212.dashboard.api import _funding
    out = _funding({"cash_gbp": 1000.0}, _snapshot(0.0))
    assert out["known"] and out["over_account"] is True


def test_allocation_within_account_free_cash_is_not_flagged():
    from trading212.dashboard.api import _funding
    out = _funding({"cash_gbp": 400.0}, _snapshot(500.0))
    assert out["known"] and out["over_account"] is False


def test_unreachable_account_reports_unknown_rather_than_safe():
    """With no account figure the comparison is unknown, never 'fine'."""
    from trading212.dashboard.api import _funding
    out = _funding({"cash_gbp": 1000.0},
                   {"account": {"ok": False, "reason": "no route to host"}})
    assert out["known"] is False and out["over_account"] is False
    assert out["account_free_gbp"] is None


def test_no_book_reports_unknown():
    from trading212.dashboard.api import _funding
    out = _funding({"cash_gbp": None}, _snapshot(500.0))
    assert out["known"] is False


# ----------------------------------------------------------------------
# Manual orders
# ----------------------------------------------------------------------

class _Ctx:
    env = "live"
    cfg = {"live": True, "endpoints": {"secret_name": "trading212_api_key"}}


@pytest.fixture()
def manual(tmp_path, monkeypatch):
    from trading212.dashboard import manual_orders
    monkeypatch.setattr(manual_orders, "execution_state_dir",
                        lambda venue: tmp_path)
    return manual_orders


def test_unconfirmed_manual_order_sends_nothing(manual):
    out = manual.place(_Ctx(), "AAPL_US_EQ", "1", confirm=False, real=True)
    assert out["outcome"] == "refused" and out["reason"] == "not_confirmed"


def test_zero_quantity_is_refused(manual):
    out = manual.place(_Ctx(), "AAPL_US_EQ", "0", confirm=True, real=True)
    assert out["outcome"] == "refused" and out["reason"] == "quantity_is_zero"


def test_non_numeric_quantity_is_refused(manual):
    out = manual.place(_Ctx(), "AAPL_US_EQ", "one", confirm=True, real=True)
    assert out["outcome"] == "refused"


def test_rehearsal_never_reaches_the_venue(manual):
    out = manual.place(_Ctx(), "AAPL_US_EQ", "1", confirm=True, real=False)
    assert out["outcome"] == "rehearsed"


def test_a_configuration_without_the_live_flag_cannot_send(manual):
    ctx = _Ctx()
    ctx.cfg = {"live": False}
    out = manual.place(ctx, "AAPL_US_EQ", "1", confirm=True, real=True)
    assert out["outcome"] == "refused" and out["reason"] == "config_not_live"


def test_every_attempt_is_journaled(manual):
    manual.place(_Ctx(), "AAPL_US_EQ", "1", confirm=False, real=False)
    manual.place(_Ctx(), "AAPL_US_EQ", "2", confirm=True, real=False)
    assert len(manual.history()) == 2


# ----------------------------------------------------------------------
# HTTP surface
# ----------------------------------------------------------------------

@pytest.fixture()
def live_server(monkeypatch):
    monkeypatch.setenv("QUANT_ENV", "live")
    from trading212.dashboard.server import build_server
    try:
        server = build_server(port=0)
    except FileNotFoundError:
        pytest.skip("no venue configuration on this machine")
    threading.Thread(target=server.serve_forever,
                     kwargs={"poll_interval": 0.1}, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _url(server, path):
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def _post(server, path, body, token):
    req = urllib.request.Request(
        _url(server, path), method="POST", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Dashboard-Nonce": token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_pages_carry_this_run_token_and_not_the_placeholder(live_server):
    with urllib.request.urlopen(_url(live_server, "/"), timeout=10) as r:
        body = r.read().decode()
    assert live_server.token in body
    assert "__DASH_TOKEN__" not in body


def test_write_routes_reject_a_wrong_token(live_server):
    status, body = _post(live_server, "/api/settings", GOOD, "not-the-token")
    assert status == 403 and body["problem"] == "bad_token"


def test_unknown_route_is_a_clean_404(live_server):
    try:
        urllib.request.urlopen(_url(live_server, "/api/nope"), timeout=10)
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_state_route_answers_without_the_venue(live_server):
    """The venue may be unreachable; the page must still render."""
    with urllib.request.urlopen(_url(live_server, "/api/state"), timeout=15) as r:
        state = json.loads(r.read())
    assert "readiness" in state and "collector" in state
    assert state["collector"]["running"] is False


# ----------------------------------------------------------------------
# Allocation changes
# ----------------------------------------------------------------------

def test_allocation_change_is_journaled_not_edited(tmp_path):
    """The book must still explain its own cash from its own history."""
    from decimal import Decimal
    from trading212.execution.shadow_ledger import ShadowLedger
    led = ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("1000"))
    led.record_allocation_change("c1", Decimal("500"), "topped up")
    assert led.cash_gbp == Decimal("1500")
    journal = (tmp_path / "a0_v0_0_1_journal.jsonl").read_text()
    assert "ALLOCATION_CHANGED" in journal
    assert ShadowLedger.load(tmp_path, "a0_v0_0_1").cash_gbp == Decimal("1500")


def test_allocation_can_be_taken_back(tmp_path):
    from decimal import Decimal
    from trading212.execution.shadow_ledger import ShadowLedger
    led = ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("1000"))
    led.record_allocation_change("c1", Decimal("-400"), "reclaimed")
    assert led.cash_gbp == Decimal("600")


def test_allocation_cannot_drive_cash_negative(tmp_path):
    from decimal import Decimal
    from trading212.execution.shadow_ledger import ShadowLedger
    led = ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("1000"))
    with pytest.raises(ValueError):
        led.record_allocation_change("c1", Decimal("-1001"), "too much")
    assert led.cash_gbp == Decimal("1000")


def test_allocation_refused_while_the_book_is_frozen(tmp_path):
    """Resizing a book whose exposure is in doubt would bury the doubt."""
    from decimal import Decimal
    from trading212.execution.shadow_ledger import (LedgerFrozenError,
                                                    ShadowLedger)
    led = ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("1000"))
    led.record_intent("i1", "NVDA", "NVDA_US_EQ", Decimal("1"),
                      Decimal("100"), Decimal("1.25"), dry_run=False)
    led.record_submit_ambiguous("i1", "NVDA", "NVDA_US_EQ", Decimal("1"), "t")
    with pytest.raises(LedgerFrozenError):
        led.record_allocation_change("c1", Decimal("500"), "topped up")


# ----------------------------------------------------------------------
# The halt flag
# ----------------------------------------------------------------------

def test_halt_can_always_be_raised(live_server):
    status, body = _post(live_server, "/api/halt", {"action": "raise"},
                         live_server.token)
    try:
        assert status == 200 and body["halted"] is True
        assert live_server.ctx.halt_path.exists()
    finally:
        live_server.ctx.halt_path.unlink(missing_ok=True)


class _HaltCtx:
    """The minimum a halt request touches."""

    def __init__(self, halt_path, ledger, params=None):
        self.halt_path = halt_path
        self._ledger = ledger
        self.params = params or {"trade_symbols": []}

    def ledger(self):
        return self._ledger

    def client(self):
        raise AssertionError("reconcile must not run once a check has failed")


def test_clearing_a_halt_is_refused_while_an_order_is_unsettled(tmp_path):
    """A halt normally means something broke; clearing it blindly would
    restart trading into the same problem."""
    from decimal import Decimal
    from trading212.dashboard.api import post_halt
    from trading212.execution.shadow_ledger import ShadowLedger
    led = ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("1000"))
    led.record_intent("i1", "NVDA", "NVDA_US_EQ", Decimal("1"), Decimal("100"),
                      Decimal("1.25"), dry_run=False)
    led.record_submitted("i1", 111, "NVDA", "NVDA_US_EQ", Decimal("1"),
                         Decimal("80"), "NEW")
    halt = tmp_path / "halt"
    halt.touch()
    status, body = post_halt(_HaltCtx(halt, led), {"action": "clear"})
    assert status == 409 and body["problem"] == "not_clean"
    assert any(b["check"] == "no_open_orders" for b in body["blockers"])
    assert halt.exists()          # the flag survives a refused clear


def test_clearing_a_halt_is_refused_while_the_book_is_frozen(tmp_path):
    from decimal import Decimal
    from trading212.dashboard.api import post_halt
    from trading212.execution.shadow_ledger import ShadowLedger
    led = ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("1000"))
    led.record_intent("i1", "NVDA", "NVDA_US_EQ", Decimal("1"), Decimal("100"),
                      Decimal("1.25"), dry_run=False)
    led.record_submit_ambiguous("i1", "NVDA", "NVDA_US_EQ", Decimal("1"), "t")
    halt = tmp_path / "halt"
    halt.touch()
    status, body = post_halt(_HaltCtx(halt, led), {"action": "clear"})
    assert status == 409
    assert any(b["check"] == "no_ambiguity" for b in body["blockers"])
    assert halt.exists()


def test_clearing_a_halt_without_a_book_is_refused(tmp_path):
    from trading212.dashboard.api import post_halt
    halt = tmp_path / "halt"
    halt.touch()
    status, body = post_halt(_HaltCtx(halt, None), {"action": "clear"})
    assert status == 409
    assert any(b["check"] == "ledger_exists" for b in body["blockers"])


def test_unknown_halt_action_is_rejected(live_server):
    status, body = _post(live_server, "/api/halt", {"action": "wobble"},
                         live_server.token)
    assert status == 400 and body["problem"] == "unknown_action"


# ----------------------------------------------------------------------
# Session spans for the chart
# ----------------------------------------------------------------------

def test_sessions_route_answers_even_without_a_calendar_cache(live_server):
    """The page must render before anything has fetched the calendar."""
    with urllib.request.urlopen(_url(live_server, "/api/sessions"),
                                timeout=15) as r:
        body = json.loads(r.read())
    assert "sessions" in body and body["tz"] == "America/New_York"


# ----------------------------------------------------------------------
# Retiring a book
# ----------------------------------------------------------------------

class _ResetCtx:
    def __init__(self, state_dir, ledger, strategy_id="a0_v0_0_1"):
        self.state_dir = state_dir
        self.strategy_id = strategy_id
        self._ledger = ledger

    def ledger(self):
        return self._ledger


def test_reset_needs_confirmation(tmp_path):
    from trading212.dashboard.api import post_ledger_reset
    status, body = post_ledger_reset(_ResetCtx(tmp_path, None), {})
    assert status == 400 and body["problem"] == "not_confirmed"


def test_reset_renames_rather_than_deletes(tmp_path):
    """A book is the only record of which holdings were the strategy's, so
    finishing with it is not a reason to destroy what it recorded."""
    from decimal import Decimal
    from trading212.dashboard.api import post_ledger_reset
    from trading212.execution.shadow_ledger import ShadowLedger
    led = ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("1000"))
    status, body = post_ledger_reset(_ResetCtx(tmp_path, led),
                                     {"confirm": True})
    assert status == 200 and len(body["retired_as"]) == 2
    assert not (tmp_path / "a0_v0_0_1_snapshot.json").exists()
    assert list(tmp_path.glob("*.retired-*"))


def test_a_new_book_can_be_created_after_a_reset(tmp_path):
    from decimal import Decimal
    from trading212.dashboard.api import post_ledger_reset
    from trading212.execution.shadow_ledger import ShadowLedger
    led = ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("1000"))
    post_ledger_reset(_ResetCtx(tmp_path, led), {"confirm": True})
    fresh = ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("2500"))
    assert fresh.cash_gbp == Decimal("2500")


def test_reset_refused_while_the_book_holds_a_position(tmp_path):
    from decimal import Decimal
    from trading212.dashboard.api import post_ledger_reset
    from trading212.execution.shadow_ledger import ShadowLedger
    led = ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("1000"))
    led.record_intent("i1", "NVDA", "NVDA_US_EQ", Decimal("1"), Decimal("100"),
                      Decimal("1.25"), dry_run=False)
    led.record_submitted("i1", 111, "NVDA", "NVDA_US_EQ", Decimal("1"),
                         Decimal("80"), "NEW")
    led.record_fill(111, 9001, Decimal("1"), Decimal("100"), Decimal("-80"),
                    [], "t")
    led.record_order_terminal(111, "FILLED", Decimal("1"))
    status, body = post_ledger_reset(_ResetCtx(tmp_path, led),
                                     {"confirm": True})
    assert status == 409 and body["problem"] == "not_empty"
    assert any(b["check"] == "no_positions" for b in body["blockers"])
    assert (tmp_path / "a0_v0_0_1_snapshot.json").exists()


def test_reset_refused_while_an_order_is_open(tmp_path):
    from decimal import Decimal
    from trading212.dashboard.api import post_ledger_reset
    from trading212.execution.shadow_ledger import ShadowLedger
    led = ShadowLedger.init_fresh(tmp_path, "a0_v0_0_1", Decimal("1000"))
    led.record_intent("i1", "NVDA", "NVDA_US_EQ", Decimal("1"), Decimal("100"),
                      Decimal("1.25"), dry_run=False)
    led.record_submitted("i1", 111, "NVDA", "NVDA_US_EQ", Decimal("1"),
                         Decimal("80"), "NEW")
    status, body = post_ledger_reset(_ResetCtx(tmp_path, led),
                                     {"confirm": True})
    assert status == 409
    assert any(b["check"] == "no_open_orders" for b in body["blockers"])
