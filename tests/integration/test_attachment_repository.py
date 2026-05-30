"""S28.4 — meinchat_attachment child table: repo + schema constraints.

DB-backed (real PG): exercises the protocol/envelope CHECK, the
one-kind-per-message UNIQUE, and the kind CHECK. Data through the repo/ORM
(no raw INSERT), per feedback_no_direct_db_for_test_data.
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from plugins.meinchat.meinchat.models.conversation import Conversation
from plugins.meinchat.meinchat.models.message import Message
from plugins.meinchat.meinchat.repositories.attachment_repository import (
    AttachmentRepository,
)

UTC = timezone.utc


def _two_users(app, suffix):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    user_repo = UserRepository(db.session)
    auth_service = app.container.auth_service()
    ids = []
    for label in ("a", "b"):
        email = f"attach-{suffix}-{label}@example.com"
        existing = user_repo.find_by_email(email)
        if existing is None:
            auth_service.register(email=email, password="AttTest123@")
            existing = user_repo.find_by_email(email)
        ids.append(existing.id)
    db.session.commit()
    return ids


def _e2e_message(app, suffix):
    from vbwd.extensions import db

    a, b = _two_users(app, suffix)
    low, high = sorted([a, b], key=str)
    conv = Conversation(participant_low_id=low, participant_high_id=high)
    db.session.add(conv)
    db.session.flush()
    msg = Message(
        conversation_id=conv.id,
        sender_id=a,
        sender_nickname="tester",
        protocol="e2e_v1",
        envelope=b"opaque",
        sent_at=datetime.now(UTC),
    )
    db.session.add(msg)
    db.session.flush()
    return msg


def test_add_and_list_e2e_attachments(app):
    from vbwd.extensions import db

    with app.app_context():
        msg = _e2e_message(app, uuid4().hex[:8])
        repo = AttachmentRepository(db.session)
        repo.add(
            message_id=msg.id,
            kind="fullres",
            storage_url="/u/full.enc",
            protocol="e2e_v1",
            envelope_header={"per_recipient_key_envelopes": {}},
            mime="image/webp",
            bytes_count=1234,
        )
        repo.add(
            message_id=msg.id,
            kind="thumb",
            storage_url="/u/thumb.enc",
            protocol="e2e_v1",
            envelope_header={"per_recipient_key_envelopes": {}},
            mime="image/webp",
            bytes_count=99,
        )
        db.session.commit()
        rows = repo.list_by_message(msg.id)
        assert {r.kind for r in rows} == {"fullres", "thumb"}
        assert set(repo.storage_urls_for_message(msg.id)) == {
            "/u/full.enc",
            "/u/thumb.enc",
        }


def test_one_kind_per_message_unique(app):
    from vbwd.extensions import db

    with app.app_context():
        msg = _e2e_message(app, uuid4().hex[:8])
        repo = AttachmentRepository(db.session)
        repo.add(
            message_id=msg.id,
            kind="fullres",
            storage_url="/u/a.enc",
            protocol="e2e_v1",
            envelope_header={},
            mime="image/webp",
            bytes_count=1,
        )
        db.session.flush()
        with pytest.raises(IntegrityError):
            repo.add(
                message_id=msg.id,
                kind="fullres",
                storage_url="/u/b.enc",
                protocol="e2e_v1",
                envelope_header={},
                mime="image/webp",
                bytes_count=2,
            )
        db.session.rollback()


def test_protocol_envelope_check_constraint(app):
    from vbwd.extensions import db

    with app.app_context():
        msg = _e2e_message(app, uuid4().hex[:8])
        repo = AttachmentRepository(db.session)
        # e2e_v1 without an envelope_header violates the CHECK.
        with pytest.raises(IntegrityError):
            repo.add(
                message_id=msg.id,
                kind="fullres",
                storage_url="/u/a.enc",
                protocol="e2e_v1",
                envelope_header=None,
                mime="image/webp",
                bytes_count=1,
            )
        db.session.rollback()


def test_plain_attachment_must_not_carry_envelope(app):
    from vbwd.extensions import db

    with app.app_context():
        msg = _e2e_message(app, uuid4().hex[:8])
        repo = AttachmentRepository(db.session)
        with pytest.raises(IntegrityError):
            repo.add(
                message_id=msg.id,
                kind="fullres",
                storage_url="/u/a.enc",
                protocol="plain",
                envelope_header={"x": 1},
                mime="image/webp",
                bytes_count=1,
            )
        db.session.rollback()
