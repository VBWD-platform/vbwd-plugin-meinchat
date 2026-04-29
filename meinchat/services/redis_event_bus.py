"""Redis pub/sub-backed event bus for SSE fan-out across gunicorn workers.

Same public interface as `MeinchatEventBus`. Every `publish(channel, event)`
hits `PUBLISH`, every `subscribe(channel)` opens a dedicated pub/sub
connection that yields events until `close()`. Heartbeats fire from the
subscriber side when the connection stays quiet for `heartbeat_seconds`.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, Optional


class _RedisSubscription:
    def __init__(
        self,
        pubsub,
        channel: str,
        heartbeat_seconds: Optional[float],
    ) -> None:
        self._pubsub = pubsub
        self._channel = channel
        self._heartbeat_seconds = heartbeat_seconds
        self._closed = False

    @property
    def channel(self) -> str:
        return self._channel

    def iter_events(
        self, timeout: Optional[float] = None
    ) -> Iterator[Dict[str, Any]]:
        import time

        hb = self._heartbeat_seconds
        deadline = time.time() + timeout if timeout is not None else None

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

            message = self._pubsub.get_message(
                ignore_subscribe_messages=True, timeout=wait
            )
            if message is None:
                if hb is not None:
                    yield {"type": "heartbeat"}
                continue
            data = message.get("data")
            if data is None:
                continue
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                # Ignore malformed payloads rather than killing the stream.
                continue

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._pubsub.unsubscribe(self._channel)
        finally:
            self._pubsub.close()


class RedisEventBus:
    """Drop-in replacement for MeinchatEventBus backed by Redis pub/sub."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    def subscribe(
        self,
        channel: str,
        heartbeat_seconds: Optional[float] = None,
    ) -> _RedisSubscription:
        pubsub = self._redis.pubsub()
        pubsub.subscribe(channel)
        return _RedisSubscription(pubsub, channel, heartbeat_seconds)

    def publish(self, channel: str, event: Dict[str, Any]) -> None:
        self._redis.publish(channel, json.dumps(event))
