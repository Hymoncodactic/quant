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

Constants:
    T212_BASE_PAPER / T212_BASE_LIVE  str  Venue hosts. Source: the OpenAPI
                                      mirror's servers block, retrieved
                                      2026-08-20.
    TIMEOUT_REST_SEC  float  30.0, the default per-request timeout. Suits a
                             batch caller; an interactive one passes its own.
    RATE_LIMITS       dict   Requests per second per endpoint group. Source:
                             the per-endpoint rate-limit lines in the same
                             mirror, plus the live response headers probed
                             2026-08-21.

Inputs:
    The Trading 212 public API v0.
Outputs:
    None.

Change log:
    2026-08-21  Created.
    2026-08-23  timeout_sec and max_attempts parameterized so an interactive
                caller can fail fast on an unreachable venue; order placement
                stays single-attempt whatever they are set to.
"""

from __future__ import annotations

__all__ = ["T212Client", "OrderSubmitAmbiguousError",
           "T212_BASE_PAPER", "T212_BASE_LIVE", "RATE_LIMITS"]

import json
import time
from decimal import Decimal
from email.utils import parsedate_to_datetime
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
        timeout_sec: Per-request timeout. The default suits a batch job that
            would rather wait than fail; an interactive caller should pass a
            short one so an unreachable venue cannot stall it.
        max_attempts: Retry budget for IDEMPOTENT reads only. Order
            placement is never retried whatever this is set to. Pass 1 for
            an interactive caller: on a dead venue the default budget spends
            close to a minute in back-off before giving up.
    """

    def __init__(self, env: str, cfg: dict[str, Any] | None = None,
                 secret_name: str = "trading212_api_key",
                 timeout_sec: float = TIMEOUT_REST_SEC,
                 max_attempts: int = RETRY_MAX_ATTEMPTS) -> None:
        if env not in (ENV_PAPER, ENV_LIVE):
            raise ValueError(f"unknown env {env!r}")
        self.env = env
        self.base = T212_BASE_LIVE if env == ENV_LIVE else T212_BASE_PAPER
        self._cfg = cfg
        self._max_attempts = max(1, int(max_attempts))
        key = get_secret(secret_name)
        assert key is not None
        self._session = httpx.Client(
            base_url=self.base,
            headers={"Authorization": key},
            timeout=timeout_sec,
        )
        self._buckets = {name: TokenBucket(rate) for name, rate in RATE_LIMITS.items()}
        self._last_response_date: str | None = None
        self._last_response_at: float | None = None
        log.info("[client] env=%s base=%s key=%s", env, self.base, mask(key))

    def last_clock_skew_sec(self) -> float | None:
        """Local clock minus the venue clock at the last successful GET.

        Derived from the HTTP Date header (one-second resolution, GMT);
        positive means the local clock runs ahead. None when no successful
        GET has happened yet or the header was absent -- callers treat that
        as "cannot be evaluated", not as zero skew.
        """
        if self._last_response_date is None or self._last_response_at is None:
            return None
        try:
            venue = parsedate_to_datetime(self._last_response_date)
        except (TypeError, ValueError):
            return None
        return self._last_response_at - venue.timestamp()

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

    def history_transactions(self, cursor: str | None = None,
                             limit: int = 50) -> dict[str, Any]:
        """One page of cash movements: deposits, withdrawals, fees, interest.

        The transactions cursor is a STRING while the orders and dividends
        cursors are integers (spec, pagination section), so the two cursor
        types must not share a code path.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return self._get_retrying("history_orders",
                                  "/api/v0/equity/history/transactions",
                                  params=params)

    def history_dividends(self, cursor: int | None = None,
                          ticker: str | None = None,
                          limit: int = 50) -> dict[str, Any]:
        """One page of dividend payments."""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if ticker is not None:
            params["ticker"] = ticker
        return self._get_retrying("history_orders",
                                  "/api/v0/equity/history/dividends",
                                  params=params)

    def follow_page(self, bucket: str, next_path: str,
                    base_path: str | None = None) -> dict[str, Any]:
        """Fetch a nextPagePath, repairing the one endpoint that omits its path.

        The documentation says to use the string verbatim, and for dividends
        and orders that works: they return a full path such as
        "/api/v0/equity/history/dividends?limit=1&cursor=160534735".
        TRANSACTIONS returns only the query string --
        "limit=2&cursor=<uuid>&time=<iso>" -- with no path and no leading
        question mark, so using it verbatim requests a path that does not
        exist and the venue answers 403 with an HTML page. Measured against
        the live account 2026-08-23; not in the OpenAPI mirror.

        base_path supplies the endpoint to graft such a fragment onto.
        """
        if next_path.startswith("/"):
            return self._get_retrying(bucket, next_path)
        if base_path is None:
            raise ValueError(
                f"nextPagePath {next_path!r} carries no path and no base_path "
                f"was given to graft it onto")
        joiner = "&" if "?" in base_path else "?"
        return self._get_retrying(bucket, f"{base_path}{joiner}{next_path}")

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
        for attempt in range(1, self._max_attempts + 1):
            self._buckets[bucket].acquire()
            try:
                response = self._session.get(path, params=params)
            except httpx.HTTPError as exc:
                if attempt == self._max_attempts:
                    raise TransientError(f"GET {path}: {exc!r}") from exc
                time.sleep(backoff_seconds(attempt))
                continue
            if response.status_code == 200:
                self._last_response_date = response.headers.get("date")
                self._last_response_at = time.time()
                return response.json()
            if response.status_code in _RETRYABLE_STATUS:
                if attempt == self._max_attempts:
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
