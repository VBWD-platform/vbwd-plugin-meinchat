"""Tests for MeinchatEventBus — in-process pub/sub for SSE."""
import threading
import time

import pytest

from plugins.meinchat.meinchat.services.event_bus import MeinchatEventBus


@pytest.fixture
def bus():
    return MeinchatEventBus()


class TestPubSub:
    def test_subscriber_receives_published_event(self, bus):
        received = []

        def worker(subscription):
            for event in subscription.iter_events(timeout=0.5):
                received.append(event)
                if len(received) == 1:
                    return

        sub = bus.subscribe("user:123")
        thread = threading.Thread(target=worker, args=(sub,))
        thread.start()
        # Give the subscriber a moment to reach `.iter_events`.
        time.sleep(0.05)
        bus.publish("user:123", {"type": "message", "body": "hi"})
        thread.join(timeout=1)

        assert received == [{"type": "message", "body": "hi"}]

    def test_publish_without_subscribers_is_a_noop(self, bus):
        # No raise, no hang.
        bus.publish("user:nobody", {"type": "message"})

    def test_channel_scoping_isolates_users(self, bus):
        sub_a = bus.subscribe("user:aaa")
        sub_b = bus.subscribe("user:bbb")

        bus.publish("user:aaa", {"for": "a"})

        a_events = list(sub_a.iter_events(timeout=0.05))
        b_events = list(sub_b.iter_events(timeout=0.05))

        assert len(a_events) == 1
        assert a_events[0]["for"] == "a"
        assert b_events == []

    def test_multiple_subscribers_on_same_channel_all_receive(self, bus):
        sub1 = bus.subscribe("user:xyz")
        sub2 = bus.subscribe("user:xyz")

        bus.publish("user:xyz", {"hello": "world"})

        e1 = list(sub1.iter_events(timeout=0.05))
        e2 = list(sub2.iter_events(timeout=0.05))

        assert len(e1) == 1 and len(e2) == 1
        assert e1[0] == e2[0]

    def test_unsubscribe_stops_future_delivery(self, bus):
        sub = bus.subscribe("user:xyz")
        sub.close()
        bus.publish("user:xyz", {"type": "message"})
        assert list(sub.iter_events(timeout=0.05)) == []


class TestHeartbeatEmitter:
    def test_heartbeats_fire_at_configured_interval(self, bus):
        """When no events arrive, iter_events yields a heartbeat sentinel
        every `heartbeat_seconds` so the SSE route can write a keepalive."""
        sub = bus.subscribe("user:hb", heartbeat_seconds=0.1)
        start = time.time()
        beats = []
        for event in sub.iter_events(timeout=0.35):
            if event.get("type") == "heartbeat":
                beats.append(time.time() - start)
            if len(beats) >= 2:
                break
        assert len(beats) >= 2
        # Second heartbeat should arrive roughly `heartbeat_seconds` after
        # the first — we give it a generous envelope for scheduler jitter.
        assert 0.05 < beats[1] - beats[0] < 0.5


class TestStreamLifetimeCap:
    """Regression: the SSE route must pass a finite `timeout` so a stream
    cannot run forever and pin a gunicorn worker. A heartbeat-enabled
    subscription used to loop indefinitely when iter_events() was called with
    no timeout (the prod worker-starvation freeze)."""

    def test_iter_events_terminates_at_deadline_despite_heartbeats(self, bus):
        # Heartbeat would otherwise re-loop forever; the timeout must win.
        sub = bus.subscribe("user:cap", heartbeat_seconds=0.05)
        start = time.time()
        events = list(sub.iter_events(timeout=0.2))
        elapsed = time.time() - start
        # Returns near the deadline, not never; only heartbeats, no real events.
        assert elapsed < 1.0
        assert all(e.get("type") == "heartbeat" for e in events)

    def test_iter_events_with_timeout_and_no_heartbeat_returns_when_idle(self, bus):
        sub = bus.subscribe("user:cap2")
        start = time.time()
        events = list(sub.iter_events(timeout=0.1))
        assert time.time() - start < 1.0
        assert events == []
