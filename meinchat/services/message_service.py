"""Business logic for messages: send, read, paginate, hard-delete."""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from plugins.meinchat.meinchat.extensibility import registry
from plugins.meinchat.meinchat.extensibility.pipeline import (
    IBodyCodec,
    IdentityBodyCodec,
    IPostSendHook,
    SendContext,
)
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
from plugins.meinchat.meinchat.services.attachment_service import AttachmentService
from plugins.meinchat.meinchat.services.conversation_service import (
    ConversationService,
)


class ConversationNotFoundError(Exception):
    pass


class NotAConversationMemberError(Exception):
    """Caller is not one of the conversation's two participants."""


class MessageNotFoundError(Exception):
    """Message missing or not authored by the caller (same error — no
    probing the authorship of other users' messages)."""


class MessageBodyTooLongError(ValueError):
    pass


_BODY_MAX = 4000

logger = logging.getLogger(__name__)


def _url_to_storage_path(url: Optional[str]) -> Optional[str]:
    """Recover the storage-relative path from the public URL.

    Our attachment paths always carry the `meinchat/` prefix, so cutting
    the URL at that marker is unambiguous and independent of the storage
    backend's base_url.
    """
    if not url:
        return None
    marker = "meinchat/"
    idx = url.find(marker)
    return url[idx:] if idx >= 0 else url.lstrip("/")


class MessageService:
    """Send / read / paginate / delete.

    Hard-delete semantics (Q4): `delete_message` removes the row for both
    participants. Attachment cleanup wires in next slice alongside the
    attachment upload endpoint.
    """

    def __init__(
        self,
        conv_repo: ConversationRepository,
        message_repo: MessageRepository,
        nickname_repo: NicknameRepository,
        attachment_service: Optional[AttachmentService] = None,
        event_bus: Optional[Any] = None,
    ) -> None:
        self._conv_repo = conv_repo
        self._message_repo = message_repo
        self._nickname_repo = nickname_repo
        self._attachments = attachment_service
        self._event_bus = event_bus

    def _publish(self, user_id, event_type: str, payload: Dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        body = {"type": event_type, **payload}
        self._event_bus.publish(f"user:{user_id}", body)

    @staticmethod
    def _body_codec() -> IBodyCodec:
        """Resolve the registered body codec, falling back to the identity
        codec when no plugin registered one (meinchat-alone / unit tests)."""
        try:
            return registry.resolve_first(IBodyCodec)
        except LookupError:
            return IdentityBodyCodec()

    def _run_post_send_hooks(self, row: Message) -> None:
        for hook in registry.resolve_all(IPostSendHook):
            try:
                hook.on_sent(row)
            except Exception as exc:  # a hook must never fail the send
                logger.error("post-send hook %s failed: %s", hook, exc)

    # ── send ───────────────────────────────────────────────────────────────

    def send_text(
        self,
        conversation_id: UUID,
        *,
        sender_user_id: UUID,
        body: str,
        protocol_hint: str = "plain",
    ) -> Message:
        conv = self._conv_repo.find_by_id(conversation_id)
        if conv is None:
            raise ConversationNotFoundError(f"conversation {conversation_id} not found")
        if not ConversationService.is_member(sender_user_id, conv):
            raise NotAConversationMemberError(
                f"user {sender_user_id} is not in this conversation"
            )

        body_clean = (body or "").strip()
        # Plaintext validation stays inline (single impl, no second consumer).
        # Non-plain (envelope) rows validate inside the registered codec.
        if protocol_hint == "plain":
            if not body_clean:
                raise ValueError("body must be non-empty")
            if len(body_clean) > _BODY_MAX:
                raise MessageBodyTooLongError(f"body exceeds {_BODY_MAX} characters")

        sender_nick = self._nickname_repo.find_by_user_id(sender_user_id)
        sender_nickname = sender_nick.nickname if sender_nick is not None else ""

        peer_id = ConversationService.peer_of(sender_user_id, conv)
        ctx = SendContext(
            sender=sender_user_id,
            recipients=[peer_id],
            conversation=conv,
            body_or_envelope=body_clean if protocol_hint == "plain" else body,
            protocol_hint=protocol_hint,
        )
        encoded = self._body_codec().encode(ctx)

        now = datetime.now(timezone.utc)
        msg = Message()
        msg.conversation_id = conversation_id
        msg.sender_id = sender_user_id
        msg.sender_nickname = sender_nickname
        msg.protocol = encoded.protocol
        msg.body = encoded.body
        msg.envelope = encoded.envelope
        msg.sent_at = now
        self._message_repo.save(msg)

        ConversationService.increment_unread_for(peer_id, conv)
        conv.last_message_at = now
        conv.last_message_preview = (
            body_clean[:120] if encoded.body is not None else "[encrypted]"
        )
        self._conv_repo.save(conv)

        message_dict = msg.to_dict()
        self._publish(peer_id, "message", {"message": message_dict})
        self._publish(sender_user_id, "message", {"message": message_dict})
        self._run_post_send_hooks(msg)
        return msg

    # ── read ──────────────────────────────────────────────────────────────

    def list_messages(
        self,
        conversation_id: UUID,
        *,
        caller_user_id: UUID,
        before: Optional[UUID] = None,
        limit: int = 50,
    ) -> List[Message]:
        conv = self._require_member(conversation_id, caller_user_id)
        del conv  # unused after the authz check
        return self._message_repo.page(conversation_id, before=before, limit=limit)

    def mark_read(self, conversation_id: UUID, *, reader_user_id: UUID) -> None:
        conv = self._require_member(conversation_id, reader_user_id)
        ConversationService.clear_unread_for(reader_user_id, conv)
        self._conv_repo.save(conv)

        peer_id = ConversationService.peer_of(reader_user_id, conv)
        payload = {
            "conversation_id": str(conversation_id),
            "reader_id": str(reader_user_id),
        }
        self._publish(peer_id, "read", payload)
        self._publish(reader_user_id, "read", payload)

    # ── send (attachment variant) ──────────────────────────────────────────

    def send_attachment(
        self,
        conversation_id: UUID,
        *,
        sender_user_id: UUID,
        raw_image_bytes: bytes,
        body: str = "",
    ) -> Message:
        if self._attachments is None:
            raise RuntimeError("AttachmentService not injected")

        conv = self._conv_repo.find_by_id(conversation_id)
        if conv is None:
            raise ConversationNotFoundError(f"conversation {conversation_id} not found")
        if not ConversationService.is_member(sender_user_id, conv):
            raise NotAConversationMemberError(
                f"user {sender_user_id} is not in this conversation"
            )

        body_clean = (body or "").strip()
        if len(body_clean) > _BODY_MAX:
            raise MessageBodyTooLongError(f"body exceeds {_BODY_MAX} characters")

        attachment = self._attachments.process_and_store(
            raw_image_bytes, owner_user_id=sender_user_id
        )

        sender_nick = self._nickname_repo.find_by_user_id(sender_user_id)
        sender_nickname = sender_nick.nickname if sender_nick is not None else ""

        now = datetime.now(timezone.utc)
        msg = Message()
        msg.conversation_id = conversation_id
        msg.sender_id = sender_user_id
        msg.sender_nickname = sender_nickname
        msg.body = body_clean
        msg.attachment_url = attachment["attachment_url"]
        msg.attachment_thumb_url = attachment["attachment_thumb_url"]
        msg.attachment_width_px = attachment["attachment_width_px"]
        msg.attachment_height_px = attachment["attachment_height_px"]
        msg.sent_at = now
        self._message_repo.save(msg)

        peer_id = ConversationService.peer_of(sender_user_id, conv)
        ConversationService.increment_unread_for(peer_id, conv)
        conv.last_message_at = now
        conv.last_message_preview = body_clean[:120] if body_clean else "[image]"
        self._conv_repo.save(conv)

        message_dict = msg.to_dict()
        self._publish(peer_id, "message", {"message": message_dict})
        self._publish(sender_user_id, "message", {"message": message_dict})
        return msg

    # ── system messages (token_transfer) ───────────────────────────────────

    def post_system_message(
        self,
        *,
        conversation_id: UUID,
        sender_user_id: UUID,
        system_kind: str,
        payload: Dict[str, Any],
    ) -> Message:
        """Write a `system_kind` row (e.g. "token_transfer") straight into
        the timeline. Bypasses body-empty / rate-limit / membership checks
        because only other services emit these (not end users)."""
        import json

        conv = self._conv_repo.find_by_id(conversation_id)
        if conv is None:
            raise ConversationNotFoundError(f"conversation {conversation_id} not found")

        sender_nick = self._nickname_repo.find_by_user_id(sender_user_id)
        sender_nickname = sender_nick.nickname if sender_nick is not None else ""

        now = datetime.now(timezone.utc)
        msg = Message()
        msg.conversation_id = conversation_id
        msg.sender_id = sender_user_id
        msg.sender_nickname = sender_nickname
        msg.body = json.dumps(payload, ensure_ascii=False)
        msg.sent_at = now
        msg.system_kind = system_kind
        self._message_repo.save(msg)

        peer_id = ConversationService.peer_of(sender_user_id, conv)
        conv.last_message_at = now
        conv.last_message_preview = f"[{system_kind}]"
        # System messages do bump unread — the recipient should know they
        # got tokens/etc. without opening the thread.
        ConversationService.increment_unread_for(peer_id, conv)
        self._conv_repo.save(conv)

        message_dict = msg.to_dict()
        self._publish(peer_id, "message", {"message": message_dict})
        self._publish(sender_user_id, "message", {"message": message_dict})
        return msg

    # ── delete ─────────────────────────────────────────────────────────────

    def delete_message(
        self, message_id: UUID, *, caller_user_id: UUID
    ) -> Dict[str, Any]:
        msg = self._message_repo.find_by_id(message_id)
        if msg is None or msg.sender_id != caller_user_id:
            raise MessageNotFoundError(f"message {message_id} not found")
        conv = self._conv_repo.find_by_id(msg.conversation_id)
        recipient_id = ConversationService.peer_of(caller_user_id, conv)

        # Purge attachment bytes (Q3) before dropping the DB row so storage
        # stays in sync even if the commit fails afterwards. Attachment-less
        # messages skip this with no storage calls.
        if self._attachments is not None and msg.attachment_url:
            self._attachments.delete_attachment(
                original_path=_url_to_storage_path(msg.attachment_url),
                thumb_path=_url_to_storage_path(msg.attachment_thumb_url),
            )

        self._message_repo.delete(msg)

        payload = {
            "conversation_id": str(msg.conversation_id),
            "message_id": str(message_id),
        }
        self._publish(recipient_id, "message_deleted", payload)
        self._publish(caller_user_id, "message_deleted", payload)

        return {
            "message_id": message_id,
            "conversation_id": msg.conversation_id,
            "recipient_id": recipient_id,
        }

    # ── private ────────────────────────────────────────────────────────────

    def _require_member(self, conversation_id, user_id):
        conv = self._conv_repo.find_by_id(conversation_id)
        if conv is None:
            raise ConversationNotFoundError(f"conversation {conversation_id} not found")
        if not ConversationService.is_member(user_id, conv):
            raise NotAConversationMemberError(
                f"user {user_id} is not in this conversation"
            )
        return conv
