"""Event-bus backend selection (S38 §3b).

Decided once, explicitly, and loudly — never the pre-S38 silent+sticky fallback
to the single-worker in-process bus on a transient Redis blip.

- ``memory`` — always in-process (dev / single-worker / tests).
- ``redis``  — require Redis; fail loud if unreachable (recommended for prod).
- ``auto``   — prefer Redis; fall back to in-process with a WARNING that names
               the consequence (SSE limited to one worker).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from plugins.meinchat.meinchat.services.event_bus import MeinchatEventBus
from plugins.meinchat.meinchat.services.event_bus_base import EventBus
from plugins.meinchat.meinchat.services.redis_event_bus import RedisEventBus

logger = logging.getLogger(__name__)

VALID_BACKENDS = ("auto", "redis", "memory")


class EventBusUnavailableError(RuntimeError):
    """Raised when backend=redis is required but Redis is unreachable."""


def _redis_reachable(redis_client: Optional[Any]) -> bool:
    if redis_client is None:
        return False
    try:
        redis_client.ping()
        return True
    except Exception:
        logger.debug("meinchat: Redis ping failed", exc_info=True)
        return False


def create_event_bus(
    backend: str,
    channel_prefix: str = "meinchat:",
    redis_client: Optional[Any] = None,
) -> EventBus:
    """Return the configured event bus, logging the choice."""
    backend = (backend or "auto").lower()
    if backend not in VALID_BACKENDS:
        raise ValueError(
            f"event_bus_backend must be one of {VALID_BACKENDS}, got {backend!r}"
        )

    if backend == "memory":
        logger.info("meinchat event bus: in-process (memory) backend selected")
        return MeinchatEventBus()

    if _redis_reachable(redis_client):
        logger.info(
            "meinchat event bus: redis backend selected (channel prefix %r)",
            channel_prefix,
        )
        return RedisEventBus(redis_client, channel_prefix=channel_prefix)

    if backend == "redis":
        raise EventBusUnavailableError(
            "event_bus_backend=redis but Redis is unreachable — refusing to "
            "serve SSE with a single-worker in-process bus in a multi-worker "
            "deployment. Fix Redis, or set event_bus_backend=auto/memory."
        )

    # backend == "auto"
    logger.warning(
        "meinchat event bus: Redis unreachable — falling back to the in-process "
        "backend. SSE real-time delivery is LIMITED TO A SINGLE gunicorn worker. "
        "Set event_bus_backend=redis to fail loud, or run GUNICORN_WORKERS=1."
    )
    return MeinchatEventBus()
