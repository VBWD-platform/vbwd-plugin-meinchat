"""S28.1 §3.2 — integration specs for the server retention prune against
real Postgres.

1. End-to-end: seed -5d / -1d messages, prune with days=2, assert -5d gone
   and -1d remains (verified via raw SELECT count).
2. Cascade-safety: pruning a message does NOT cascade-delete its parent
   `conversation` row.
3. Attachment cleanup: a row with `attachment_url` whose object is written
   to a temp-dir-backed LocalFileStorage — after prune both file + row gone.

Data is seeded through the repository / service layer (no raw INSERT), per
feedback_no_direct_db_for_test_data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from vbwd.interfaces.file_storage import LocalFileStorage
from plugins.meinchat.meinchat.models.conversation import Conversation
from plugins.meinchat.meinchat.models.message import Message
from plugins.meinchat.meinchat.repositories.message_repository import (
    MessageRepository,
)
from plugins.meinchat.meinchat.services.retention_policy import (
    ConfigRetentionPolicy,
)
from plugins.meinchat.meinchat.services.retention_service import RetentionService


UTC = timezone.utc


def _ordered_pair(user_a_id, user_b_id):
    low, high = sorted([user_a_id, user_b_id], key=str)
    return low, high


def _make_two_users(app, suffix):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    user_repo = UserRepository(db.session)
    auth_service = app.container.auth_service()
    ids = {}
    for label in ("a", "b"):
        email = f"retention-{suffix}-{label}@example.com"
        existing = user_repo.find_by_email(email)
        if existing is None:
            auth_service.register(email=email, password="RetTest123@")
            existing = user_repo.find_by_email(email)
        ids[label] = existing.id
    db.session.commit()
    return ids["a"], ids["b"]


def _seed_conversation(db, user_a_id, user_b_id):
    low, high = _ordered_pair(user_a_id, user_b_id)
    conv = Conversation()
    conv.participant_low_id = low
    conv.participant_high_id = high
    db.session.add(conv)
    db.session.flush()
    return conv


def _seed_message(repo, *, conv, sender_id, sent_at, attachment_url=None):
    msg = Message()
    msg.conversation_id = conv.id
    msg.sender_id = sender_id
    msg.sender_nickname = "tester"
    msg.body = "hello"
    msg.sent_at = sent_at
    msg.attachment_url = attachment_url
    return repo.save(msg)


def _service(db, *, days, storage):
    return RetentionService(
        message_repo=MessageRepository(db.session),
        attachment_storage=storage,
        retention_policy=ConfigRetentionPolicy(
            config_provider=lambda: {"messages_retention_days_server": days}
        ),
        clock=lambda: datetime.now(tz=UTC),
    )


@pytest.mark.integration
def test_prune_keeps_recent_removes_old(app):
    from vbwd.extensions import db
    from vbwd.interfaces.file_storage import InMemoryFileStorage

    with app.app_context():
        user_a_id, user_b_id = _make_two_users(app, "e2e")
        conv = _seed_conversation(db, user_a_id, user_b_id)
        repo = MessageRepository(db.session)
        now = datetime.now(tz=UTC)
        old = _seed_message(
            repo, conv=conv, sender_id=user_a_id, sent_at=now - timedelta(days=5)
        )
        recent = _seed_message(
            repo, conv=conv, sender_id=user_a_id, sent_at=now - timedelta(days=1)
        )
        db.session.commit()
        old_id, recent_id, conv_id = old.id, recent.id, conv.id

        result = _service(
            db, days=2, storage=InMemoryFileStorage(base_url="/uploads")
        ).prune_messages()
        db.session.commit()

        assert old_id in result.deleted_ids
        assert recent_id not in result.deleted_ids

        remaining = (
            db.session.query(Message).filter(Message.conversation_id == conv_id).count()
        )
        assert remaining == 1
        assert db.session.get(Message, recent_id) is not None
        assert db.session.get(Message, old_id) is None


@pytest.mark.integration
def test_pruning_message_does_not_delete_conversation(app):
    from vbwd.extensions import db
    from vbwd.interfaces.file_storage import InMemoryFileStorage

    with app.app_context():
        user_a_id, user_b_id = _make_two_users(app, "cascade")
        conv = _seed_conversation(db, user_a_id, user_b_id)
        repo = MessageRepository(db.session)
        now = datetime.now(tz=UTC)
        _seed_message(
            repo, conv=conv, sender_id=user_a_id, sent_at=now - timedelta(days=10)
        )
        db.session.commit()
        conv_id = conv.id

        _service(
            db, days=2, storage=InMemoryFileStorage(base_url="/uploads")
        ).prune_messages()
        db.session.commit()

        surviving = db.session.execute(
            text("SELECT count(*) FROM conversation WHERE id = :cid"),
            {"cid": str(conv_id)},
        ).scalar()
        assert surviving == 1


@pytest.mark.integration
def test_attachment_cleanup_removes_file_and_row(app, tmp_path):
    from vbwd.extensions import db

    with app.app_context():
        storage = LocalFileStorage(base_path=str(tmp_path), base_url="/uploads")
        rel_path = f"meinchat/attachments/{uuid4().hex}/photo.webp"
        storage.save(b"image-bytes", rel_path)
        assert storage.exists(rel_path)

        user_a_id, user_b_id = _make_two_users(app, "attach")
        conv = _seed_conversation(db, user_a_id, user_b_id)
        repo = MessageRepository(db.session)
        now = datetime.now(tz=UTC)
        row = _seed_message(
            repo,
            conv=conv,
            sender_id=user_a_id,
            sent_at=now - timedelta(days=7),
            attachment_url=f"/uploads/{rel_path}",
        )
        db.session.commit()
        row_id = row.id

        service = _service(db, days=2, storage=storage)
        attach_result = service.prune_attachments()
        service.prune_messages()
        db.session.commit()

        assert attach_result.errors == 0
        assert not storage.exists(rel_path)
        assert db.session.get(Message, row_id) is None
