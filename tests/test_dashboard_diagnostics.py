"""Tests for the connection-failure classifier.

Responsibility: pin the mapping from a failed account poll to a cause,
including the two cases that have actually occurred on this machine -- a
resolver answering with an unrelated address, and a live-only key sent to
the demo host because QUANT_ENV defaulted to paper.

Out of scope: the wording shown to the reader, which lives in
trading212/dashboard/assets/labels.json.

Change log:
    2026-08-29  Created alongside trading212/dashboard/diagnostics.py.
"""

from __future__ import annotations

from trading212.dashboard import diagnostics


def test_live_only_key_on_demo_host_is_named_as_environment_not_bad_key():
    """The 401 that QUANT_ENV=paper causes must not read as a dead key."""
    verdict = diagnostics.diagnose("demo.trading212.com", "HTTP 401",
                                   env="paper")
    assert verdict["cause"] == "wrong_environment"


def test_same_401_on_the_live_host_is_a_real_auth_failure():
    verdict = diagnostics.diagnose("live.trading212.com", "HTTP 401",
                                   env="live")
    assert verdict["cause"] == "auth"


def test_rate_limit_is_classified_without_touching_the_network(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("must not probe DNS for a 429")

    monkeypatch.setattr(diagnostics, "_os_addresses", fail)
    verdict = diagnostics.diagnose("live.trading212.com", "HTTP 429")
    assert verdict["cause"] == "rate_limited"


def test_hijacked_resolver_is_called_a_mismatch(monkeypatch):
    """Local answer unreachable, public answer fine -- the observed failure."""
    monkeypatch.setattr(diagnostics, "_os_addresses", lambda h: ["104.244.46.5"])
    monkeypatch.setattr(diagnostics, "_public_addresses",
                        lambda h: ["162.159.140.1"])
    monkeypatch.setattr(diagnostics, "_connects",
                        lambda addr, host: addr == "162.159.140.1")
    verdict = diagnostics.diagnose("live.trading212.com", "timed out")
    assert verdict["cause"] == "dns_mismatch"
    assert verdict["reachable_bypassing_dns"] is True


def test_different_edge_addresses_that_both_work_are_not_called_a_hijack():
    """A CDN hands out different edge IPs to different resolvers routinely.

    Claiming a hijack on that alone would send the reader off disabling a VPN
    that was never the problem.
    """
    import trading212.dashboard.diagnostics as d
    orig_os, orig_pub, orig_con = d._os_addresses, d._public_addresses, d._connects
    try:
        d._os_addresses = lambda h: ["162.159.140.1"]
        d._public_addresses = lambda h: ["172.64.145.1"]
        d._connects = lambda addr, host: True
        verdict = d.diagnose("live.trading212.com", "timed out")
        assert verdict["cause"] != "dns_mismatch"
    finally:
        d._os_addresses, d._public_addresses, d._connects = orig_os, orig_pub, orig_con


def test_every_returned_cause_has_wording_in_the_label_file():
    """A cause with no label would render as an empty explanation."""
    import json
    from pathlib import Path
    labels = json.loads(
        (Path(__file__).resolve().parents[1] / "trading212" / "dashboard" /
         "assets" / "labels.json").read_text(encoding="utf-8"))
    for cause in diagnostics.CAUSES:
        assert cause in labels["status"]["diagnosis"], cause
