"""Unit tests for the meinchat BotSenderProvisioner (S60).

Idempotently provisions the per-contact-form bot sender: a core user with
role ``BOT`` plus a meinchat nickname. Never repurposes a real (non-BOT)
account. All collaborators are mocked — no DB.
"""
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from vbwd.models.enums import UserRole
from vbwd.models.user import User

from plugins.meinchat.meinchat.services.bot_sender_provisioner import (
    BotSenderProvisioner,
    NonBotAccountError,
)


def _make_provisioner(existing_user=None):
    user_service = MagicMock()
    user_repository = MagicMock()
    nickname_service = MagicMock()
    session = MagicMock()

    user_repository.find_by_email.return_value = existing_user

    provisioner = BotSenderProvisioner(
        user_service=user_service,
        user_repository=user_repository,
        nickname_service=nickname_service,
        session=session,
    )
    return provisioner, user_service, user_repository, nickname_service


def test_creates_bot_user_and_nickname_when_absent():
    created = User()
    created.id = uuid4()
    created.email = "form-bot@example.com"
    created.role = UserRole.BOT

    provisioner, user_service, user_repo, nickname_service = _make_provisioner(
        existing_user=None
    )
    user_service.admin_create.return_value = created

    user_id = provisioner.ensure_bot_sender("form-bot@example.com", "contactbot")

    assert user_id == created.id
    # Created through the core user service with the BOT role + weak password.
    args, kwargs = user_service.admin_create.call_args
    payload = args[0] if args else kwargs["payload"]
    assert payload["email"] == "form-bot@example.com"
    assert payload["role"] == "BOT"
    assert payload["password"] == "useruser"
    # Nickname ensured via the nickname service.
    nickname_service.set_nickname.assert_called_once_with(created.id, "contactbot")


def test_reuses_existing_bot_account():
    existing = User()
    existing.id = uuid4()
    existing.email = "form-bot@example.com"
    existing.role = UserRole.BOT

    provisioner, user_service, user_repo, nickname_service = _make_provisioner(
        existing_user=existing
    )

    user_id = provisioner.ensure_bot_sender("form-bot@example.com", "contactbot")

    assert user_id == existing.id
    user_service.admin_create.assert_not_called()
    # Nickname is re-ensured idempotently (set_nickname is a no-op if same).
    nickname_service.set_nickname.assert_called_once_with(existing.id, "contactbot")


def test_aborts_when_email_belongs_to_real_account():
    real_user = User()
    real_user.id = uuid4()
    real_user.email = "real@example.com"
    real_user.role = UserRole.USER

    provisioner, user_service, user_repo, nickname_service = _make_provisioner(
        existing_user=real_user
    )

    with pytest.raises(NonBotAccountError):
        provisioner.ensure_bot_sender("real@example.com", "contactbot")

    user_service.admin_create.assert_not_called()
    nickname_service.set_nickname.assert_not_called()
