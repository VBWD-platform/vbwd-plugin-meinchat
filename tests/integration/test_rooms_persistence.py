"""S86.1 — rooms persistence against real Postgres.

Exercises the RoomService write path through the real repositories + DB:
- a 3-member room persists with the right roles;
- the UNIQUE(room_id, user_id) constraint rejects a duplicate membership row;
- a meinchat_message with room_id set + conversation_id NULL persists, while a
  both-null and a both-set row are rejected by ck_meinchat_message_one_parent.

Users are created through the auth service (no raw INSERT), per
feedback_no_direct_db_for_test_data.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from vbwd.models.enums import UserRole

from plugins.meinchat.meinchat.models.message import Message
from plugins.meinchat.meinchat.models.room_member import (
    ROOM_ROLE_ADMIN,
    ROOM_ROLE_MEMBER,
    RoomMember,
)
from plugins.meinchat.meinchat.repositories.room_member_repository import (
    RoomMemberRepository,
)
from plugins.meinchat.meinchat.repositories.room_repository import RoomRepository
from plugins.meinchat.meinchat.services.room_protocol import RoomProtocolSelector
from plugins.meinchat.meinchat.services.room_service import RoomService

UTC = timezone.utc


def _make_users(app, suffix, count):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    user_repo = UserRepository(db.session)
    auth_service = app.container.auth_service()
    ids = []
    for index in range(count):
        email = f"rooms-{suffix}-{index}@example.com"
        existing = user_repo.find_by_email(email)
        if existing is None:
            auth_service.register(email=email, password="RoomTest123@")
            existing = user_repo.find_by_email(email)
        ids.append(existing.id)
    db.session.commit()
    return ids


def _build_service():
    from vbwd.extensions import db

    selector = RoomProtocolSelector(
        server_capabilities_provider=lambda: {"plain"},
        device_has_keys=lambda user_id: False,
    )
    return RoomService(
        room_repo=RoomRepository(db.session),
        member_repo=RoomMemberRepository(db.session),
        protocol_selector=selector,
        role_resolver=lambda user_id: UserRole.USER,
    )


@pytest.mark.integration
def test_three_member_room_persists_with_roles(app):
    from vbwd.extensions import db

    creator, alice, bob = _make_users(app, "trio", 3)
    service = _build_service()

    room = service.create_room(creator, [alice, bob], name="trio")
    db.session.commit()

    reloaded = RoomRepository(db.session).find_by_id(room.id)
    assert reloaded is not None
    assert reloaded.protocol == "plain"

    roles = {
        member.user_id: member.role
        for member in RoomMemberRepository(db.session).list_for_room(room.id)
    }
    assert roles[creator] == ROOM_ROLE_ADMIN
    assert roles[alice] == ROOM_ROLE_MEMBER
    assert roles[bob] == ROOM_ROLE_MEMBER
    assert len(roles) == 3


@pytest.mark.integration
def test_duplicate_membership_rejected_by_unique_constraint(app):
    from vbwd.extensions import db

    creator, alice = _make_users(app, "dup", 2)
    service = _build_service()
    room = service.create_room(creator, [alice])
    db.session.commit()

    duplicate = RoomMember()
    duplicate.room_id = room.id
    duplicate.user_id = alice
    duplicate.role = ROOM_ROLE_MEMBER
    duplicate.joined_at = datetime.now(UTC)
    duplicate.unread_count = 0
    db.session.add(duplicate)
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()


@pytest.mark.integration
def test_room_message_persists_with_room_id_only(app):
    from vbwd.extensions import db

    creator, alice = _make_users(app, "msg", 2)
    service = _build_service()
    room = service.create_room(creator, [alice])
    db.session.commit()

    message = Message()
    message.room_id = room.id
    message.conversation_id = None
    message.sender_id = creator
    message.sender_nickname = "creator"
    message.body = "hi room"
    message.protocol = "plain"
    message.sent_at = datetime.now(UTC)
    db.session.add(message)
    db.session.flush()

    assert message.id is not None
    assert message.to_dict()["room_id"] == str(room.id)
    assert message.to_dict()["conversation_id"] is None
    db.session.rollback()


@pytest.mark.integration
def test_message_with_no_parent_rejected_by_check(app):
    from vbwd.extensions import db

    creator = _make_users(app, "noparent", 1)[0]

    message = Message()
    message.room_id = None
    message.conversation_id = None
    message.sender_id = creator
    message.sender_nickname = "creator"
    message.body = "orphan"
    message.protocol = "plain"
    message.sent_at = datetime.now(UTC)
    db.session.add(message)
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()


@pytest.mark.integration
def test_message_with_both_parents_rejected_by_check(app):
    from vbwd.extensions import db

    creator, alice = _make_users(app, "bothparents", 2)
    service = _build_service()
    room = service.create_room(creator, [alice])
    db.session.commit()

    from plugins.meinchat.meinchat.models.conversation import Conversation

    low, high = sorted([creator, alice], key=str)
    conv = Conversation()
    conv.participant_low_id = low
    conv.participant_high_id = high
    db.session.add(conv)
    db.session.flush()

    message = Message()
    message.room_id = room.id
    message.conversation_id = conv.id
    message.sender_id = creator
    message.sender_nickname = "creator"
    message.body = "two parents"
    message.protocol = "plain"
    message.sent_at = datetime.now(UTC)
    db.session.add(message)
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()
