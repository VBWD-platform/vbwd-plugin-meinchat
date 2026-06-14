"""Unit tests for GuestSessionService (S86.3).

Provisions a core GUEST user + uniquified nickname + short-lived access token.
All collaborators mocked — no DB.
"""
from unittest.mock import MagicMock
from uuid import uuid4

from vbwd.models.enums import UserRole

from plugins.meinchat.meinchat.services.guest_session_service import (
    GuestSessionService,
    _slugify_nickname_base,
)


def _build_service():
    user_service = MagicMock()
    nickname_service = MagicMock()
    auth_service = MagicMock()
    session = MagicMock()

    created = MagicMock()
    created.id = uuid4()
    user_service.admin_create.return_value = created
    auth_service.generate_access_token.return_value = "guest.jwt.token"

    service = GuestSessionService(
        user_service=user_service,
        nickname_service=nickname_service,
        auth_service=auth_service,
        session=session,
        token_ttl_hours=2,
    )
    return service, user_service, nickname_service, auth_service, created


def test_provision_creates_guest_user_with_guest_role():
    service, user_service, _nickname, _auth, created = _build_service()

    result = service.provision("Jane Doe")

    assert result.user_id == created.id
    args, _kwargs = user_service.admin_create.call_args
    payload = args[0]
    assert payload["role"] == UserRole.GUEST.value
    assert payload["status"] == "ACTIVE"
    assert payload["email"].startswith("guest+")
    assert payload["email"].endswith("@widget.local")
    # Random, non-empty password (the account can never password-login anyway).
    assert isinstance(payload["password"], str) and len(payload["password"]) >= 16


def test_provision_sets_unique_nickname_derived_from_display_name():
    service, _user_service, nickname_service, _auth, created = _build_service()

    result = service.provision("Jane Doe")

    nickname_service.set_nickname.assert_called_once()
    set_user_id, set_nickname = nickname_service.set_nickname.call_args.args
    assert set_user_id == created.id
    assert set_nickname.startswith("jane-doe-")
    assert result.nickname == set_nickname


def test_provision_mints_short_lived_token():
    service, _user_service, _nickname, auth_service, created = _build_service()

    result = service.provision("Jane")

    auth_service.generate_access_token.assert_called_once()
    _args, kwargs = auth_service.generate_access_token.call_args
    assert kwargs["expiration_hours"] == 2
    assert result.access_token == "guest.jwt.token"


def test_slugify_falls_back_for_garbage_name():
    # Empty / leading-digit / punctuation-only names get the "guest" stem so the
    # final nickname always starts with a lowercase letter (validator rule).
    assert _slugify_nickname_base("").startswith("guest")
    assert _slugify_nickname_base("123").startswith("guest")
    assert _slugify_nickname_base("  !! ").startswith("guest")


def test_slugify_keeps_ascii_letters_from_display_name():
    assert _slugify_nickname_base("Jane Doe") == "jane-doe"
    # Non-ASCII letters are dropped; the remaining ASCII stem still starts with a
    # letter, so no fallback is needed.
    assert _slugify_nickname_base("Älterer Gast") == "lterer-gast"
