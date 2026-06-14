"""Redis pub/sub-backed event bus for SSE fan-out across gunicorn workers (S38).

Design: ONE listener thread per worker holds ONE Redis pub/sub connection and
`psubscribe`s a single channel pattern; it bridges incoming messages into the
local queue-based `Subscription`s. So the number of Redis connections a worker
holds is independent of how many SSE streams it serves (the pre-S38 design
opened one connection per stream — see s38 §2a).

Channels are namespaced with a prefix so the pattern subscribe cannot catch
foreign channels. Same `EventBus` port as `MeinchatEventBus`; publishers and the
SSE route are unchanged (OCP).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from plugins.meinchat.meinchat.services.event_bus_base import Subscription

logger = logging.getLogger(__name__)

_RECONNECT_BACKOFF_SECONDS = 1.0
_RECONNECT_BACKOFF_MAX_SECONDS = 30.0
# Listener poll granularity — short enough to notice `stop()` promptly.
_LISTEN_POLL_SECONDS = 1.0


class RedisEventBus:
    """Cross-worker bus: one listener connection per worker, local fan-out."""

    def __init__(
        self,
        redis_client: Any,
        channel_prefix: str = "meinchat:",
    ) -> None:
        self._redis = redis_client
        self._prefix = channel_prefix
        self._pattern = f"{channel_prefix}*"
        self._subs: Dict[str, List[Subscription]] = {}
        self._lock = threading.Lock()
        self._listener_thread: Optional[threading.Thread] = None
        self._listener_lock = threading.Lock()
        self._stopped = False
        # Set once the listener's psubscribe is active — lets callers/tests
        # publish only after the worker is actually receiving.
        self._listening = threading.Event()

    # ── EventBus port ───────────────────────────────────────────────────────
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
        """Register ONE subscription under several channels (S86.1 D5). The
        single per-worker listener already psubscribes the whole prefix
        pattern, so no extra Redis connection is opened per added channel."""
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
        self._ensure_listener()
        return sub

    def publish(self, channel: str, event: Dict[str, Any]) -> None:
        self._redis.publish(self._prefix + channel, json.dumps(event))

    def channel_count(self) -> int:
        with self._lock:
            return len(self._subs)

    # ── Lifecycle ───────────────────────────────────────────────────────────
    def wait_listening(self, timeout: Optional[float] = None) -> bool:
        """Block until the listener's psubscribe is active (best-effort)."""
        return self._listening.wait(timeout)

    def stop(self) -> None:
        """Stop the listener thread and drop its connection."""
        self._stopped = True
        thread = self._listener_thread
        if thread is not None:
            thread.join(timeout=_LISTEN_POLL_SECONDS + 1.0)

    # ── Internals ───────────────────────────────────────────────────────────
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

    def _ensure_listener(self) -> None:
        with self._listener_lock:
            if self._listener_thread is not None and self._listener_thread.is_alive():
                return
            self._stopped = False
            thread = threading.Thread(
                target=self._listen,
                name="meinchat-redis-listener",
                daemon=True,
            )
            self._listener_thread = thread
            thread.start()

    def _fanout(self, channel: str, event: Dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs.get(channel, ()))
        for sub in subs:
            sub.deliver(event)

    def _strip_prefix(self, channel: str) -> str:
        if channel.startswith(self._prefix):
            return channel[len(self._prefix) :]
        return channel

    def _listen(self) -> None:
        backoff = _RECONNECT_BACKOFF_SECONDS
        while not self._stopped:
            pubsub = None
            try:
                pubsub = self._redis.pubsub()
                pubsub.psubscribe(self._pattern)
                self._listening.set()
                backoff = _RECONNECT_BACKOFF_SECONDS
                self._consume(pubsub)
            except Exception:
                # The listener must never crash the worker; log and reconnect.
                self._listening.clear()
                if self._stopped:
                    break
                logger.warning(
                    "meinchat redis listener error; reconnecting in %.1fs",
                    backoff,
                    exc_info=True,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX_SECONDS)
            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        logger.debug("error closing meinchat pubsub", exc_info=True)
        self._listening.clear()

    def _consume(self, pubsub: Any) -> None:
        while not self._stopped:
            message = pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=_LISTEN_POLL_SECONDS,
            )
            if message is None:
                continue
            if message.get("type") not in ("message", "pmessage"):
                continue
            channel = message.get("channel")
            if isinstance(channel, bytes):
                channel = channel.decode("utf-8")
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            if not isinstance(data, str):
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                # Ignore malformed payloads rather than killing the listener.
                continue
            self._fanout(self._strip_prefix(channel), event)
