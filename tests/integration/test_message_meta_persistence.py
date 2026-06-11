"""S70.0 — `meta` persists end-to-end through MessageService against real PG,
and e2e (`envelope`) rows / plain rows without `meta` are unaffected.

Data is seeded through the service / ORM (no raw INSERT), per
feedback_no_direct_db_for_test_data.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from plugins.meinchat.meinchat.models.conversation import Conversation
from plugins.meinchat.meinchat.models.message import Message
from plugins.meinchat.meinchat.repositories.conversation_repository import (
    ConversationRepository,
)
from plugins.meinchat.meinchat.repositories.message_repository import (
    MessageRepository,
)
from plugins.meinchat.meinchat.repositories.nickname_repository import (
    NicknameRepository,
)
from plugins.meinchat.meinchat.services.message_service import MessageService

UTC = timezone.utc


def _two_users(app, suffix):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    user_repo = UserRepository(db.session)
    auth_service = app.container.auth_service()
    ids = []
    for label in ("a", "b"):
        email = f"meta-{suffix}-{label}@example.com"
        existing = user_repo.find_by_email(email)
        if existing is None:
            auth_service.register(email=email, password="MetaTest123@")
            existing = user_repo.find_by_email(email)
        ids.append(existing.id)
    db.session.commit()
    return ids


def _seed_conversation(db, user_a_id, user_b_id, protocol="plain"):
    low, high = sorted([user_a_id, user_b_id], key=str)
    conv = Conversation()
    conv.participant_low_id = low
    conv.participant_high_id = high
    conv.protocol = protocol
    db.session.add(conv)
    db.session.flush()
    return conv


def _seed_nickname(db, user_id, nickname):
    from plugins.meinchat.meinchat.models.user_nickname import UserNickname

    row = UserNickname()
    row.user_id = user_id
    row.nickname = nickname
    db.session.add(row)
    db.session.flush()


def _service(db):
    return MessageService(
        conv_repo=ConversationRepository(db.session),
        message_repo=MessageRepository(db.session),
        nickname_repo=NicknameRepository(db.session),
    )


@pytest.mark.integration
def test_bot_choices_meta_persists_and_reloads(app):
    from vbwd.extensions import db

    with app.app_context():
        user_a_id, user_b_id = _two_users(app, "choices")
        conv = _seed_conversation(db, user_a_id, user_b_id)
        _seed_nickname(db, user_a_id, "alice-choices")
        meta = {
            "kind": "bot_choices",
            "choices": [
                {"label": "Pro", "action_data": "subscription:plan:42", "hint": "€29"},
                {"label": "Cancel", "action_data": "bot-base:cancel:0"},
            ],
        }
        saved = _service(db).send_text(
            conv.id, sender_user_id=user_a_id, body="Pick one", meta=meta
        )
        db.session.commit()

        reloaded = MessageRepository(db.session).find_by_id(saved.id)
        assert reloaded.meta == meta
        assert reloaded.to_dict()["meta"] == meta


@pytest.mark.integration
def test_plain_message_without_meta_serializes_meta_none(app):
    from vbwd.extensions import db

    with app.app_context():
        user_a_id, user_b_id = _two_users(app, "plain")
        conv = _seed_conversation(db, user_a_id, user_b_id)
        _seed_nickname(db, user_a_id, "alice-plain")
        saved = _service(db).send_text(conv.id, sender_user_id=user_a_id, body="hi")
        db.session.commit()

        reloaded = MessageRepository(db.session).find_by_id(saved.id)
        assert reloaded.meta is None
        assert reloaded.to_dict()["meta"] is None


@pytest.mark.integration
def test_e2e_envelope_row_is_unaffected_meta_none(app):
    """A non-plain (envelope) row never carries rich choices — `meta` is NULL
    and `to_dict()` still emits the envelope unchanged."""
    from vbwd.extensions import db

    with app.app_context():
        user_a_id, user_b_id = _two_users(app, "e2e")
        conv = _seed_conversation(db, user_a_id, user_b_id, protocol="e2e_v1")
        msg = Message(
            conversation_id=conv.id,
            sender_id=user_a_id,
            sender_nickname="tester",
            protocol="e2e_v1",
            envelope=b"opaque-ciphertext",
            sent_at=datetime.now(UTC),
        )
        db.session.add(msg)
        db.session.commit()

        reloaded = MessageRepository(db.session).find_by_id(msg.id)
        serialized = reloaded.to_dict()
        assert reloaded.meta is None
        assert serialized["meta"] is None
        assert "envelope" in serialized  # envelope path unchanged
