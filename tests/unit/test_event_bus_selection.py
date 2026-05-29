"""S38 — event-bus backend selection (create_event_bus).

Pure unit: no real Redis. Proves the fix for the silent+sticky fallback —
`redis` mode fails loud, `auto` degrades with a warning, choices are explicit.
"""
import pytest

from plugins.meinchat.meinchat.services.event_bus import MeinchatEventBus
from plugins.meinchat.meinchat.services.event_bus_factory import (
    EventBusUnavailableError,
    create_event_bus,
)
from plugins.meinchat.meinchat.services.redis_event_bus import RedisEventBus


class _FakeRedis:
    """Minimal client; only `ping` matters for selection."""

    def __init__(self, reachable: bool = True) -> None:
        self._reachable = reachable

    def ping(self) -> bool:
        if not self._reachable:
            raise ConnectionError("redis down")
        return True


class TestBackendSelection:
    def test_memory_backend_is_in_process(self):
        bus = create_event_bus("memory", redis_client=None)
        assert isinstance(bus, MeinchatEventBus)

    def test_auto_uses_redis_when_reachable(self):
        bus = create_event_bus("auto", redis_client=_FakeRedis(reachable=True))
        assert isinstance(bus, RedisEventBus)

    def test_auto_falls_back_to_memory_when_redis_down(self, caplog):
        with caplog.at_level("WARNING"):
            bus = create_event_bus("auto", redis_client=_FakeRedis(reachable=False))
        assert isinstance(bus, MeinchatEventBus)
        assert any("single" in r.message.lower() for r in caplog.records)

    def test_auto_falls_back_to_memory_when_no_client(self):
        bus = create_event_bus("auto", redis_client=None)
        assert isinstance(bus, MeinchatEventBus)

    def test_redis_backend_fails_loud_when_unreachable(self):
        with pytest.raises(EventBusUnavailableError):
            create_event_bus("redis", redis_client=_FakeRedis(reachable=False))

    def test_redis_backend_fails_loud_when_no_client(self):
        with pytest.raises(EventBusUnavailableError):
            create_event_bus("redis", redis_client=None)

    def test_redis_backend_uses_redis_when_reachable(self):
        bus = create_event_bus("redis", redis_client=_FakeRedis(reachable=True))
        assert isinstance(bus, RedisEventBus)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError):
            create_event_bus("postgres", redis_client=None)

    def test_channel_prefix_is_passed_through(self):
        bus = create_event_bus(
            "redis", channel_prefix="mc_x:", redis_client=_FakeRedis()
        )
        assert isinstance(bus, RedisEventBus)
        assert bus._prefix == "mc_x:"
