"""In-process pub/sub bus for SSE fan-out.

Correct ONLY when a single gunicorn worker handles both the publisher and the
subscriber (e.g. `GUNICORN_WORKERS=1` or unit-test harnesses). For multi-worker
fan-out use `RedisEventBus`. Both satisfy the `EventBus` port in
`event_bus_base` and deliver into the same queue-based `Subscription`.

Scope constraint (documented in sprint 57): events only reach subscribers
inside the same gunicorn worker.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Sequence

from plugins.meinchat.meinchat.services.event_bus_base import Subscription


class MeinchatEventBus:
    """Thread-safe, single-process channel-to-subscribers registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: Dict[str, List[Subscription]] = {}

    def subscribe(
        self,
        channel: str,
        heartbeat_seconds: Optional[float] = None,
    ) -> Subscription:
        return self.subscribe_many([channel], heartbeat_seconds=heartbeat_seconds)

    def subscribe_many(
        self,
        channels: Sequence[str],
        heartbeat_seconds: Optional[float] = None,
    ) -> Subscription:
        """Register ONE subscription under several channels (S86.1 D5 — a
        user's own channel plus each of their rooms feed the same stream).
        De-duplicated so a channel listed twice delivers once."""
        unique_channels = list(dict.fromkeys(channels))
        sub = Subscription(
            unique_channels[0] if unique_channels else "",
            heartbeat_seconds=heartbeat_seconds,
            on_close=self._unsubscribe,
            channels=unique_channels,
        )
        with self._lock:
            for channel in unique_channels:
                self._subs.setdefault(channel, []).append(sub)
        return sub

    def publish(self, channel: str, event: Dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs.get(channel, ()))
        for sub in subs:
            sub.deliver(event)

    def _unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            for channel in sub.channels:
                bucket = self._subs.get(channel)
                if bucket is None:
                    continue
                try:
                    bucket.remove(sub)
                except ValueError:
                    continue
                if not bucket:
                    del self._subs[channel]

    # Handy for tests / health endpoints
    def channel_count(self) -> int:
        with self._lock:
            return len(self._subs)
