"""S86.1 Slice 1b — MessageService room path (D2), in-memory repo fakes.

The room send/list/read path mirrors the 1:1 conversation path (same
`meinchat_message` rows, parent = `room_id`) with no duplicated
attachment/meta logic. The fakes obey the same repo contracts as the
production repositories (Liskov).
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from plugins.meinchat.meinchat.models.room import Room
from plugins.meinchat.meinchat.models.room_member import (
    ROOM_ROLE_ADMIN,
    ROOM_ROLE_MEMBER,
    RoomMember,
)
from plugins.meinchat.meinchat.services.message_service import (
    MessageBodyTooLongError,
    MessageService,
    NotARoomMemberError,
    RoomNotFoundError,
)

UTC = timezone.utc


class FakeRoomRepository:
    def __init__(self):
        self.rooms = {}

    def find_by_id(self, room_id):
        return self.rooms.get(room_id)

    def add(self, room):
        self.rooms[room.id] = room
        return room

    def save(self, room):
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

    def add(self, member):
        self.members.append(member)
        return member

    def save(self, member):
        if member not in self.members:
            self.members.append(member)
        return member


class FakeMessageRepository:
    def __init__(self):
        self.saved = []

    def save(self, row):
        if row.id is None:
            row.id = uuid4()
        self.saved.append(row)
        return row

    def page_room(self, room_id, *, before=None, limit=50):
        rows = [r for r in self.saved if getattr(r, "room_id", None) == room_id]
        return list(reversed(rows))[:limit]


class FakeEventBus:
    def __init__(self):
        self.published = []

    def publish(self, channel, event):
        self.published.append((channel, event))


def _room(creator_id):
    room = Room()
    room.id = uuid4()
    room.created_by_id = creator_id
    room.protocol = "plain"
    room.capabilities = ["plain"]
    return room


def _member(room_id, user_id, role):
    member = RoomMember()
    member.id = uuid4()
    member.room_id = room_id
    member.user_id = user_id
    member.role = role
    member.joined_at = datetime.now(UTC)
    member.unread_count = 0
    return member


def _nickname_row(user_id, nickname):
    from plugins.meinchat.meinchat.models.user_nickname import UserNickname

    row = UserNickname()
    row.user_id = user_id
    row.nickname = nickname
    return row


def _service(room_repo, member_repo, message_repo, nickname_repo, event_bus):
    from unittest.mock import MagicMock

    conv_repo = MagicMock()
    conv_repo.save.side_effect = lambda row: row
    return MessageService(
        conv_repo=conv_repo,
        message_repo=message_repo,
        nickname_repo=nickname_repo,
        event_bus=event_bus,
        room_repo=room_repo,
        member_repo=member_repo,
    )


def _seed_room(creator, members):
    from unittest.mock import MagicMock

    room_repo = FakeRoomRepository()
    member_repo = FakeRoomMemberRepository()
    message_repo = FakeMessageRepository()
    nickname_repo = MagicMock()
    nickname_repo.find_by_user_id.side_effect = lambda uid: _nickname_row(uid, "nick")
    event_bus = FakeEventBus()

    room = _room(creator)
    room_repo.add(room)
    member_repo.add(_member(room.id, creator, ROOM_ROLE_ADMIN))
    for member_id in members:
        member_repo.add(_member(room.id, member_id, ROOM_ROLE_MEMBER))

    service = _service(room_repo, member_repo, message_repo, nickname_repo, event_bus)
    return service, room, member_repo, message_repo, event_bus


class TestSendRoomText:
    def test_stores_message_with_room_id(self):
        creator, alice = uuid4(), uuid4()
        service, room, _members, message_repo, _bus = _seed_room(creator, [alice])

        msg = service.send_room_text(room.id, sender_user_id=creator, body="hi room")

        assert msg.room_id == room.id
        assert msg.conversation_id is None
        assert msg.body == "hi room"
        assert message_repo.saved == [msg]

    def test_increments_unread_for_every_other_member_only(self):
        creator, alice, bob = uuid4(), uuid4(), uuid4()
        service, room, member_repo, _msgs, _bus = _seed_room(creator, [alice, bob])

        service.send_room_text(room.id, sender_user_id=creator, body="hi")

        unread = {
            member.user_id: member.unread_count
            for member in member_repo.list_for_room(room.id)
        }
        assert unread[creator] == 0
        assert unread[alice] == 1
        assert unread[bob] == 1

    def test_updates_room_last_message_fields(self):
        creator, alice = uuid4(), uuid4()
        service, room, _members, _msgs, _bus = _seed_room(creator, [alice])

        service.send_room_text(room.id, sender_user_id=creator, body="latest")

        assert room.last_message_at is not None
        assert room.last_message_preview == "latest"

    def test_publishes_on_room_channel(self):
        creator, alice = uuid4(), uuid4()
        service, room, _members, _msgs, bus = _seed_room(creator, [alice])

        service.send_room_text(room.id, sender_user_id=creator, body="hi")

        channels = [channel for channel, _event in bus.published]
        assert f"room:{room.id}" in channels
        assert all(channel == f"room:{room.id}" for channel in channels)

    def test_rejects_non_member(self):
        creator, alice, stranger = uuid4(), uuid4(), uuid4()
        service, room, _members, message_repo, _bus = _seed_room(creator, [alice])

        with pytest.raises(NotARoomMemberError):
            service.send_room_text(room.id, sender_user_id=stranger, body="sneaky")
        assert message_repo.saved == []

    def test_unknown_room_raises_not_found(self):
        creator = uuid4()
        service, _room, _members, _msgs, _bus = _seed_room(creator, [])

        with pytest.raises(RoomNotFoundError):
            service.send_room_text(uuid4(), sender_user_id=creator, body="hi")

    def test_rejects_body_over_cap(self):
        creator, alice = uuid4(), uuid4()
        service, room, _members, _msgs, _bus = _seed_room(creator, [alice])

        with pytest.raises(MessageBodyTooLongError):
            service.send_room_text(room.id, sender_user_id=creator, body="x" * 4001)


class TestListRoomMessages:
    def test_membership_gated(self):
        creator, alice, stranger = uuid4(), uuid4(), uuid4()
        service, room, _members, _msgs, _bus = _seed_room(creator, [alice])

        with pytest.raises(NotARoomMemberError):
            service.list_room_messages(room.id, caller_user_id=stranger)

    def test_returns_room_messages(self):
        creator, alice = uuid4(), uuid4()
        service, room, _members, _msgs, _bus = _seed_room(creator, [alice])
        service.send_room_text(room.id, sender_user_id=creator, body="one")

        rows = service.list_room_messages(room.id, caller_user_id=alice)

        assert [r.body for r in rows] == ["one"]


class TestRoomSendRunsPostSendHooks:
    """S86.3 D6 — a room send must fire the SAME ``IPostSendHook.on_sent`` chain
    the 1:1 send fires, so the bot inbound bridge is invoked for room messages.
    """

    def test_send_room_text_invokes_registered_post_send_hooks(self):
        from plugins.meinchat.meinchat.extensibility import registry
        from plugins.meinchat.meinchat.extensibility.pipeline import IPostSendHook

        class _RecordingHook:
            def __init__(self):
                self.rows = []

            def on_sent(self, row, *, fetched_by=None):
                self.rows.append(row)

        creator, alice = uuid4(), uuid4()
        service, room, _members, _msgs, _bus = _seed_room(creator, [alice])
        hook = _RecordingHook()
        registry.register(IPostSendHook, hook)
        try:
            msg = service.send_room_text(room.id, sender_user_id=creator, body="hi")
        finally:
            registry.unregister(IPostSendHook, hook)

        assert hook.rows == [msg]

    def test_a_throwing_room_hook_never_fails_the_send(self):
        from plugins.meinchat.meinchat.extensibility import registry
        from plugins.meinchat.meinchat.extensibility.pipeline import IPostSendHook

        class _BoomHook:
            def on_sent(self, row, *, fetched_by=None):
                raise RuntimeError("boom")

        creator, alice = uuid4(), uuid4()
        service, room, _members, message_repo, _bus = _seed_room(creator, [alice])
        hook = _BoomHook()
        registry.register(IPostSendHook, hook)
        try:
            msg = service.send_room_text(room.id, sender_user_id=creator, body="hi")
        finally:
            registry.unregister(IPostSendHook, hook)

        assert message_repo.saved == [msg]


class TestMarkRoomRead:
    def test_clears_caller_unread_only(self):
        creator, alice = uuid4(), uuid4()
        service, room, member_repo, _msgs, _bus = _seed_room(creator, [alice])
        service.send_room_text(room.id, sender_user_id=creator, body="hi")

        service.mark_room_read(room.id, reader_user_id=alice)

        unread = {
            member.user_id: member.unread_count
            for member in member_repo.list_for_room(room.id)
        }
        assert unread[alice] == 0
        alice_member = member_repo.find(room.id, alice)
        assert alice_member.last_read_at is not None

    def test_membership_gated(self):
        creator, alice, stranger = uuid4(), uuid4(), uuid4()
        service, room, _members, _msgs, _bus = _seed_room(creator, [alice])

        with pytest.raises(NotARoomMemberError):
            service.mark_room_read(room.id, reader_user_id=stranger)
