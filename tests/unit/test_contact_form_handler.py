"""Unit tests for the meinchat ContactFormHandler (S60).

On a ``contact_form.received`` event whose payload has an enabled meinchat
block, the handler provisions the bot sender, resolves the recipient
handles, and posts one message per distinct recipient as the bot. It is
best-effort: any failure logs a warning and NEVER raises, and a disabled /
absent meinchat block is a no-op.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from vbwd.models.enums import UserRole

from plugins.meinchat.meinchat.handlers.contact_form_handler import (
    ContactFormHandler,
)


def _conv_for(user_a, user_b):
    return SimpleNamespace(
        id=uuid4(), participant_low_id=user_a, participant_high_id=user_b
    )


def _make_handler(admin_users=None, nickname_lookup=None):
    bot_id = uuid4()

    provisioner = MagicMock()
    provisioner.ensure_bot_sender.return_value = bot_id

    user_repository = MagicMock()
    user_repository.find_by_status.return_value = []
    # admin-role resolution helper used by the handler.
    admin_users = admin_users if admin_users is not None else []
    user_repository.find_by_role = MagicMock(return_value=admin_users)

    nickname_repository = MagicMock()
    nickname_lookup = nickname_lookup or {}

    def _find_by_nickname_ci(handle):
        return nickname_lookup.get(handle.lower())

    nickname_repository.find_by_nickname_ci.side_effect = _find_by_nickname_ci

    conversation_service = MagicMock()
    conversation_service.start_or_get.side_effect = lambda a, b: _conv_for(a, b)

    message_service = MagicMock()

    handler = ContactFormHandler(
        provisioner=provisioner,
        user_repository=user_repository,
        nickname_repository=nickname_repository,
        conversation_service=conversation_service,
        message_service=message_service,
    )
    return handler, bot_id, message_service, conversation_service, provisioner


def _payload(recipients, enabled=True):
    return {
        "widget_slug": "contact",
        "fields": [
            {"id": "name", "label": "Name", "value": "Ada"},
            {"id": "message", "label": "Message", "value": "Hello there"},
        ],
        "source_host": "example.com",
        "meinchat": {
            "enabled": enabled,
            "sender_email": "form-bot@example.com",
            "sender_nickname": "contactbot",
            "recipients": recipients,
        },
    }


def test_admin_handle_resolves_to_admin_role_users():
    admin = SimpleNamespace(id=uuid4(), role=UserRole.ADMIN)
    handler, bot_id, message_service, conv_service, provisioner = _make_handler(
        admin_users=[admin]
    )

    handler.handle("contact_form.received", _payload(["@admin"]))

    provisioner.ensure_bot_sender.assert_called_once_with(
        "form-bot@example.com", "contactbot"
    )
    conv_service.start_or_get.assert_called_once_with(bot_id, admin.id)
    assert message_service.send_text.call_count == 1
    _, kwargs = message_service.send_text.call_args
    assert kwargs["sender_user_id"] == bot_id
    assert "Hello there" in kwargs["body"]


def test_nickname_handle_resolves_via_find_by_nickname_ci():
    recipient = SimpleNamespace(user_id=uuid4())
    handler, bot_id, message_service, conv_service, _ = _make_handler(
        nickname_lookup={"support": recipient}
    )

    handler.handle("contact_form.received", _payload(["@support"]))

    conv_service.start_or_get.assert_called_once_with(bot_id, recipient.user_id)
    assert message_service.send_text.call_count == 1


def test_unresolved_handle_is_skipped_not_errored():
    handler, bot_id, message_service, _, _ = _make_handler(
        admin_users=[], nickname_lookup={}
    )

    # @admin resolves to nothing (no admins) and @ghost is unknown.
    handler.handle("contact_form.received", _payload(["@admin", "@ghost"]))

    message_service.send_text.assert_not_called()


def test_distinct_recipients_only():
    admin = SimpleNamespace(id=uuid4(), role=UserRole.ADMIN)
    nickname_row = SimpleNamespace(user_id=admin.id)  # same user via two handles
    handler, bot_id, message_service, conv_service, _ = _make_handler(
        admin_users=[admin], nickname_lookup={"admin_nick": nickname_row}
    )

    handler.handle("contact_form.received", _payload(["@admin", "@admin_nick"]))

    assert message_service.send_text.call_count == 1


def test_disabled_block_is_noop():
    handler, _, message_service, _, provisioner = _make_handler()

    handler.handle("contact_form.received", _payload(["@admin"], enabled=False))

    provisioner.ensure_bot_sender.assert_not_called()
    message_service.send_text.assert_not_called()


def test_absent_block_is_noop():
    handler, _, message_service, _, provisioner = _make_handler()

    handler.handle("contact_form.received", {"widget_slug": "contact", "fields": []})

    provisioner.ensure_bot_sender.assert_not_called()
    message_service.send_text.assert_not_called()


def test_handler_never_raises_on_failure():
    handler, _, message_service, _, provisioner = _make_handler(
        admin_users=[SimpleNamespace(id=uuid4(), role=UserRole.ADMIN)]
    )
    message_service.send_text.side_effect = RuntimeError("boom")

    # Must swallow the error — the contact-form POST must not break.
    handler.handle("contact_form.received", _payload(["@admin"]))


def test_provisioning_failure_never_raises():
    handler, _, message_service, _, provisioner = _make_handler(
        admin_users=[SimpleNamespace(id=uuid4(), role=UserRole.ADMIN)]
    )
    provisioner.ensure_bot_sender.side_effect = RuntimeError("nope")

    handler.handle("contact_form.received", _payload(["@admin"]))

    message_service.send_text.assert_not_called()
