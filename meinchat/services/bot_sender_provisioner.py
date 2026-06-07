"""Provision the per-contact-form bot sender (S60).

A contact form delivers its submissions into meinchat as messages from a
dedicated, non-privileged ``BOT`` user (one per form, identified by email +
nickname). This provisioner creates that user idempotently and never
repurposes a real account.
"""
import logging
from typing import Any
from uuid import UUID

from vbwd.models.enums import UserRole

logger = logging.getLogger(__name__)

# Weak-by-design password: a BOT cannot log in (auth rejects role==BOT), so
# this credential is inert. Documented in S60.
_BOT_PASSWORD = "useruser"  # noqa: S105 — see module docstring; non-secret


class NonBotAccountError(Exception):
    """The configured sender email already belongs to a non-BOT account.

    Provisioning aborts rather than hijack a real user.
    """


class BotSenderProvisioner:
    """Idempotently ensure a BOT user + meinchat nickname for a contact form."""

    def __init__(
        self,
        *,
        user_service: Any,
        user_repository: Any,
        nickname_service: Any,
        session: Any,
    ) -> None:
        self._user_service = user_service
        self._user_repository = user_repository
        self._nickname_service = nickname_service
        self._session = session

    def ensure_bot_sender(self, email: str, nickname: str) -> UUID:
        """Return the bot user's id, creating the user + nickname if absent.

        Raises:
            NonBotAccountError: the email already belongs to a real (non-BOT)
                account — never repurpose it.
        """
        existing = self._user_repository.find_by_email(email)
        if existing is not None:
            if existing.role != UserRole.BOT:
                raise NonBotAccountError(
                    f"email {email!r} already belongs to a non-BOT account"
                )
            user_id = existing.id
        else:
            created = self._user_service.admin_create(
                {
                    "email": email,
                    "password": _BOT_PASSWORD,
                    "role": "BOT",
                    "status": "ACTIVE",
                },
                self._session,
            )
            user_id = created.id

        # Ensure the meinchat nickname (idempotent: set_nickname is a no-op
        # when the user already owns this slug).
        self._nickname_service.set_nickname(user_id, nickname)
        return user_id
