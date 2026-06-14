"""Unit tests for RoomService (S86.1, D3/D4) — in-memory repo fakes.

The fakes obey the same contract as the real repositories (Liskov): `find`
returns the row or `None`, `save` flushes + returns, `delete` removes. This lets
the membership/role logic be exercised without a database.
"""
from uuid import uuid4

import pytest

from vbwd.models.enums import UserRole

from plugins.meinchat.meinchat.models.room import Room
from plugins.meinchat.meinchat.models.room_member import (
    ROOM_ROLE_ADMIN,
    ROOM_ROLE_MEMBER,
)
from plugins.meinchat.meinchat.services.room_protocol import RoomProtocolSelector
from plugins.meinchat.meinchat.services.room_service import (
    NotARoomMemberError,
    RoomNotFoundError,
    RoomPermissionError,
    RoomService,
)


class FakeRoomRepository:
    def __init__(self):
        self.rooms = {}

    def find_by_id(self, room_id):
        return self.rooms.get(room_id)

    def list_for_user(self, user_id):  # not exercised here
        return list(self.rooms.values())

    def save(self, room):
        if room.id is None:
            room.id = uuid4()
        self.rooms[room.id] = room
        return room


class FakeRoomMemberRepository:
    def __init__(self):
        self.members = []

    def find(self, room_id, user_id):
        for member in self.members:
            if member.room_id == room_id and member.user_id == user_id:
                return member
        return None

    def list_for_room(self, room_id):
        return [m for m in self.members if m.room_id == room_id]

    def save(self, member):
        if member.id is None:
            member.id = uuid4()
        self.members.append(member)
        return member

    def delete(self, member):
        self.members = [m for m in self.members if m is not member]


def _service(roles=None, server_caps=None, has_keys_for=None):
    roles = roles or {}
    server_caps = server_caps if server_caps is not None else {"plain"}
    has_keys_for = has_keys_for or set()
    selector = RoomProtocolSelector(
        server_capabilities_provider=lambda: set(server_caps),
        device_has_keys=lambda user_id: user_id in has_keys_for,
    )
    return RoomService(
        room_repo=FakeRoomRepository(),
        member_repo=FakeRoomMemberRepository(),
        protocol_selector=selector,
        role_resolver=lambda user_id: roles.get(user_id, UserRole.USER),
    )


class TestCreateRoom:
    def test_creator_is_admin_others_member(self):
        creator, alice, bob = uuid4(), uuid4(), uuid4()
        service = _service()

        room = service.create_room(creator, [alice, bob], name="team")

        assert isinstance(room, Room)
        assert service.role_of(room.id, creator) == ROOM_ROLE_ADMIN
        assert service.role_of(room.id, alice) == ROOM_ROLE_MEMBER
        assert service.role_of(room.id, bob) == ROOM_ROLE_MEMBER

    def test_creator_not_added_as_plain_member_row(self):
        creator, alice = uuid4(), uuid4()
        service = _service()

        room = service.create_room(creator, [alice, creator])

        member_ids = [m.user_id for m in service.members(room.id)]
        assert member_ids.count(creator) == 1
        assert service.role_of(room.id, creator) == ROOM_ROLE_ADMIN

    def test_dedupes_duplicate_member_id(self):
        creator, alice = uuid4(), uuid4()
        service = _service()

        room = service.create_room(creator, [alice, alice, alice])

        member_ids = [m.user_id for m in service.members(room.id)]
        assert member_ids.count(alice) == 1

    def test_pins_plain_when_a_bot_is_a_member(self):
        creator, alice, bot = uuid4(), uuid4(), uuid4()
        service = _service(
            roles={bot: UserRole.BOT},
            server_caps={"plain", "e2e_v1"},
            has_keys_for={creator, alice, bot},
        )

        room = service.create_room(creator, [alice, bot], accepted_protocols=["e2e_v1"])

        assert room.protocol == "plain"
        assert room.capabilities == ["plain"]

    def test_pins_plain_when_no_member_has_device_keys(self):
        creator, alice = uuid4(), uuid4()
        service = _service(server_caps={"plain", "e2e_v1"}, has_keys_for=set())

        room = service.create_room(creator, [alice], accepted_protocols=["e2e_v1"])

        assert room.protocol == "plain"


class TestInvite:
    def test_any_member_can_invite(self):
        creator, alice, carol = uuid4(), uuid4(), uuid4()
        service = _service()
        room = service.create_room(creator, [alice])

        # alice is a plain member, not an admin — she may still invite.
        member = service.invite(room.id, alice, carol)

        assert member.role == ROOM_ROLE_MEMBER
        assert member.invited_by_id == alice
        assert service.is_member(room.id, carol)

    def test_non_member_cannot_invite(self):
        creator, alice, stranger, carol = uuid4(), uuid4(), uuid4(), uuid4()
        service = _service()
        room = service.create_room(creator, [alice])

        with pytest.raises(NotARoomMemberError):
            service.invite(room.id, stranger, carol)

    def test_invite_is_idempotent_on_repeat(self):
        creator, alice = uuid4(), uuid4()
        service = _service()
        room = service.create_room(creator, [])

        first = service.invite(room.id, creator, alice)
        second = service.invite(room.id, creator, alice)

        assert first is second
        member_ids = [m.user_id for m in service.members(room.id)]
        assert member_ids.count(alice) == 1


class TestRemoveAndRename:
    def test_admin_can_remove_other_member(self):
        creator, alice = uuid4(), uuid4()
        service = _service()
        room = service.create_room(creator, [alice])

        service.remove_member(room.id, creator, alice)

        assert not service.is_member(room.id, alice)

    def test_non_admin_cannot_remove_other_member(self):
        creator, alice, bob = uuid4(), uuid4(), uuid4()
        service = _service()
        room = service.create_room(creator, [alice, bob])

        with pytest.raises(RoomPermissionError):
            service.remove_member(room.id, alice, bob)

    def test_member_can_remove_self(self):
        creator, alice = uuid4(), uuid4()
        service = _service()
        room = service.create_room(creator, [alice])

        service.remove_member(room.id, alice, alice)

        assert not service.is_member(room.id, alice)

    def test_leave_drops_self(self):
        creator, alice = uuid4(), uuid4()
        service = _service()
        room = service.create_room(creator, [alice])

        service.leave(room.id, alice)

        assert not service.is_member(room.id, alice)

    def test_rename_is_admin_only(self):
        creator, alice = uuid4(), uuid4()
        service = _service()
        room = service.create_room(creator, [alice], name="old")

        with pytest.raises(RoomPermissionError):
            service.rename(room.id, alice, "new")

        renamed = service.rename(room.id, creator, "new")
        assert renamed.name == "new"


class TestGetForMember:
    def test_returns_room_for_member(self):
        creator, alice = uuid4(), uuid4()
        service = _service()
        room = service.create_room(creator, [alice])

        assert service.get_for_member(room.id, alice).id == room.id

    def test_raises_for_non_member(self):
        creator, alice, stranger = uuid4(), uuid4(), uuid4()
        service = _service()
        room = service.create_room(creator, [alice])

        with pytest.raises(NotARoomMemberError):
            service.get_for_member(room.id, stranger)

    def test_unknown_room_raises_not_found(self):
        service = _service()
        with pytest.raises(RoomNotFoundError):
            service.get_for_member(uuid4(), uuid4())


class TestHelpers:
    def test_peers_of_excludes_self(self):
        creator, alice, bob = uuid4(), uuid4(), uuid4()
        service = _service()
        room = service.create_room(creator, [alice, bob])

        peers = service.peers_of(room.id, creator)

        assert set(peers) == {alice, bob}

    def test_role_of_raises_for_non_member(self):
        creator, stranger = uuid4(), uuid4()
        service = _service()
        room = service.create_room(creator, [])

        with pytest.raises(NotARoomMemberError):
            service.role_of(room.id, stranger)
