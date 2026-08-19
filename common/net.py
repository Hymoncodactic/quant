"""Network primitives: exponential back-off and client-side rate limiting
(quant-error-handling section 2).

Design notes:
    - Only idempotent operations may be retried automatically. Order submission
      is not idempotent, so this module deliberately offers no order retry: that
      belongs with the caller, which must supply a client order id.
    - Rate limiting is enforced proactively with a token bucket rather than
      reactively on HTTP 429. Being throttled by the venue is a fault, not a
      control mechanism.
    - Concrete rate ceilings are venue facts, so each venue module supplies its
      own from official documentation. Nothing is assumed here.

Public functions:
    backoff_seconds(attempt, retry_after=None)  Seconds to wait before a retry

Public classes:
    TokenBucket(rate_per_sec, burst=None)       Thread-safe token-bucket limiter
    TransientError / RateLimitError             Retryable failures
    PermanentError                              Non-retryable failures
"""

from __future__ import annotations

__all__ = ["backoff_seconds", "TokenBucket",
           "TransientError", "RateLimitError", "PermanentError",
           "RETRY_MAX_ATTEMPTS", "RETRY_BASE_SEC", "RETRY_MAX_SEC", "SAFETY_RATIO"]

import random
import threading
import time

RETRY_MAX_ATTEMPTS = 5
RETRY_BASE_SEC = 0.5
RETRY_MAX_SEC = 30.0
SAFETY_RATIO = 0.7          # fraction of the venue's published ceiling we actually use


class TransientError(Exception):
    """Retryable: network fault, throttling, or a server-side 5xx."""


class RateLimitError(TransientError):
    """Throttled by the venue. Carries the server's retry hint when one was given."""

    def __init__(self, endpoint: str, retry_after: float | None = None):
        super().__init__(f"rate limited on {endpoint}")
        self.endpoint = endpoint
        self.retry_after = retry_after


class PermanentError(Exception):
    """Not retryable: authentication, business rejection, or misconfiguration."""


def backoff_seconds(attempt: int, retry_after: float | None = None) -> float:
    """Return the delay before retry number `attempt`, counting from one.

    A server-supplied retry_after always wins, since the venue knows when it will
    accept traffic again. Otherwise the delay grows exponentially and is
    multiplied by jitter, so that several processes throttled at the same instant
    do not retry in lockstep and re-create the burst that throttled them.
    """
    if attempt < 1:
        raise ValueError("attempt is one-based")
    if retry_after is not None:
        return min(float(retry_after), RETRY_MAX_SEC)
    base = min(RETRY_BASE_SEC * (2 ** (attempt - 1)), RETRY_MAX_SEC)
    return base * (0.5 + random.random())


class TokenBucket:
    """Thread-safe token-bucket rate limiter.

    Use one instance per endpoint group, since venues meter groups separately
    (market data, trading and account queries typically have distinct ceilings).
    Callers acquire before issuing a request and block while the bucket is empty.

    Args:
        rate_per_sec: The venue's published requests per second. The effective
            rate is this multiplied by SAFETY_RATIO to leave headroom.
        burst: Bucket capacity, i.e. the instantaneous burst allowed.
    """

    def __init__(self, rate_per_sec: float, burst: int | None = None):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self.rate = rate_per_sec * SAFETY_RATIO
        self.capacity = float(burst if burst is not None else max(1.0, self.rate))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: float = 1.0) -> None:
        """Take n tokens, blocking until they are available.

        Weighted endpoints pass their weight as n, so a single expensive call
        consumes proportionally more of the budget.
        """
        if n > self.capacity:
            raise ValueError(f"request of {n} tokens exceeds bucket capacity {self.capacity}")
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                wait = (n - self._tokens) / self.rate
            # Sleep outside the lock so other threads can still top up and proceed.
            time.sleep(wait)
