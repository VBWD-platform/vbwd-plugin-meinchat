"""Business logic for messages: send, read, paginate, hard-delete."""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from plugins.meinchat.meinchat.extensibility import registry
from plugins.meinchat.meinchat.extensibility.identity import (
    IDeviceDirectory,
    NullDeviceDirectory,
)
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


class AttachmentNotFoundError(Exception):
    """Attachment row missing, or its message is not visible to the caller."""


class PlainAttachmentError(ValueError):
    """Client tried to attach an e2e blob to a plain message (or vice-versa)."""


_BODY_MAX = 4000
# Cap the serialized `meta` so a structured payload can never bloat a row or
# the SSE stream. 8 KB comfortably fits a choice menu while bounding abuse.
_META_MAX_SERIALIZED_BYTES = 8 * 1024

logger = logging.getLogger(__name__)


def _validate_meta(meta: Dict[str, Any]) -> None:
    """Reject a malformed/oversize structured `meta` with a clear ``ValueError``
    (the route maps it to 400). `action_data` stays opaque — its content is
    never parsed or trusted here; only shape + size are enforced.

    Known kinds:
      * ``bot_choices`` — ``choices`` is a list of
        ``{label:str, action_data:str, hint?:str}``; optional ``text`` is a
        clean prompt string.
      * ``bot_action`` — ``action_data`` is a non-empty str.
      * ``bot_menu`` — ``commands`` is a list of ``{command:str, description:str}``.
      * ``bot_cart`` — ``items`` is a list of
        ``{name:str, quantity:int, unit_price, line_total}``, plus a ``total``
        and a ``currency`` str.
    An unknown ``kind`` carries no shape contract (additive — future client-only
    kinds need no backend change) but is still size-capped.
    """
    import json

    if not isinstance(meta, dict):
        raise ValueError("meta must be an object")
    try:
        serialized = json.dumps(meta, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("meta must be JSON-serializable") from exc
    if len(serialized.encode("utf-8")) > _META_MAX_SERIALIZED_BYTES:
        raise ValueError("meta exceeds the maximum serialized size")

    kind = meta.get("kind")
    if kind == "bot_choices":
        _validate_bot_choices_meta(meta)
    elif kind == "bot_action":
        if not isinstance(meta.get("action_data"), str) or not meta["action_data"]:
            raise ValueError("bot_action meta requires a non-empty 'action_data'")
    elif kind == "bot_menu":
        _validate_bot_menu_meta(meta)
    elif kind == "bot_cart":
        _validate_bot_cart_meta(meta)


def _validate_bot_choices_meta(meta: Dict[str, Any]) -> None:
    choices = meta.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("bot_choices meta requires a non-empty 'choices' list")
    for choice in choices:
        if not isinstance(choice, dict):
            raise ValueError("each choice must be an object")
        if not isinstance(choice.get("label"), str) or not choice["label"]:
            raise ValueError("each choice requires a non-empty 'label' string")
        if not isinstance(choice.get("action_data"), str) or not choice["action_data"]:
            raise ValueError("each choice requires a non-empty 'action_data' string")
        if "hint" in choice and not isinstance(choice["hint"], str):
            raise ValueError("choice 'hint' must be a string")
    if "text" in meta and not isinstance(meta["text"], str):
        raise ValueError("bot_choices 'text' must be a string")


def _validate_bot_menu_meta(meta: Dict[str, Any]) -> None:
    commands = meta.get("commands")
    if not isinstance(commands, list):
        raise ValueError("bot_menu meta requires a 'commands' list")
    for command_row in commands:
        if not isinstance(command_row, dict):
            raise ValueError("each bot_menu command must be an object")
        if (
            not isinstance(command_row.get("command"), str)
            or not command_row["command"]
        ):
            raise ValueError(
                "each bot_menu command requires a non-empty 'command' string"
            )
        if not isinstance(command_row.get("description"), str):
            raise ValueError("each bot_menu command requires a 'description' string")


def _validate_bot_cart_meta(meta: Dict[str, Any]) -> None:
    items = meta.get("items")
    if not isinstance(items, list):
        raise ValueError("bot_cart meta requires an 'items' list")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each bot_cart item must be an object")
        if not isinstance(item.get("name"), str) or not item["name"]:
            raise ValueError("each bot_cart item requires a non-empty 'name' string")
        if not _is_integer(item.get("quantity")):
            raise ValueError("each bot_cart item requires an integer 'quantity'")
        for amount_field in ("unit_price", "line_total"):
            if amount_field not in item:
                raise ValueError(f"each bot_cart item requires a '{amount_field}'")
    if "total" not in meta:
        raise ValueError("bot_cart meta requires a 'total'")
    if not isinstance(meta.get("currency"), str):
        raise ValueError("bot_cart meta requires a 'currency' string")


def _is_integer(value: Any) -> bool:
    """True for a real integer (a JSON ``bool`` is not an integer quantity)."""
    return isinstance(value, int) and not isinstance(value, bool)


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
        attachment_repo: Optional[Any] = None,
    ) -> None:
        self._conv_repo = conv_repo
        self._message_repo = message_repo
        self._nickname_repo = nickname_repo
        self._attachments = attachment_service
        self._event_bus = event_bus
        self._attachment_repo = attachment_repo

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

    @staticmethod
    def _device_directory() -> IDeviceDirectory:
        """Resolve the registered device directory, falling back to the null
        directory (meinchat-alone has no device keys)."""
        try:
            return registry.resolve_first(IDeviceDirectory)
        except LookupError:
            return NullDeviceDirectory()

    def _expected_device_ids(self, peer_id: UUID, sender_id: UUID) -> tuple:
        """Addressed device set for an e2e send: the peer's active devices
        plus the sender's own (own-device decrypt). Each id is the 16-byte
        UUID form the client packs into the envelope's per-recipient slots,
        so the codec can reject envelopes addressed to unknown devices.
        Empty for meinchat-alone (NullDeviceDirectory) — plain sends ignore
        it (backward-compatible)."""
        directory = self._device_directory()
        devices = (
            *directory.lookup_active(peer_id),
            *directory.lookup_active(sender_id),
        )
        return tuple(device.id.bytes for device in devices)

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
        meta: Optional[Dict[str, Any]] = None,
    ) -> Message:
        if meta is not None:
            _validate_meta(meta)
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
            expected_device_ids=(
                ()
                if protocol_hint == "plain"
                else self._expected_device_ids(peer_id, sender_user_id)
            ),
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
        msg.meta = meta
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

        if self._attachment_repo is None:
            raise RuntimeError("AttachmentRepository not injected")

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
        msg.sent_at = now
        self._message_repo.save(msg)

        # S28.4 — plain attachments live in the child table too: one `fullres`
        # row (carrying the resized dimensions) + one `thumb` row.
        self._attachment_repo.add(
            message_id=msg.id,
            kind="fullres",
            storage_url=attachment["attachment_url"],
            protocol="plain",
            envelope_header=None,
            mime="image/webp",
            bytes_count=0,
            width_px=attachment["attachment_width_px"],
            height_px=attachment["attachment_height_px"],
        )
        self._attachment_repo.add(
            message_id=msg.id,
            kind="thumb",
            storage_url=attachment["attachment_thumb_url"],
            protocol="plain",
            envelope_header=None,
            mime="image/webp",
            bytes_count=0,
        )

        peer_id = ConversationService.peer_of(sender_user_id, conv)
        ConversationService.increment_unread_for(peer_id, conv)
        conv.last_message_at = now
        conv.last_message_preview = body_clean[:120] if body_clean else "[image]"
        self._conv_repo.save(conv)

        message_dict = msg.to_dict()
        self._publish(peer_id, "message", {"message": message_dict})
        self._publish(sender_user_id, "message", {"message": message_dict})
        return msg

    # ── e2e attachments (S28.4) ─────────────────────────────────────────────

    def add_e2e_attachment(
        self,
        message_id: UUID,
        *,
        caller_user_id: UUID,
        kind: str,
        ciphertext: bytes,
        envelope_header: Dict[str, Any],
        mime: str,
    ):
        """Attach a client-encrypted blob to an existing e2e message.

        The caller must be the message's sender. The server stores the opaque
        ciphertext (never decodes/resizes) and records a `meinchat_attachment`
        child row carrying the per-recipient key envelope. Returns the row.
        """
        if self._attachments is None or self._attachment_repo is None:
            raise RuntimeError("attachment service/repo not injected")
        msg = self._message_repo.find_by_id(message_id)
        if msg is None or str(msg.sender_id) != str(caller_user_id):
            # Same opaque error whether missing or not-owned (no probing).
            raise MessageNotFoundError(f"message {message_id} not found")
        if msg.protocol == "plain":
            raise PlainAttachmentError(
                "cannot attach an encrypted blob to a plain message"
            )
        coords = self._attachments.store_encrypted(
            ciphertext,
            owner_user_id=caller_user_id,
            kind=kind,
            mime=mime,
            protocol=msg.protocol,
        )
        return self._attachment_repo.add(
            message_id=msg.id,
            kind=coords["kind"],
            storage_url=coords["storage_url"],
            protocol=coords["protocol"],
            envelope_header=envelope_header,
            mime=coords["mime"],
            bytes_count=coords["bytes_count"],
        )

    def get_attachment_blob(self, attachment_id: UUID, *, caller_user_id: UUID):
        """Return `(bytes, mime)` for an attachment the caller may read (they
        must be a participant of the attachment's conversation). Bytes are
        opaque ciphertext for e2e rows — the client decrypts."""
        if self._attachments is None or self._attachment_repo is None:
            raise RuntimeError("attachment service/repo not injected")
        row = self._attachment_repo.find_by_id(attachment_id)
        if row is None:
            raise AttachmentNotFoundError(f"attachment {attachment_id} not found")
        msg = self._message_repo.find_by_id(row.message_id)
        conv = self._conv_repo.find_by_id(msg.conversation_id) if msg else None
        if conv is None or not ConversationService.is_member(caller_user_id, conv):
            raise AttachmentNotFoundError(f"attachment {attachment_id} not found")
        path = _url_to_storage_path(row.storage_url)
        return self._attachments.read_blob(path), row.mime

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
        # stays in sync even if the commit fails afterwards. Each child
        # attachment blob (plain or e2e) is deleted from storage; the rows
        # themselves cascade with the message. Attachment-less messages skip
        # this with no storage calls.
        if self._attachments is not None:
            for att in getattr(msg, "attachments", []) or []:
                path = _url_to_storage_path(att.storage_url)
                if path is not None:
                    self._attachments.delete_attachment(
                        original_path=path, thumb_path=None
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
