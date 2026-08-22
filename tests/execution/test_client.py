"""Client tests: retry policy, non-idempotent POST contract, red-line gates.

Uses httpx.MockTransport so no request leaves the process; the credential
is injected through the environment override of common.secrets.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from common.net import PermanentError, TransientError
from trading212.client import (OrderSubmitAmbiguousError, T212Client,
                               T212_BASE_LIVE, T212_BASE_PAPER)

D = Decimal


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    monkeypatch.setenv("QUANT_SECRET_TRADING212_API_KEY", "test-key-not-real")
    # Neutralize retry sleeps so failure-path tests stay fast.
    monkeypatch.setattr("trading212.client.time.sleep", lambda s: None)


class _NoOpBucket:
    def acquire(self, n: float = 1.0) -> None:
        return None


def _client(env="live", cfg=None, handler=None):
    client = T212Client(env, cfg=cfg)
    # Token buckets pace at the venue's real ceilings (seconds apart); tests
    # assert sequencing, not pacing, so they are stubbed out.
    client._buckets = {name: _NoOpBucket() for name in client._buckets}
    if handler is not None:
        client._session = httpx.Client(
            base_url=client.base, transport=httpx.MockTransport(handler))
    return client


def test_env_selects_the_host():
    assert _client("live").base == T212_BASE_LIVE
    assert _client("paper").base == T212_BASE_PAPER


def test_get_retries_transient_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"currency": "GBP"})

    client = _client(handler=handler)
    assert client.account_summary() == {"currency": "GBP"}
    assert len(calls) == 3


def test_get_gives_up_after_max_attempts():
    def handler(request):
        return httpx.Response(503)

    with pytest.raises(TransientError):
        _client(handler=handler).account_summary()


def test_get_404_on_order_returns_none():
    client = _client(handler=lambda r: httpx.Response(404, json={}))
    assert client.order(123) is None


def test_order_post_never_retries_on_5xx():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    cfg = {"_env": "live", "live": True, "execution": {"dry_run": False}}
    client = _client(cfg=cfg, handler=handler)
    with pytest.raises(OrderSubmitAmbiguousError):
        client.place_market_order("NVDA_US_EQ", D("1"))
    # One attempt, ever: a duplicate POST is a duplicate order at the venue.
    assert len(calls) == 1


def test_order_post_transport_failure_is_ambiguous():
    def handler(request):
        raise httpx.ConnectTimeout("boom")

    cfg = {"_env": "live", "live": True, "execution": {"dry_run": False}}
    with pytest.raises(OrderSubmitAmbiguousError):
        _client(cfg=cfg, handler=handler).place_market_order("NVDA_US_EQ", D("1"))


def test_order_post_400_is_a_proven_rejection():
    def handler(request):
        return httpx.Response(400, json={"error": "insufficient funds"})

    cfg = {"_env": "live", "live": True, "execution": {"dry_run": False}}
    with pytest.raises(PermanentError):
        _client(cfg=cfg, handler=handler).place_market_order("NVDA_US_EQ", D("1"))


def test_dry_run_config_blocks_the_client_order_path():
    cfg = {"_env": "live", "live": True, "execution": {"dry_run": True}}
    client = _client(cfg=cfg, handler=lambda r: httpx.Response(200, json={}))
    with pytest.raises(PermanentError, match="dry-run"):
        client.place_market_order("NVDA_US_EQ", D("1"))


def test_missing_config_blocks_the_client_order_path():
    client = _client(handler=lambda r: httpx.Response(200, json={}))
    with pytest.raises(PermanentError, match="read-only"):
        client.place_market_order("NVDA_US_EQ", D("1"))


def test_live_env_without_live_flag_refuses():
    cfg = {"_env": "live", "live": False, "_path": "x",
           "execution": {"dry_run": False}}
    client = _client(cfg=cfg, handler=lambda r: httpx.Response(200, json={}))
    with pytest.raises(RuntimeError, match="live"):
        client.place_market_order("NVDA_US_EQ", D("1"))


def test_history_pagination_follows_next_page_path():
    def handler(request):
        if b"cursor=1" in request.url.query:
            return httpx.Response(200, json={"items": [{"order": {"id": 1}}],
                                             "nextPagePath": None})
        return httpx.Response(200, json={
            "items": [{"order": {"id": 2}}],
            "nextPagePath": "/api/v0/equity/history/orders?cursor=1"})

    client = _client(handler=handler)
    ids = [item["order"]["id"] for item in client.iter_history_orders()]
    assert ids == [2, 1]
