"""Per-user sliding-window rate limiter with pluggable counter backend.

Production uses Redis (INCR + EXPIRE). Tests use an in-memory dict via
`InMemoryCounterBackend` so no Redis is required.
"""
import time
from abc import ABC, abstractmethod
from typing import Tuple


class RateLimitExceeded(Exception):
    """Raised when a user is over their quota for a category."""

    def __init__(self, category: str, retry_after_seconds: int) -> None:
        super().__init__(
            f"rate limit for '{category}' exceeded — retry in {retry_after_seconds}s"
        )
        self.category = category
        self.retry_after_seconds = retry_after_seconds


class CounterBackend(ABC):
    """Abstract backend for integer counters scoped to a TTL window."""

    @abstractmethod
    def incr(self, key: str, ttl_seconds: int) -> Tuple[int, int]:
        """Increment the counter at `key`, set TTL to `ttl_seconds` if the
        key is new. Returns (new_count, remaining_ttl_seconds)."""


class InMemoryCounterBackend(CounterBackend):
    """Test backend — a dict of {key: (count, expires_at_unix)}.

    `advance_time(seconds)` lets tests move past the window without sleeping.
    """

    def __init__(self) -> None:
        self._store: dict[str, Tuple[int, float]] = {}
        self._clock_offset_seconds: float = 0.0

    def advance_time(self, seconds: float) -> None:
        self._clock_offset_seconds += seconds

    def _now(self) -> float:
        return time.time() + self._clock_offset_seconds

    def incr(self, key: str, ttl_seconds: int) -> Tuple[int, int]:
        now = self._now()
        existing = self._store.get(key)
        if existing is None or existing[1] <= now:
            expires_at = now + ttl_seconds
            self._store[key] = (1, expires_at)
            return 1, ttl_seconds
        count, expires_at = existing
        count += 1
        self._store[key] = (count, expires_at)
        return count, max(1, int(expires_at - now))


class RedisCounterBackend(CounterBackend):
    """Production backend. Uses Redis INCR + EXPIRE (only if new)."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    def incr(self, key: str, ttl_seconds: int) -> Tuple[int, int]:
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = pipe.execute()
        if ttl < 0:
            self._redis.expire(key, ttl_seconds)
            ttl = ttl_seconds
        return int(count), int(ttl)


class RateLimiter:
    """Category + user-scoped counter wrapper.

    Key shape: meinchat:rate:<category>:<user_id>
    """

    def __init__(self, backend: CounterBackend) -> None:
        self._backend = backend

    def check(
        self,
        category: str,
        *,
        user_id,
        limit: int,
        window_seconds: int,
    ) -> None:
        key = f"meinchat:rate:{category}:{user_id}"
        count, remaining_ttl = self._backend.incr(key, window_seconds)
        if count > limit:
            raise RateLimitExceeded(category, remaining_ttl)
