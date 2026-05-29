"""S38 integration — RedisEventBus across simulated gunicorn workers.

Headline proof: an event published by one worker reaches a subscriber on a
DIFFERENT worker (the pre-S38 in-process bus could not do this). Plus the
connection-count regression: N open streams add ~1 Redis connection (the single
per-worker listener), not N.

Requires a real Redis (present in the test container). Skips with an explicit
reason otherwise.
"""
import threading
import uuid
from typing import List

import pytest

from plugins.meinchat.meinchat.services.event_bus_base import Subscription
from plugins.meinchat.meinchat.services.redis_event_bus import RedisEventBus


def _client_or_skip():
    try:
        from vbwd.utils.redis_client import RedisClient

        client = RedisClient().client
        client.ping()
        return client
    except Exception as exc:
        pytest.skip(f"Redis not available: {exc}")


def _collect(sub: Subscription, expected: int, deadline: float) -> List[dict]:
    out: List[dict] = []
    for event in sub.iter_events(timeout=deadline):
        if event.get("type") == "heartbeat":
            continue
        out.append(event)
        if len(out) >= expected:
            break
    return out


def test_event_published_on_one_worker_reaches_another_worker():
    prefix = f"mc_xw_{uuid.uuid4().hex[:8]}:"
    worker_a = RedisEventBus(_client_or_skip(), channel_prefix=prefix)
    worker_b = RedisEventBus(_client_or_skip(), channel_prefix=prefix)
    try:
        sub = worker_b.subscribe("user:x")
        assert worker_b.wait_listening(timeout=2.0)
        worker_a.publish("user:x", {"type": "message", "body": "cross"})
        assert _collect(sub, 1, 2.0) == [{"type": "message", "body": "cross"}]
    finally:
        worker_a.stop()
        worker_b.stop()


def test_one_listener_connection_regardless_of_stream_count():
    probe = _client_or_skip()
    prefix = f"mc_cc_{uuid.uuid4().hex[:8]}:"
    before = int(probe.info("clients")["connected_clients"])
    bus = RedisEventBus(_client_or_skip(), channel_prefix=prefix)
    try:
        subs = [bus.subscribe(f"user:{i}") for i in range(50)]
        assert bus.wait_listening(timeout=2.0)
        assert bus.channel_count() == 50

        # Exactly one listener thread feeds all 50 subscriptions.
        assert bus._listener_thread is not None
        assert bus._listener_thread.is_alive()

        during = int(probe.info("clients")["connected_clients"])
        # 50 streams must NOT add ~50 connections — only the listener (+ probe).
        assert during - before < 15

        for sub in subs:
            sub.close()
        assert bus.channel_count() == 0
    finally:
        bus.stop()


def test_listener_thread_stops_cleanly():
    bus = RedisEventBus(
        _client_or_skip(), channel_prefix=f"mc_st_{uuid.uuid4().hex[:8]}:"
    )
    bus.subscribe("user:z")
    assert bus.wait_listening(timeout=2.0)
    bus.stop()
    # Give the daemon a moment to wind down past its poll cycle.
    thread = bus._listener_thread
    assert thread is not None
    thread.join(timeout=3.0)
    assert not thread.is_alive()
    assert "meinchat-redis-listener" not in {
        t.name for t in threading.enumerate() if t is thread
    }
