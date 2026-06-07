"""Deliver CMS contact-form submissions into meinchat (S60).

Subscribes to the core ``contact_form.received`` event. When the payload
carries an *enabled* meinchat block, the submission is posted as a message
from a per-form bot sender to each resolved recipient. The whole handler is
best-effort: any failure logs a warning and never raises, so it can never
break the contact-form POST or the email handler that shares the event.
"""
import logging
from typing import Any, Dict, List
from uuid import UUID

from vbwd.models.enums import UserRole

logger = logging.getLogger(__name__)

# Special handle resolving to the platform admin-role user(s).
_ADMIN_HANDLE = "@admin"
_ADMIN_ROLES = (UserRole.ADMIN, UserRole.SUPER_ADMIN)


class ContactFormHandler:
    """Bridge ``contact_form.received`` → meinchat messages."""

    def __init__(
        self,
        *,
        provisioner: Any,
        user_repository: Any,
        nickname_repository: Any,
        conversation_service: Any,
        message_service: Any,
    ) -> None:
        self._provisioner = provisioner
        self._user_repository = user_repository
        self._nickname_repository = nickname_repository
        self._conversation_service = conversation_service
        self._message_service = message_service

    # ── event entry point ────────────────────────────────────────────────────

    def handle(self, _event_name: str, payload: Dict[str, Any]) -> None:
        """Event callback — best-effort, never raises."""
        try:
            self._deliver(payload)
        except Exception as error:  # noqa: BLE001 — must not break the POST
            logger.warning(
                "[meinchat] contact-form delivery failed: %s", error, exc_info=True
            )

    # ── core logic ───────────────────────────────────────────────────────────

    def _deliver(self, payload: Dict[str, Any]) -> None:
        meinchat_block = payload.get("meinchat") or {}
        if not meinchat_block.get("enabled"):
            return

        sender_email = (meinchat_block.get("sender_email") or "").strip()
        sender_nickname = (meinchat_block.get("sender_nickname") or "").strip()
        if not sender_email or not sender_nickname:
            logger.warning(
                "[meinchat] contact-form meinchat block missing sender "
                "email/nickname — skipping delivery"
            )
            return

        bot_user_id = self._provisioner.ensure_bot_sender(sender_email, sender_nickname)

        recipient_ids = self._resolve_recipients(
            meinchat_block.get("recipients") or [], bot_user_id
        )
        if not recipient_ids:
            logger.warning(
                "[meinchat] contact-form: no recipients resolved — nothing sent"
            )
            return

        body = self._format_body(payload)
        for recipient_id in recipient_ids:
            self._send_one(bot_user_id, recipient_id, body)

    def _resolve_recipients(self, handles: List[str], bot_user_id: UUID) -> List[UUID]:
        """Map handles to distinct recipient user ids, skipping the bot itself
        and any handle that resolves to nothing (logged)."""
        resolved: List[UUID] = []
        seen = {bot_user_id}
        for handle in handles:
            for user_id in self._resolve_handle(handle):
                if user_id in seen:
                    continue
                seen.add(user_id)
                resolved.append(user_id)
        return resolved

    def _resolve_handle(self, handle: str) -> List[UUID]:
        normalized = (handle or "").strip()
        if not normalized:
            return []
        if normalized.lower() == _ADMIN_HANDLE:
            return self._admin_user_ids()
        nickname = self._nickname_repository.find_by_nickname_ci(normalized.lstrip("@"))
        if nickname is None:
            logger.warning("[meinchat] contact-form recipient %r unresolved", handle)
            return []
        return [nickname.user_id]

    def _admin_user_ids(self) -> List[UUID]:
        admins: List[UUID] = []
        for role in _ADMIN_ROLES:
            for user in self._user_repository.find_by_role(role):
                admins.append(user.id)
        return admins

    def _send_one(self, bot_user_id: UUID, recipient_id: UUID, body: str) -> None:
        conversation = self._conversation_service.start_or_get(
            bot_user_id, recipient_id
        )
        self._message_service.send_text(
            conversation.id, sender_user_id=bot_user_id, body=body
        )

    @staticmethod
    def _format_body(payload: Dict[str, Any]) -> str:
        """Readable plain-text summary of the submitted fields + source."""
        lines: List[str] = ["New contact-form submission:"]
        for field in payload.get("fields") or []:
            label = field.get("label") or field.get("id") or "?"
            value = field.get("value") or ""
            lines.append(f"{label}: {value}")
        source = payload.get("source_url") or payload.get("source_host")
        if source:
            lines.append(f"Source: {source}")
        return "\n".join(lines)
