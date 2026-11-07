"""限流：固定窗口计数器，纯内存。

* 设备 token：默认 60 次/分钟；
* `/pairing`：5 次/分钟/IP（失败也计数）；
* `/pair`：10 次/分钟/IP（失败也计数）；
* 健康检查不计入。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .errors import HubError

PRUNE_EVERY_SECONDS = 300


@dataclass
class _Bucket:
    window_start: float
    count: int


class RateLimiter:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._last_prune = self._clock()

    def hit(self, key: str, limit: int, *, window: float = 60.0) -> tuple[bool, int]:
        """记一次。返回 `(allowed, retry_after_seconds)`。"""
        if limit <= 0:
            return True, 0
        now = self._clock()
        with self._lock:
            self._prune(now)
            bucket = self._buckets.get(key)
            if bucket is None or now - bucket.window_start >= window:
                self._buckets[key] = _Bucket(window_start=now, count=1)
                return True, 0
            if bucket.count >= limit:
                retry_after = max(1, int(window - (now - bucket.window_start) + 0.999))
                return False, retry_after
            bucket.count += 1
            return True, 0

    def check(self, key: str, limit: int, *, window: float = 60.0) -> None:
        allowed, retry_after = self.hit(key, limit, window=window)
        if not allowed:
            raise HubError(
                "rate_limited",
                "too many requests",
                headers={"Retry-After": str(retry_after)},
            )

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def _prune(self, now: float) -> None:
        if now - self._last_prune < PRUNE_EVERY_SECONDS:
            return
        self._last_prune = now
        stale = [k for k, b in self._buckets.items() if now - b.window_start >= 600]
        for key in stale:
            self._buckets.pop(key, None)

    def __len__(self) -> int:
        return len(self._buckets)
