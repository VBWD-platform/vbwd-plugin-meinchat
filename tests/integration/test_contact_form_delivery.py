"""Integration: contact-form → meinchat delivery (S60).

End-to-end against a real Postgres: registering the meinchat event handlers
and publishing a ``contact_form.received`` event provisions a BOT sender +
nickname and delivers a message into a conversation with the recipient. A
failing send must NOT propagate (the contact-form POST stays unaffected).
"""
from __future__ import annotations

import uuid

import pytest

from vbwd.events.bus import EventBus
from vbwd.models.enums import UserRole


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def _payload(sender_email: str, sender_nickname: str, recipients):
    return {
        "widget_slug": "contact",
        "recipient_email": "owner@example.com",
        "fields": [
            {"id": "name", "label": "Name", "value": "Ada Lovelace"},
            {"id": "message", "label": "Message", "value": "Hello from the web"},
        ],
        "remote_ip": "203.0.113.5",
        "source_host": "example.com",
        "meinchat": {
            "enabled": True,
            "sender_email": sender_email,
            "sender_nickname": sender_nickname,
            "recipients": recipients,
        },
    }


@pytest.mark.integration
def test_event_provisions_bot_and_delivers_message(app):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    from plugins.meinchat.meinchat.repositories.conversation_repository import (
        ConversationRepository,
    )
    from plugins.meinchat.meinchat.repositories.message_repository import (
        MessageRepository,
    )
    from plugins.meinchat.meinchat.repositories.nickname_repository import (
        NicknameRepository,
    )
    from plugins.meinchat.meinchat.services.nickname_service import (
        NicknameService,
    )

    with app.app_context():
        user_repo = UserRepository(db.session)
        auth_service = app.container.auth_service()

        # A recipient user with a nickname handle.
        recipient_email = _unique_email("recipient")
        recipient_nick = f"recip{uuid.uuid4().hex[:6]}"
        auth_service.register(email=recipient_email, password="RecipTest123@")
        recipient = user_repo.find_by_email(recipient_email)
        NicknameService(repo=NicknameRepository(db.session)).set_nickname(
            recipient.id, recipient_nick
        )
        db.session.commit()

        sender_email = _unique_email("formbot")
        sender_nick = f"formbot{uuid.uuid4().hex[:6]}"

        # Wire the real handler onto a fresh bus, then publish the event.
        bus = EventBus()
        from plugins.meinchat import MeinchatPlugin

        plugin = MeinchatPlugin()
        plugin.initialize()
        plugin.register_event_handlers(bus)

        bus.publish(
            "contact_form.received",
            _payload(sender_email, sender_nick, [f"@{recipient_nick}"]),
        )
        db.session.commit()

        # Bot user exists as a BOT-role account.
        bot_user = user_repo.find_by_email(sender_email)
        assert bot_user is not None
        assert bot_user.role == UserRole.BOT

        # Bot has the configured nickname.
        bot_nick = NicknameRepository(db.session).find_by_user_id(bot_user.id)
        assert bot_nick is not None
        assert bot_nick.nickname == sender_nick

        # A conversation + message were delivered to the recipient.
        conversation = ConversationRepository(db.session).find_by_pair(
            *sorted([bot_user.id, recipient.id])
        )
        assert conversation is not None
        messages = MessageRepository(db.session).page(conversation.id, limit=10)
        assert len(messages) == 1
        assert messages[0].sender_id == bot_user.id
        assert "Hello from the web" in messages[0].body

        # Second submission reuses the same bot user + conversation.
        bus.publish(
            "contact_form.received",
            _payload(sender_email, sender_nick, [f"@{recipient_nick}"]),
        )
        db.session.commit()
        assert user_repo.find_by_email(sender_email).id == bot_user.id
        messages_after = MessageRepository(db.session).page(conversation.id, limit=10)
        assert len(messages_after) == 2


@pytest.mark.integration
def test_unresolved_recipient_is_skipped_without_error(app):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    from plugins.meinchat import MeinchatPlugin

    with app.app_context():
        user_repo = UserRepository(db.session)
        sender_email = _unique_email("formbot2")
        sender_nick = f"formbot{uuid.uuid4().hex[:6]}"

        bus = EventBus()
        plugin = MeinchatPlugin()
        plugin.initialize()
        plugin.register_event_handlers(bus)

        # Recipient handle resolves to nothing — must not raise.
        bus.publish(
            "contact_form.received",
            _payload(sender_email, sender_nick, ["@nobody_here_xyz"]),
        )
        db.session.commit()

        # Bot is still provisioned (provisioning precedes recipient send).
        assert user_repo.find_by_email(sender_email) is not None
