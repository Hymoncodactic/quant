"""网络基础：指数退避与客户端主动限频（quant-error-handling §2）。

设计要点：
    - 只有幂等操作才允许自动重试；下单**不是**幂等操作，必须带 client order id
      并由调用方自行处理，本模块不提供下单重试。
    - 限频采用令牌桶主动限速，不靠撞 429 被动退避。
    - 具体速率上限属 S4 事实，由各场所模块按官方文档填入，本模块不预设数值。

对外函数：
    backoff_seconds(attempt, retry_after=None)  第 attempt 次重试前应等待的秒数

对外类：
    TokenBucket(rate_per_sec, burst=None)       令牌桶限频器，线程安全
    TransientError / RateLimitError             可重试异常
    PermanentError                              不可重试异常
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
SAFETY_RATIO = 0.7          # 实际速率取官方上限的比例，留余量

class TransientError(Exception):
    """可重试：网络 / 限频 / 5xx。"""

class RateLimitError(TransientError):
    def __init__(self, endpoint: str, retry_after: float | None = None):
        super().__init__(f"限频 {endpoint}")
        self.endpoint = endpoint
        self.retry_after = retry_after

class PermanentError(Exception):
    """不可重试：认证 / 业务拒单 / 配置。"""

def backoff_seconds(attempt: int, retry_after: float | None = None) -> float:
    """第 attempt 次重试前应等待的秒数（attempt 从 1 起）。

    优先采用服务端给出的 retry_after；否则指数退避并乘随机抖动，
    避免多进程同步重试造成请求风暴。
    """
    if attempt < 1:
        raise ValueError("attempt 从 1 起")
    if retry_after is not None:
        return min(float(retry_after), RETRY_MAX_SEC)
    base = min(RETRY_BASE_SEC * (2 ** (attempt - 1)), RETRY_MAX_SEC)
    return base * (0.5 + random.random())

class TokenBucket:
    """令牌桶限频器，线程安全。

    用法：每个端点分组一个实例（下单、查询、行情各一组），
    调用前 acquire()，桶空则阻塞到有令牌为止。

    Args:
        rate_per_sec: 官方允许的每秒请求数。实际速率会乘 SAFETY_RATIO。
        burst: 桶容量，允许的瞬时突发数。
    """

    def __init__(self, rate_per_sec: float, burst: int | None = None):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec 须为正")
        self.rate = rate_per_sec * SAFETY_RATIO
        self.capacity = float(burst if burst is not None else max(1.0, self.rate))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n: float = 1.0) -> None:
        """取 n 个令牌，不足则阻塞等待。"""
        if n > self.capacity:
            raise ValueError(f"单次请求令牌数 {n} 超过桶容量 {self.capacity}")
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
            time.sleep(wait)
