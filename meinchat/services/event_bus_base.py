"""Shared event-bus contract + the queue-based subscription (S38).

Both bus backends — the in-process `MeinchatEventBus` and the cross-worker
`RedisEventBus` — deliver into the SAME `Subscription`. The only difference is
who feeds its queue: the in-process bus delivers directly; the Redis bus's
single per-worker listener thread bridges Redis `PUBLISH` messages into it.

Keeping one subscription type is the DRY/Liskov core of S38: `iter_events`
(heartbeat + the S-incident lifetime cap) is implemented once, so the SSE route
is backend-agnostic and a backend swap is behaviour-preserving.
"""
from __future__ import annotations

import queue
import time
from typing import Any, Callable, Dict, Iterator, Optional, Protocol, runtime_checkable


class Subscription:
    """One client's mailbox. Owns the queue a bus delivers events into.

    `on_close` lets the owning bus unregister this subscription when the SSE
    stream ends — the subscription itself stays bus-agnostic.
    """

    def __init__(
        self,
        channel: str,
        heartbeat_seconds: Optional[float] = None,
        on_close: Optional[Callable[["Subscription"], None]] = None,
    ) -> None:
        self._channel = channel
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._closed = False
        self._heartbeat_seconds = heartbeat_seconds
        self._on_close = on_close

    @property
    def channel(self) -> str:
        return self._channel

    def deliver(self, event: Dict[str, Any]) -> None:
        if not self._closed:
            self._queue.put(event)

    def iter_events(self, timeout: Optional[float] = None) -> Iterator[Dict[str, Any]]:
        """Yield queued events, with optional heartbeat and lifetime cap.

        `timeout` is an absolute lifetime cap (computed once): the stream
        returns after at most `timeout` seconds regardless of activity, so a
        worker is never pinned forever (the S-incident fix). If
        `heartbeat_seconds` was set, a `{"type": "heartbeat"}` sentinel is
        yielded each time the queue stays empty that long.
        """
        deadline = time.time() + timeout if timeout is not None else None
        hb = self._heartbeat_seconds
        while not self._closed:
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
        if self._on_close is not None:
            self._on_close(self)


@runtime_checkable
class EventBus(Protocol):
    """Narrow port both backends satisfy (ISP/DIP)."""

    def subscribe(
        self, channel: str, heartbeat_seconds: Optional[float] = None
    ) -> Subscription:
        ...

    def publish(self, channel: str, event: Dict[str, Any]) -> None:
        ...

    def channel_count(self) -> int:
        ...
