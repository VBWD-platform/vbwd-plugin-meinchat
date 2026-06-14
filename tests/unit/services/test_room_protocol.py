"""Unit tests for RoomProtocolSelector (S86.1, D4)."""
from uuid import uuid4

from vbwd.models.enums import UserRole

from plugins.meinchat.meinchat.services.room_protocol import RoomProtocolSelector


def _selector(server_caps, has_keys_for=None):
    has_keys_for = has_keys_for or set()
    return RoomProtocolSelector(
        server_capabilities_provider=lambda: set(server_caps),
        device_has_keys=lambda user_id: user_id in has_keys_for,
    )


def test_bot_member_forces_plain_even_when_all_have_keys():
    alice, bob, bot = uuid4(), uuid4(), uuid4()
    selector = _selector({"plain", "e2e_v1"}, has_keys_for={alice, bob, bot})
    roles = {alice: UserRole.USER, bob: UserRole.USER, bot: UserRole.BOT}

    protocol, caps = selector.select([alice, bob, bot], roles, ["e2e_v1", "plain"])

    assert protocol == "plain"
    assert caps == ["plain"]


def test_falls_back_to_plain_when_no_member_has_device_keys():
    alice, bob = uuid4(), uuid4()
    # e2e enabled on the instance, but the (Null) directory grants no keys.
    selector = _selector({"plain", "e2e_v1"}, has_keys_for=set())
    roles = {alice: UserRole.USER, bob: UserRole.USER}

    protocol, caps = selector.select([alice, bob], roles, ["e2e_v1", "plain"])

    assert protocol == "plain"
    assert caps == ["plain"]


def test_falls_back_to_plain_when_only_some_members_have_keys():
    alice, bob = uuid4(), uuid4()
    selector = _selector({"plain", "e2e_v1"}, has_keys_for={alice})
    roles = {alice: UserRole.USER, bob: UserRole.USER}

    protocol, _ = selector.select([alice, bob], roles, ["e2e_v1", "plain"])

    assert protocol == "plain"


def test_selects_e2e_when_all_members_have_keys_and_no_bot():
    """Proves the S86.2 hook: with a directory that grants keys to everyone the
    same selector resolves e2e_v1 — no change needed here for S86.2."""
    alice, bob = uuid4(), uuid4()
    selector = _selector({"plain", "e2e_v1"}, has_keys_for={alice, bob})
    roles = {alice: UserRole.USER, bob: UserRole.USER}

    protocol, caps = selector.select([alice, bob], roles, ["e2e_v1", "plain"])

    assert protocol == "e2e_v1"
    assert caps == ["e2e_v1"]


def test_plain_when_server_only_enables_plain():
    alice, bob = uuid4(), uuid4()
    selector = _selector({"plain"}, has_keys_for={alice, bob})
    roles = {alice: UserRole.USER, bob: UserRole.USER}

    protocol, _ = selector.select([alice, bob], roles, None)

    assert protocol == "plain"
