"""APNs push hook (S66) — the gnostic half of the push pipeline.

Core owns the mechanism (device tokens + ApnsClient + dispatcher registry);
this hook owns the meaning: it translates a meinchat ``Message`` into the
APNs payload (§4.3 + the S67.1 root keys), resolves the recipient's active
iOS tokens for its app, computes the badge from the inbox unread counts,
and revokes a token APNs reports stale (410) — the transport client never
mutates rows.

Registered via ``push_dispatcher_registry.register_push_handler`` in the
plugin's ``on_enable``; the registry logs and swallows any exception, so a
push can never fail or block a message send.
"""
import logging
from typing import Callable, List, Optional

from vbwd.services.apns_client import ApnsClient, ApnsSendResult
from vbwd.services.push_dispatcher_registry import PushEvent

from plugins.meinchat.meinchat.services.conversation_service import (
    ConversationService,
)

logger = logging.getLogger(__name__)

# For e2e_v1 the server cannot decrypt — the body is a fixed placeholder and
# the envelope/plaintext NEVER enter the push payload (iOS decrypts on
# foreground; the NSE is out of scope for v1).
ENCRYPTED_BODY_PLACEHOLDER = "🔒 New encrypted message"
E2E_PROTOCOL = "e2e_v1"
PREVIEW_MAX_CHARS = 120
APNS_TOKEN_GONE_STATUS = 410
# Apple requires apns-priority 5 for background (silent) pushes.
BACKGROUND_PUSH_TYPE = "background"
BACKGROUND_PUSH_PRIORITY = 5
BADGE_ONLY_KIND = "badge_only"


class MeinChatPushHook:
    """Translate a sent message into APNs pushes for the recipient's devices.

    Collaborators arrive as zero-arg providers so each dispatch builds them
    off the current request session (the hook itself is registered once at
    plugin-enable time). ``app_name`` scopes token targeting — meinchat-plus
    registers a second instance with ``app_name="meinchat-plus"`` so each
    iOS app's tokens are addressed independently (S67.1 §5).
    """

    def __init__(
        self,
        *,
        apns_client: ApnsClient,
        device_token_service_provider: Callable,
        conversation_repository_provider: Callable,
        app_name: str = "meinchat",
    ) -> None:
        self._apns_client = apns_client
        self._device_token_service_provider = device_token_service_provider
        self._conversation_repository_provider = conversation_repository_provider
        self._app_name = app_name

    def __call__(self, event: PushEvent) -> None:
        if event.kind == BADGE_ONLY_KIND:
            self._handle_badge_only(event)
            return
        self._handle_message(event)

    # ── message (S66 alert) ──────────────────────────────────────────────────

    def _handle_message(self, event: PushEvent) -> None:
        message = event.message
        recipient_user_id = event.recipient_user_id
        if str(message.sender_id) == str(recipient_user_id):
            return  # self-push guard (impossible in a 1-to-1, but cheap)

        device_token_service = self._device_token_service_provider()
        active_rows = device_token_service.find_active(
            recipient_user_id, self._app_name
        )
        if not active_rows:
            return  # short-circuit: no devices, no badge query, no APNs call

        payload = self._build_payload(
            message, event.conversation, self._unread_total(recipient_user_id)
        )
        results: List[ApnsSendResult] = self._apns_client.send_bulk(
            [row.token for row in active_rows], payload
        )
        self._revoke_stale_tokens(device_token_service, results)

    # ── badge_only (S68 silent) ──────────────────────────────────────────────

    def _handle_badge_only(self, event: PushEvent) -> None:
        """Push a silent badge update to the reader's OTHER iOS devices: no
        alert, no sound, no thread-id — only the home-screen icon badge moves.
        The originating device already cleared its badge in-process, so it is
        filtered out of the broadcast."""
        recipient_user_id = event.recipient_user_id
        device_token_service = self._device_token_service_provider()
        target_tokens = [
            row.token
            for row in device_token_service.find_active(
                recipient_user_id, self._app_name
            )
            if row.token != event.originating_device_token
        ]
        if not target_tokens:
            return  # short-circuit: nothing to push, no badge query, no APNs call

        payload = self._build_silent_payload(self._unread_total(recipient_user_id))
        results: List[ApnsSendResult] = self._apns_client.send_bulk(
            target_tokens,
            payload,
            push_type=BACKGROUND_PUSH_TYPE,
            priority=BACKGROUND_PUSH_PRIORITY,
        )
        self._revoke_stale_tokens(device_token_service, results)

    @staticmethod
    def _build_silent_payload(badge: int) -> dict:
        return {"aps": {"badge": badge, "content-available": 1}}

    # ── internals ───────────────────────────────────────────────────────────

    def _unread_total(self, recipient_user_id) -> int:
        """Badge = Σ the recipient's unread counts across their inbox —
        the same per-conversation counters the inbox listing serves."""
        conversation_repository = self._conversation_repository_provider()
        return sum(
            ConversationService.unread_for(recipient_user_id, conversation)
            for conversation in conversation_repository.list_for_user(recipient_user_id)
        )

    def _build_payload(self, message, conversation, badge: int) -> dict:
        return {
            "aps": {
                "alert": {
                    "title": f"@{message.sender_nickname}",
                    "body": self._alert_body(message),
                },
                "badge": badge,
                "sound": "default",
                "thread-id": str(conversation.id),
            },
            "conversation_id": str(conversation.id),
            "message_id": str(message.id),
            # The S35 tap-route /meinchat/<peer>?conv=<id>: from the
            # recipient's side, the peer is the sender.
            "peer_nickname": message.sender_nickname,
            "protocol": message.protocol,
        }

    @staticmethod
    def _alert_body(message) -> str:
        if message.protocol == E2E_PROTOCOL:
            return ENCRYPTED_BODY_PLACEHOLDER
        return (message.body or "")[:PREVIEW_MAX_CHARS]

    def _revoke_stale_tokens(
        self, device_token_service, results: List[ApnsSendResult]
    ) -> None:
        """The hook (caller), not the transport client, revokes 410 tokens."""
        for result in results:
            if result.status == APNS_TOKEN_GONE_STATUS:
                logger.info(
                    "[meinchat] revoking stale device token %s… (APNs %s)",
                    result.token[:8],
                    result.reason or APNS_TOKEN_GONE_STATUS,
                )
                device_token_service.unregister(result.token)


def build_push_hook(app_name: str, apns_client: Optional[ApnsClient] = None):
    """Wire a hook against the live app: core DI for the device-token
    service, the request-scoped session for the inbox query. Shared by
    meinchat's and meinchat-plus's ``on_enable``."""
    from flask import current_app

    from vbwd.extensions import db
    from vbwd.services.apns_client import apns_client_from_env

    from plugins.meinchat.meinchat.repositories.conversation_repository import (
        ConversationRepository,
    )

    def _device_token_service():
        return current_app.container.device_token_service()

    def _conversation_repository():
        return ConversationRepository(db.session)

    return MeinChatPushHook(
        apns_client=apns_client or apns_client_from_env(),
        device_token_service_provider=_device_token_service,
        conversation_repository_provider=_conversation_repository,
        app_name=app_name,
    )
