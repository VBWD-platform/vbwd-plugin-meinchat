"""S86.1 Slice 1b — retention + cascade coverage for rooms (D6).

Room messages live in `meinchat_message` (parent = room_id), so the existing
retention prune — which selects rows by `sent_at` alone, with no parent filter
— already covers them. This locks that in. It also asserts that deleting a room
CASCADEs its members + messages.

Data is seeded through the service / repository layer (no raw INSERT), per
feedback_no_direct_db_for_test_data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from vbwd.interfaces.file_storage import InMemoryFileStorage
from vbwd.models.enums import UserRole

from plugins.meinchat.meinchat.models.message import Message
from plugins.meinchat.meinchat.repositories.message_repository import (
    MessageRepository,
)
from plugins.meinchat.meinchat.repositories.room_member_repository import (
    RoomMemberRepository,
)
from plugins.meinchat.meinchat.repositories.room_repository import RoomRepository
from plugins.meinchat.meinchat.services.retention_policy import ConfigRetentionPolicy
from plugins.meinchat.meinchat.services.retention_service import RetentionService
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
        email = f"room-ret-{suffix}-{index}@example.com"
        existing = user_repo.find_by_email(email)
        if existing is None:
            auth_service.register(email=email, password="RoomRet123@")
            existing = user_repo.find_by_email(email)
        ids.append(existing.id)
    db.session.commit()
    return ids


def _room_service(db):
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


def _seed_room_message(db, room_id, sender_id, sent_at):
    msg = Message()
    msg.room_id = room_id
    msg.conversation_id = None
    msg.sender_id = sender_id
    msg.sender_nickname = "tester"
    msg.body = "room line"
    msg.protocol = "plain"
    msg.sent_at = sent_at
    db.session.add(msg)
    db.session.flush()
    return msg


@pytest.mark.integration
def test_retention_prunes_old_room_messages(app):
    from vbwd.extensions import db

    with app.app_context():
        creator, alice = _make_users(app, "prune", 2)
        room = _room_service(db).create_room(creator, [alice], name="prune-room")
        db.session.commit()

        now = datetime.now(tz=UTC)
        old = _seed_room_message(db, room.id, creator, now - timedelta(days=5))
        recent = _seed_room_message(db, room.id, creator, now - timedelta(days=1))
        db.session.commit()
        old_id, recent_id = old.id, recent.id

        service = RetentionService(
            message_repo=MessageRepository(db.session),
            attachment_storage=InMemoryFileStorage(base_url="/uploads"),
            retention_policy=ConfigRetentionPolicy(
                config_provider=lambda: {"messages_retention_days_server": 2}
            ),
            clock=lambda: now,
        )
        result = service.prune_messages()
        db.session.commit()

        assert old_id in result.deleted_ids
        assert recent_id not in result.deleted_ids
        assert db.session.get(Message, old_id) is None
        assert db.session.get(Message, recent_id) is not None


@pytest.mark.integration
def test_deleting_room_cascades_members_and_messages(app):
    from vbwd.extensions import db

    with app.app_context():
        creator, alice = _make_users(app, "cascade", 2)
        room = _room_service(db).create_room(creator, [alice], name="cascade-room")
        db.session.commit()
        room_id = room.id
        _seed_room_message(db, room_id, creator, datetime.now(tz=UTC))
        db.session.commit()

        db.session.delete(db.session.get(type(room), room_id))
        db.session.commit()

        member_count = db.session.execute(
            text("SELECT count(*) FROM meinchat_room_member WHERE room_id = :rid"),
            {"rid": str(room_id)},
        ).scalar()
        message_count = db.session.execute(
            text("SELECT count(*) FROM meinchat_message WHERE room_id = :rid"),
            {"rid": str(room_id)},
        ).scalar()
        assert member_count == 0
        assert message_count == 0


def test_retention_query_has_no_parent_filter():
    """Guard the D6 finding: `find_older_than` selects by `sent_at` alone, so
    room rows are covered without a code change. A future edit that scopes the
    query to `conversation_id` would silently skip room messages — this unit
    test fails loudly if that happens."""
    import inspect

    source = inspect.getsource(MessageRepository.find_older_than)
    assert "conversation_id" not in source
    assert "room_id" not in source
