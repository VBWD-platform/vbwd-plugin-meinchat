"""S28.3a §5 — conversation protocol negotiation contract."""
import uuid

import pytest

from plugins.meinchat.meinchat.extensibility import registry
from plugins.meinchat.meinchat.extensibility.identity import IDeviceDirectory
from plugins.meinchat.meinchat.extensibility.lifecycle import (
    IConversationCapabilities,
    PlainCapability,
)
from plugins.meinchat.meinchat.routes import _NegotiationError, _negotiate_protocol


class _Caps:
    def __init__(self, protocols):
        self._protocols = set(protocols)

    def for_conversation(self, conv):
        return set(self._protocols)


class _Dir:
    def __init__(self, has):
        self._has = has

    def register(self, *a, **k):
        raise NotImplementedError

    def lookup_active(self, user_id):
        return []

    def revoke(self, device_id):
        raise NotImplementedError

    def has_any(self, user_id):
        return self._has


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.reset_for_tests()
    yield
    registry.reset_for_tests()


def test_omitted_accepted_protocols_is_plain_backcompat():
    assert _negotiate_protocol(None, uuid.uuid4()) == ("plain", ["plain"])


def test_plain_negotiates_to_plain():
    registry.register(IConversationCapabilities, PlainCapability())
    assert _negotiate_protocol(["plain"], uuid.uuid4()) == ("plain", ["plain"])


def test_protocol_not_enabled_on_instance_raises_400():
    registry.register(IConversationCapabilities, PlainCapability())
    with pytest.raises(_NegotiationError) as exc:
        _negotiate_protocol(["e2e_v1"], uuid.uuid4())
    assert exc.value.status == 400
    assert exc.value.code == "protocol_not_enabled"


def test_peer_without_device_keys_raises_409():
    registry.register(IConversationCapabilities, _Caps({"plain", "e2e_v1"}))
    registry.register(IDeviceDirectory, _Dir(has=False))
    with pytest.raises(_NegotiationError) as exc:
        _negotiate_protocol(["e2e_v1"], uuid.uuid4())
    assert exc.value.status == 409
    assert exc.value.code == "peer_has_no_device_keys"


def test_e2e_chosen_when_peer_has_device_keys():
    registry.register(IConversationCapabilities, _Caps({"plain", "e2e_v1"}))
    registry.register(IDeviceDirectory, _Dir(has=True))
    assert _negotiate_protocol(["e2e_v1", "plain"], uuid.uuid4()) == (
        "e2e_v1",
        ["e2e_v1"],
    )


def test_empty_accepted_list_raises_400():
    registry.register(IConversationCapabilities, PlainCapability())
    with pytest.raises(_NegotiationError) as exc:
        _negotiate_protocol([], uuid.uuid4())
    assert exc.value.status == 400
