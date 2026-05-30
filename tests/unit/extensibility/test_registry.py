"""S28.3a §6.1 — port registry resolver specs."""
import pytest

from plugins.meinchat.meinchat.extensibility import registry
from plugins.meinchat.meinchat.extensibility.identity import (
    IDeviceDirectory,
    NullDeviceDirectory,
)
from plugins.meinchat.meinchat.extensibility.pipeline import (
    IBodyCodec,
    IdentityBodyCodec,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.reset_for_tests()
    yield
    registry.reset_for_tests()


class _OtherCodec:
    def encode(self, ctx):
        return None

    def decode(self, row, viewer_device=None):
        return ""


def test_resolve_first_returns_registered_default():
    default = IdentityBodyCodec()
    registry.register(IBodyCodec, default)
    assert registry.resolve_first(IBodyCodec) is default


def test_resolve_first_last_write_wins():
    first = IdentityBodyCodec()
    second = _OtherCodec()
    registry.register(IBodyCodec, first)
    registry.register(IBodyCodec, second)
    assert registry.resolve_first(IBodyCodec) is second


def test_resolve_first_default_plus_override_returns_override():
    default = IdentityBodyCodec()
    override = _OtherCodec()
    registry.register(IBodyCodec, default)
    registry.register(IBodyCodec, override)
    assert registry.resolve_first(IBodyCodec) is override


def test_resolve_first_empty_raises():
    with pytest.raises(LookupError):
        registry.resolve_first(IBodyCodec)


def test_resolve_all_empty_returns_empty_list():
    assert registry.resolve_all(IBodyCodec) == []


def test_resolve_all_preserves_registration_order():
    a, b = IdentityBodyCodec(), _OtherCodec()
    registry.register(IBodyCodec, a)
    registry.register(IBodyCodec, b)
    assert registry.resolve_all(IBodyCodec) == [a, b]


def test_reset_for_tests_clears_one_port_only():
    registry.register(IBodyCodec, IdentityBodyCodec())
    registry.register(IDeviceDirectory, NullDeviceDirectory())
    registry.reset_for_tests(IBodyCodec)
    assert registry.resolve_all(IBodyCodec) == []
    assert len(registry.resolve_all(IDeviceDirectory)) == 1


def test_reset_for_tests_clears_all_ports():
    registry.register(IBodyCodec, IdentityBodyCodec())
    registry.register(IDeviceDirectory, NullDeviceDirectory())
    registry.reset_for_tests()
    assert registry.resolve_all(IBodyCodec) == []
    assert registry.resolve_all(IDeviceDirectory) == []


def test_re_registering_same_instance_is_idempotent():
    codec = IdentityBodyCodec()
    registry.register(IBodyCodec, codec)
    registry.register(IBodyCodec, codec)
    assert registry.resolve_all(IBodyCodec) == [codec]


def test_register_wrong_type_raises():
    with pytest.raises(TypeError):
        registry.register(IBodyCodec, NullDeviceDirectory())


def test_unregister_restores_previous_default():
    default = IdentityBodyCodec()
    override = _OtherCodec()
    registry.register(IBodyCodec, default)
    registry.register(IBodyCodec, override)
    registry.unregister(IBodyCodec, override)
    assert registry.resolve_first(IBodyCodec) is default


def test_registry_isolation_between_tests():
    # If a prior test leaked an impl, this would be non-empty.
    assert registry.resolve_all(IBodyCodec) == []
