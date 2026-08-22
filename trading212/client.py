"""Trading 212 public API v0 REST client.

Responsibility: transport only -- authentication, per-endpoint client-side
rate limiting, retry policy, and typed errors. Shared by ingest and
execution (ARCHITECTURE.md section 2). No business decisions live here; the
DRY_RUN and live-environment gates for order placement are enforced both
here (defense in depth, see place_order) and in the order router.

API contract facts (all S4, from the official OpenAPI mirror
data/reference/t212_openapi_v0_20260820.yaml, retrieved 2026-08-20, and
probed live 2026-08-21):
    - Base URLs: https://demo.trading212.com (paper), https://live.trading212.com
      (spec servers block, yaml L2205-2207).
    - Auth: either Basic key:secret or the legacy raw API key in the
      Authorization header (yaml L810-818). The stored key was probed
      2026-08-21: legacy header works against the live host.
    - Order placement endpoints are NOT idempotent and there is no client
      order id (yaml L1651-1653 and peers). Therefore this module NEVER
      retries a POST to an order endpoint; an ambiguous outcome raises
      OrderSubmitAmbiguousError and the caller must reconcile through
      GET /equity/orders before doing anything else.
    - Rate limits are per account, not per key or IP (docs, yaml L1066-1070).
      Ceilings per endpoint are encoded in RATE_LIMITS below; the token
      bucket applies common.net.SAFETY_RATIO (70%) headroom on top.

Public classes:
    T212Client                Client; one instance per process
    OrderSubmitAmbiguousError Raised when an order POST outcome is unknown

Public functions (methods of T212Client):
    account_summary()                     GET /equity/account/summary
    instruments()                         GET /equity/metadata/instruments
    exchanges()                           GET /equity/metadata/exchanges
    positions()                           GET /equity/positions
    pending_orders()                      GET /equity/orders
    order(order_id)                       GET /equity/orders/{id}
    place_market_order(ticker, quantity)  POST /equity/orders/market
    cancel_order(order_id)                DELETE /equity/orders/{id}
    history_orders(...)                   GET /equity/history/orders (one page)
    iter_history_orders(...)              Generator over history pages
"""

from __future__ import annotations

__all__ = ["T212Client", "OrderSubmitAmbiguousError",
           "T212_BASE_PAPER", "T212_BASE_LIVE", "RATE_LIMITS"]

import json
import time
from decimal import Decimal
from typing import Any, Iterator

import httpx

from common.config import ENV_LIVE, ENV_PAPER, assert_live_allowed
from common.logging_setup import get_logger
from common.net import (PermanentError, RateLimitError, TokenBucket,
                        TransientError, backoff_seconds, RETRY_MAX_ATTEMPTS)
from common.secrets import get_secret, mask

log = get_logger("t212.client")

# ============================================================================
# [1] Constants
# ============================================================================

# Source: OpenAPI mirror servers block (yaml L2205-2207) + docs API
# Environments section (yaml L858-861), retrieved 2026-08-20.
T212_BASE_PAPER = "https://demo.trading212.com"
T212_BASE_LIVE = "https://live.trading212.com"

TIMEOUT_REST_SEC = 30.0

# Requests per second per endpoint group. Source: per-endpoint "Rate limit"
# lines in the OpenAPI mirror (retrieved 2026-08-20) and the live response
# headers probed 2026-08-21 (account summary: x-ratelimit-limit=1,
# x-ratelimit-period=5). The TokenBucket multiplies by SAFETY_RATIO.
RATE_LIMITS: dict[str, float] = {
    "account_summary": 1 / 5,     # 1 req / 5 s
    "instruments": 1 / 50,        # 1 req / 50 s
    "exchanges": 1 / 30,          # 1 req / 30 s
    "positions": 1 / 1,           # 1 req / 1 s
    "orders_pending": 1 / 5,      # 1 req / 5 s
    "order_by_id": 1 / 1,         # 1 req / 1 s
    "order_market": 50 / 60,      # 50 req / 1 min
    "order_cancel": 50 / 60,      # 50 req / 1 min
    "history_orders": 6 / 60,     # 6 req / 1 min
}

_RETRYABLE_STATUS = (408, 429, 500, 502, 503, 504)


class OrderSubmitAmbiguousError(Exception):
    """An order POST failed in a way that does not prove the venue rejected it.

    The order may or may not exist at the venue (the endpoint is not
    idempotent and there is no client order id). The caller MUST reconcile
    through pending orders / order history before submitting anything else
    for the same symbol.
    """

    def __init__(self, ticker: str, quantity: Decimal, detail: str):
        super().__init__(f"ambiguous order submit outcome for {ticker}: {detail}")
        self.ticker = ticker
        self.quantity = quantity
        self.detail = detail


# ============================================================================
# [2] Client
# ============================================================================

class T212Client:
    """Trading 212 REST client bound to one environment.

    Args:
        env: "paper" (demo host) or "live". The stored API key was verified
            2026-08-21 to authenticate against the live host only; paper
            requires a separate practice-account key.
        cfg: The loaded venue configuration. Required when placing orders so
            the live gate (common.config.assert_live_allowed) can run; may be
            None for read-only use.
        secret_name: Credential name for common.secrets.get_secret.
    """

    def __init__(self, env: str, cfg: dict[str, Any] | None = None,
                 secret_name: str = "trading212_api_key") -> None:
        if env not in (ENV_PAPER, ENV_LIVE):
            raise ValueError(f"unknown env {env!r}")
        self.env = env
        self.base = T212_BASE_LIVE if env == ENV_LIVE else T212_BASE_PAPER
        self._cfg = cfg
        key = get_secret(secret_name)
        assert key is not None
        self._session = httpx.Client(
            base_url=self.base,
            headers={"Authorization": key},
            timeout=TIMEOUT_REST_SEC,
        )
        self._buckets = {name: TokenBucket(rate) for name, rate in RATE_LIMITS.items()}
        log.info("[client] env=%s base=%s key=%s", env, self.base, mask(key))

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "T212Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # [3] Read endpoints (idempotent, retried)
    # ------------------------------------------------------------------

    def account_summary(self) -> dict[str, Any]:
        """Account cash and investment totals in the primary account currency."""
        return self._get_retrying("account_summary", "/api/v0/equity/account/summary")

    def instruments(self) -> list[dict[str, Any]]:
        """All tradable instruments. Venue refreshes this every 10 minutes."""
        return self._get_retrying("instruments", "/api/v0/equity/metadata/instruments")

    def exchanges(self) -> list[dict[str, Any]]:
        """Exchanges with their working schedules (the venue's market calendar)."""
        return self._get_retrying("exchanges", "/api/v0/equity/metadata/exchanges")

    def positions(self) -> list[dict[str, Any]]:
        """All open positions for the account (strategy-owned or not)."""
        return self._get_retrying("positions", "/api/v0/equity/positions")

    def pending_orders(self) -> list[dict[str, Any]]:
        """All currently active orders. No pagination exists on this endpoint."""
        return self._get_retrying("orders_pending", "/api/v0/equity/orders")

    def order(self, order_id: int) -> dict[str, Any] | None:
        """One pending order by id; None when the venue reports 404.

        A 404 does NOT mean the order never existed: filled or canceled
        orders leave this endpoint and appear in history. The caller decides
        what a 404 means from its own context.
        """
        try:
            return self._get_retrying("order_by_id", f"/api/v0/equity/orders/{order_id}")
        except PermanentError as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise

    def history_orders(self, cursor: int | None = None, ticker: str | None = None,
                       limit: int = 50) -> dict[str, Any]:
        """One page of historical orders with their fills and itemized taxes."""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if ticker is not None:
            params["ticker"] = ticker
        return self._get_retrying("history_orders", "/api/v0/equity/history/orders",
                                  params=params)

    def iter_history_orders(self, ticker: str | None = None,
                            max_pages: int = 40) -> Iterator[dict[str, Any]]:
        """Yield historical order items newest-first, following nextPagePath.

        max_pages bounds the walk (40 pages x 50 items covers far more than a
        daily strategy writes between reconciliations); hitting the bound is
        logged so truncation is never silent.
        """
        page = self.history_orders(ticker=ticker)
        for _ in range(max_pages):
            for item in page.get("items", []):
                yield item
            next_path = page.get("nextPagePath")
            if not next_path:
                return
            page = self._get_retrying("history_orders", next_path)
        log.warning("[history] pagination stopped at max_pages=%s ticker=%s",
                    max_pages, ticker)

    # ------------------------------------------------------------------
    # [4] Order endpoints (NOT idempotent, never retried)
    # ------------------------------------------------------------------

    def place_market_order(self, ticker: str, quantity: Decimal,
                           extended_hours: bool = False) -> dict[str, Any]:
        """Submit one market order. Sell = negative quantity (venue convention).

        Placed while the market is closed, the order is queued to execute at
        the next open (OpenAPI mirror, market order description). Exactly one
        HTTP attempt is made: any outcome that does not prove rejection
        raises OrderSubmitAmbiguousError for the caller to reconcile.

        Money red line (CLAUDE.md section 3.1): this method refuses to run
        without a loaded configuration whose execution.dry_run is False, and
        in the live environment it additionally passes assert_live_allowed.
        The DRY_RUN short circuit lives in the order router; reaching this
        method in dry-run mode is a bug and raises.
        """
        self._assert_order_allowed()
        payload = {"ticker": ticker, "quantity": float(quantity),
                   "extendedHours": extended_hours}
        return self._post_order_once("order_market", "/api/v0/equity/orders/market",
                                     payload, ticker, quantity)

    def cancel_order(self, order_id: int) -> None:
        """Request cancellation of one pending order.

        A 200 means the request was ACCEPTED, not that the order is
        canceled (spec, cancel description); the caller must poll the order
        to a terminal state. Cancelling an already-terminal order surfaces
        as PermanentError(404) which the caller interprets.
        """
        self._assert_order_allowed()
        self._buckets["order_cancel"].acquire()
        response = self._session.delete(f"/api/v0/equity/orders/{order_id}")
        if response.status_code == 200:
            log.info("[cancel] order_id=%s accepted", order_id)
            return
        raise self._error_for(response, f"DELETE orders/{order_id}")

    # ------------------------------------------------------------------
    # [5] Internals
    # ------------------------------------------------------------------

    def _assert_order_allowed(self) -> None:
        """Refuse order traffic unless the configuration explicitly arms it."""
        if self._cfg is None:
            raise PermanentError("order endpoints need the venue configuration; "
                                 "read-only client refuses to trade")
        execution = self._cfg.get("execution") or {}
        if execution.get("dry_run", True):
            raise PermanentError("execution.dry_run is enabled; the client must "
                                 "never see an order call in dry-run mode")
        if self.env == ENV_LIVE:
            assert_live_allowed(self._cfg)

    def _post_order_once(self, bucket: str, path: str, payload: dict[str, Any],
                         ticker: str, quantity: Decimal) -> dict[str, Any]:
        """Single-attempt order POST with the ambiguity contract."""
        self._buckets[bucket].acquire()
        log.info("[submit] path=%s ticker=%s qty=%s env=%s", path, ticker,
                 quantity, self.env)
        try:
            response = self._session.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise OrderSubmitAmbiguousError(ticker, quantity,
                                            f"transport failure: {exc!r}") from exc
        if response.status_code == 200:
            # A 200 means the venue ACCEPTED the order. From here on, any
            # failure to understand the response is still an accepted order:
            # it must surface as ambiguity, never as a rejection.
            try:
                order = response.json()
                order["id"] = int(order["id"])
                return order
            except Exception as exc:
                raise OrderSubmitAmbiguousError(
                    ticker, quantity,
                    f"venue accepted (200) but the response is unusable: "
                    f"{exc!r}; body {response.text[:200]}") from exc
        if response.status_code in (400, 401, 403):
            # Proven rejection: the venue answered and refused.
            raise self._error_for(response, path)
        # 408 / 429 / 5xx and anything else: outcome unknown.
        raise OrderSubmitAmbiguousError(
            ticker, quantity,
            f"HTTP {response.status_code}: {response.text[:200]}")

    def _get_retrying(self, bucket: str, path: str,
                      params: dict[str, Any] | None = None) -> Any:
        """GET with token-bucket pacing and bounded retry (idempotent only)."""
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            self._buckets[bucket].acquire()
            try:
                response = self._session.get(path, params=params)
            except httpx.HTTPError as exc:
                if attempt == RETRY_MAX_ATTEMPTS:
                    raise TransientError(f"GET {path}: {exc!r}") from exc
                time.sleep(backoff_seconds(attempt))
                continue
            if response.status_code == 200:
                return response.json()
            if response.status_code in _RETRYABLE_STATUS:
                if attempt == RETRY_MAX_ATTEMPTS:
                    raise RateLimitError(path) if response.status_code == 429 \
                        else TransientError(f"GET {path}: HTTP {response.status_code}")
                time.sleep(backoff_seconds(attempt, self._retry_after(response)))
                continue
            raise self._error_for(response, path)
        raise TransientError(f"GET {path}: retries exhausted")  # pragma: no cover

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        """Seconds until the venue's advertised reset, from the limit headers."""
        reset = response.headers.get("x-ratelimit-reset")
        if reset is None:
            return None
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            return None

    @staticmethod
    def _error_for(response: httpx.Response, path: str) -> PermanentError:
        """Build a PermanentError carrying the status and a body snippet."""
        try:
            body = json.dumps(response.json())[:300]
        except Exception:
            body = response.text[:300]
        error = PermanentError(f"{path}: HTTP {response.status_code} {body}")
        error.status_code = response.status_code  # type: ignore[attr-defined]
        return error
