"""In-process pub/sub bus for SSE fan-out.

Scope constraint (documented in sprint 57): events only reach subscribers
inside the same gunicorn worker. Multi-worker scale requires swapping
`MeinchatEventBus` for a Redis-pub/sub backed implementation. Interface
stays the same.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Dict, Iterator, Optional


class _Subscription:
    """One client's mailbox. Owns the queue the bus publishes into."""

    def __init__(
        self,
        bus: "MeinchatEventBus",
        channel: str,
        heartbeat_seconds: Optional[float] = None,
    ) -> None:
        self._bus = bus
        self._channel = channel
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._closed = False
        self._heartbeat_seconds = heartbeat_seconds

    @property
    def channel(self) -> str:
        return self._channel

    def deliver(self, event: Dict[str, Any]) -> None:
        if not self._closed:
            self._queue.put(event)

    def iter_events(
        self, timeout: Optional[float] = None
    ) -> Iterator[Dict[str, Any]]:
        """Yield queued events until `timeout` seconds pass with no event.

        If `heartbeat_seconds` was set at subscribe time, yields a
        `{"type": "heartbeat"}` sentinel when the queue stays empty that
        long — the SSE route writes a keep-alive line each time.
        """
        import time

        deadline = time.time() + timeout if timeout is not None else None
        hb = self._heartbeat_seconds
        while not self._closed:
            # One wait cycle = shorter of (remaining deadline, heartbeat).
            now = time.time()
            if deadline is not None and now >= deadline:
                return
            wait = None
            if hb is not None:
                wait = hb
            if deadline is not None:
                remaining = deadline - now
                wait = remaining if wait is None else min(wait, remaining)
            try:
                event = self._queue.get(timeout=wait)
            except queue.Empty:
                if hb is not None:
                    yield {"type": "heartbeat"}
                continue
            yield event

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._unsubscribe(self)


class MeinchatEventBus:
    """Thread-safe channel-to-subscribers registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: Dict[str, list[_Subscription]] = {}

    def subscribe(
        self,
        channel: str,
        heartbeat_seconds: Optional[float] = None,
    ) -> _Subscription:
        sub = _Subscription(self, channel, heartbeat_seconds=heartbeat_seconds)
        with self._lock:
            self._subs.setdefault(channel, []).append(sub)
        return sub

    def publish(self, channel: str, event: Dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs.get(channel, ()))
        for sub in subs:
            sub.deliver(event)

    def _unsubscribe(self, sub: _Subscription) -> None:
        with self._lock:
            bucket = self._subs.get(sub.channel)
            if bucket is None:
                return
            try:
                bucket.remove(sub)
            except ValueError:
                return
            if not bucket:
                del self._subs[sub.channel]

    # Handy for tests / health endpoints
    def channel_count(self) -> int:
        with self._lock:
            return len(self._subs)
