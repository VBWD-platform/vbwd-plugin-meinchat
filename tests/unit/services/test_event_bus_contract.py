"""S38 — one contract, both event-bus backends (Liskov).

The same observable behaviour is asserted against the in-process
`MeinchatEventBus` and the cross-worker `RedisEventBus`. The Redis cases are
skipped (not failed) when Redis is unreachable — no silent skips
(feedback_ci_precommit_lessons): the skip reason is explicit.
"""
import time
import uuid
from typing import List

import pytest

from plugins.meinchat.meinchat.services.event_bus import MeinchatEventBus
from plugins.meinchat.meinchat.services.event_bus_base import Subscription
from plugins.meinchat.meinchat.services.redis_event_bus import RedisEventBus


def _redis_client_or_skip():
    try:
        from vbwd.utils.redis_client import RedisClient

        client = RedisClient().client
        client.ping()
        return client
    except Exception as exc:  # pragma: no cover - infra-dependent
        pytest.skip(f"Redis not available for RedisEventBus contract: {exc}")


@pytest.fixture(params=["memory", "redis"])
def bus(request):
    if request.param == "memory":
        yield MeinchatEventBus()
        return
    client = _redis_client_or_skip()
    instance = RedisEventBus(client, channel_prefix=f"mc_test_{uuid.uuid4().hex[:8]}:")
    yield instance
    instance.stop()


def _publish(bus, channel: str, event: dict) -> None:
    """Publish, ensuring an async (Redis) listener is active first."""
    if hasattr(bus, "wait_listening"):
        assert bus.wait_listening(timeout=2.0)
    bus.publish(channel, event)


def _collect(sub: Subscription, expected: int, deadline: float) -> List[dict]:
    out: List[dict] = []
    for event in sub.iter_events(timeout=deadline):
        if event.get("type") == "heartbeat":
            continue
        out.append(event)
        if len(out) >= expected:
            break
    return out


class TestEventBusContract:
    def test_subscriber_receives_published_event(self, bus):
        sub = bus.subscribe("user:123")
        _publish(bus, "user:123", {"type": "message", "body": "hi"})
        assert _collect(sub, 1, 2.0) == [{"type": "message", "body": "hi"}]

    def test_channel_scoping_isolates_users(self, bus):
        sub_a = bus.subscribe("user:aaa")
        sub_b = bus.subscribe("user:bbb")
        _publish(bus, "user:aaa", {"for": "a"})
        assert _collect(sub_a, 1, 2.0) == [{"for": "a"}]
        # b receives nothing within a short window
        assert _collect(sub_b, 1, 0.3) == []

    def test_multiple_subscribers_same_channel_all_receive(self, bus):
        sub1 = bus.subscribe("user:xyz")
        sub2 = bus.subscribe("user:xyz")
        _publish(bus, "user:xyz", {"hello": "world"})
        assert _collect(sub1, 1, 2.0) == [{"hello": "world"}]
        assert _collect(sub2, 1, 2.0) == [{"hello": "world"}]

    def test_unsubscribe_stops_future_delivery(self, bus):
        sub = bus.subscribe("user:gone")
        if hasattr(bus, "wait_listening"):
            assert bus.wait_listening(timeout=2.0)
        sub.close()
        bus.publish("user:gone", {"type": "message"})
        assert _collect(sub, 1, 0.3) == []

    def test_publish_without_subscribers_is_a_noop(self, bus):
        # No raise, no hang. No subscribers → no listener is started; publish
        # directly (don't wait for a listener that intentionally never runs).
        bus.publish("user:nobody", {"type": "message"})

    def test_channel_count_tracks_active_subscriptions(self, bus):
        assert bus.channel_count() == 0
        sub = bus.subscribe("user:count")
        assert bus.channel_count() == 1
        sub.close()
        assert bus.channel_count() == 0

    def test_heartbeat_fires_at_configured_interval(self, bus):
        sub = bus.subscribe("user:hb", heartbeat_seconds=0.1)
        start = time.time()
        beats = []
        for event in sub.iter_events(timeout=0.35):
            if event.get("type") == "heartbeat":
                beats.append(time.time() - start)
            if len(beats) >= 2:
                break
        assert len(beats) >= 2

    def test_iter_events_honours_lifetime_cap(self, bus):
        # Heartbeat would loop forever; the timeout must win (S-incident fix).
        sub = bus.subscribe("user:cap", heartbeat_seconds=0.05)
        start = time.time()
        events = list(sub.iter_events(timeout=0.2))
        assert time.time() - start < 1.5
        assert all(e.get("type") == "heartbeat" for e in events)


class TestSubscribeMany:
    """S86.1 D5 — one stream fans events from several channels (a user's own
    channel plus each of their rooms) into ONE subscription, reusing the same
    queue/iter_events machinery (no second stream)."""

    def test_single_subscription_receives_from_every_channel(self, bus):
        sub = bus.subscribe_many(["user:m1", "room:r1", "room:r2"])
        _publish(bus, "room:r1", {"src": "r1"})
        _publish(bus, "user:m1", {"src": "u"})
        received = {event["src"] for event in _collect(sub, 2, 2.0)}
        assert received == {"r1", "u"}

    def test_does_not_receive_unsubscribed_channels(self, bus):
        sub = bus.subscribe_many(["user:m2", "room:rA"])
        _publish(bus, "room:rZ", {"src": "rZ"})
        assert _collect(sub, 1, 0.3) == []

    def test_close_unsubscribes_from_all_channels(self, bus):
        sub = bus.subscribe_many(["user:m3", "room:rB"])
        if hasattr(bus, "wait_listening"):
            assert bus.wait_listening(timeout=2.0)
        sub.close()
        assert bus.channel_count() == 0
        bus.publish("room:rB", {"src": "rB"})
        assert _collect(sub, 1, 0.3) == []
