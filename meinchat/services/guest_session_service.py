"""Provision a core ``GUEST`` for a public bot-widget (S86.3, D5/AD7).

Mirrors ``BotSenderProvisioner`` but creates a ``role=GUEST`` user (synthetic
``guest+<uuid>@widget.local`` email, random unguessable password — the account
can never password-login, the auth login guard rejects ``GUEST``) plus a
uniquified meinchat nickname derived from the visitor's ``display_name``, and
mints a short-TTL access token with the SAME payload shape the room/message
routes' ``@require_auth`` accepts.

This slice always provisions a FRESH guest per start. Return-visitor reuse +
the token grant (D11/D12) are deferred to S86.3 3c, which builds on the
``meinchat_guest_session`` row written by the caller.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from uuid import UUID, uuid4

from vbwd.models.enums import UserRole

from plugins.meinchat.meinchat.services.nickname_service import (
    NicknameService,
    NicknameTakenError,
)

# A GUEST cannot log in (the auth guard rejects role==GUEST), but the account
# still needs a credential that passes admin_create's strength check. A random
# password keeps the inert credential unguessable.
_GUEST_PASSWORD_BYTES = 24
_GUEST_NICKNAME_SUFFIX_BYTES = 4
_GUEST_NICKNAME_BASE_MAX_LEN = 16
_GUEST_NICKNAME_FALLBACK_BASE = "guest"
_MAX_NICKNAME_ATTEMPTS = 5


@dataclass(frozen=True)
class ProvisionedGuest:
    """The result of provisioning a widget guest."""

    user_id: UUID
    nickname: str
    access_token: str


class GuestSessionService:
    """Create a GUEST user + nickname + short-lived access token."""

    def __init__(
        self,
        *,
        user_service,
        nickname_service: NicknameService,
        auth_service,
        session,
        token_ttl_hours: float,
    ) -> None:
        self._user_service = user_service
        self._nickname_service = nickname_service
        self._auth_service = auth_service
        self._session = session
        self._token_ttl_hours = token_ttl_hours

    def provision(self, display_name: str) -> ProvisionedGuest:
        """Provision a fresh GUEST and return (user_id, nickname, token)."""
        email = f"guest+{uuid4()}@widget.local"
        password = secrets.token_urlsafe(_GUEST_PASSWORD_BYTES)
        created = self._user_service.admin_create(
            {
                "email": email,
                "password": password,
                "role": UserRole.GUEST.value,
                "status": "ACTIVE",
            },
            self._session,
        )
        nickname = self._assign_unique_nickname(created.id, display_name)
        access_token = self._auth_service.generate_access_token(
            created.id,
            email,
            expiration_hours=self._token_ttl_hours,
        )
        return ProvisionedGuest(
            user_id=created.id, nickname=nickname, access_token=access_token
        )

    def _assign_unique_nickname(self, user_id: UUID, display_name: str) -> str:
        """Set a valid, unique nickname derived from ``display_name``.

        Always appends a random suffix so two guests typing the same name never
        collide; retries with a fresh suffix on the rare race."""
        base = _slugify_nickname_base(display_name)
        last_error: Exception | None = None
        for _ in range(_MAX_NICKNAME_ATTEMPTS):
            candidate = f"{base}-{secrets.token_hex(_GUEST_NICKNAME_SUFFIX_BYTES)}"
            try:
                self._nickname_service.set_nickname(user_id, candidate)
                return candidate
            except NicknameTakenError as error:  # pragma: no cover - rare race
                last_error = error
        raise last_error  # type: ignore[misc]


def _slugify_nickname_base(display_name: str) -> str:
    """Reduce a free-text display name to a valid nickname stem.

    A nickname must match ``^[a-z][a-z0-9_-]{2,31}$``. The suffix the caller
    appends guarantees the final length, so the stem only needs to start with a
    lowercase letter and contain allowed characters; an empty/garbage name falls
    back to ``guest``."""
    lowered = (display_name or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"{_GUEST_NICKNAME_FALLBACK_BASE}-{cleaned}".strip("-")
    return cleaned[:_GUEST_NICKNAME_BASE_MAX_LEN].strip("-")
