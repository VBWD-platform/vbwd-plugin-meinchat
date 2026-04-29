"""Tests for RateLimiter — in-memory counter backend for test isolation."""
import pytest

from plugins.meinchat.meinchat.services.rate_limiter import (
    InMemoryCounterBackend,
    RateLimitExceeded,
    RateLimiter,
)


@pytest.fixture
def backend():
    return InMemoryCounterBackend()


@pytest.fixture
def limiter(backend):
    return RateLimiter(backend)


class TestRateLimiter:
    def test_allows_up_to_limit(self, limiter):
        for _ in range(5):
            limiter.check("send", user_id="alice", limit=5, window_seconds=60)
        # still within window, sixth raises
        with pytest.raises(RateLimitExceeded):
            limiter.check("send", user_id="alice", limit=5, window_seconds=60)

    def test_separate_users_have_separate_counters(self, limiter):
        for _ in range(3):
            limiter.check("send", user_id="alice", limit=3, window_seconds=60)
        # bob still has his own quota
        for _ in range(3):
            limiter.check("send", user_id="bob", limit=3, window_seconds=60)
        with pytest.raises(RateLimitExceeded):
            limiter.check("send", user_id="alice", limit=3, window_seconds=60)

    def test_separate_categories_have_separate_counters(self, limiter):
        for _ in range(3):
            limiter.check("send", user_id="alice", limit=3, window_seconds=60)
        # attachment is its own category
        limiter.check("attach", user_id="alice", limit=1, window_seconds=60)
        with pytest.raises(RateLimitExceeded):
            limiter.check("attach", user_id="alice", limit=1, window_seconds=60)

    def test_window_resets_after_expiry(self, limiter, backend):
        # Simulate the passage of time via the backend's `now()` hook.
        limiter.check("send", user_id="alice", limit=1, window_seconds=60)
        with pytest.raises(RateLimitExceeded):
            limiter.check("send", user_id="alice", limit=1, window_seconds=60)

        backend.advance_time(61)
        limiter.check("send", user_id="alice", limit=1, window_seconds=60)

    def test_retry_after_is_reported_on_exception(self, limiter):
        limiter.check("send", user_id="alice", limit=1, window_seconds=60)
        with pytest.raises(RateLimitExceeded) as exc_info:
            limiter.check("send", user_id="alice", limit=1, window_seconds=60)
        # retry_after must be a positive int ≤ window
        assert 0 < exc_info.value.retry_after_seconds <= 60
