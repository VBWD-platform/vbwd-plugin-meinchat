"""Meinchat plugin — nickname + contacts + messaging + token transfer.

Sprint 57 slice one ships the nickname subsystem; contacts / messaging /
token-transfer land in the same plugin in subsequent commits.
"""
from typing import Any, Dict, Optional, TYPE_CHECKING

from vbwd.plugins.base import BasePlugin, PluginMetadata


if TYPE_CHECKING:
    from flask import Blueprint


DEFAULT_CONFIG: Dict[str, Any] = {
    # Decision Q5: banned slug becomes reusable this many days after ban.
    "nickname_ban_grace_period_days": 30,
    # Pre-S26 flat keys — kept as fallback for instances upgrading from an
    # older config. RateLimitPolicy reads these only when the new
    # `rate_message_send_*` / `rate_attachment_send_*` pair is missing.
    "message_rate_per_minute": 30,
    "attachment_rate_per_hour": 6,
    "attachment_max_bytes": 5 * 1024 * 1024,
    "attachment_max_dimension_px": 2048,
    "sse_heartbeat_seconds": 20,
    "sse_stream_token_ttl_minutes": 60,
    # ── Baseline rate limits (web + unknown platforms) ──────────────────────
    "rate_new_conversation_per_window": 10,
    "rate_new_conversation_window_seconds": 3600,
    "rate_nickname_search_per_window": 30,
    "rate_nickname_search_window_seconds": 60,
    "rate_message_send_per_window": 30,
    "rate_message_send_window_seconds": 60,
    "rate_attachment_send_per_window": 6,
    "rate_attachment_send_window_seconds": 3600,
    # ── iOS overrides (selected when X-Client-Platform: ios) ────────────────
    "rate_ios_new_conversation_per_window": 60,
    "rate_ios_new_conversation_window_seconds": 3600,
    "rate_ios_nickname_search_per_window": 90,
    "rate_ios_nickname_search_window_seconds": 60,
    "rate_ios_message_send_per_window": 120,
    "rate_ios_message_send_window_seconds": 60,
    "rate_ios_attachment_send_per_window": 30,
    "rate_ios_attachment_send_window_seconds": 3600,
}


class MeinchatPlugin(BasePlugin):
    """Nickname + contacts + messaging + token transfer, as one bundle."""

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="meinchat",
            version="1.0.0",
            author="VBWD Team",
            description=(
                "User nickname directory, address book, 1-on-1 messaging "
                "with images, and peer token transfer."
            ),
            dependencies=["subscription"],
        )

    def initialize(self, config: Optional[Dict[str, Any]] = None) -> None:
        merged = {**DEFAULT_CONFIG}
        if config:
            merged.update(config)
        super().initialize(merged)

    def get_blueprint(self) -> Optional["Blueprint"]:
        from plugins.meinchat.meinchat.routes import meinchat_bp

        return meinchat_bp

    def get_url_prefix(self) -> Optional[str]:
        # All routes carry absolute `/api/v1/…` paths, no prefix needed.
        return ""

    @property
    def admin_permissions(self):
        return [
            {
                "key": "meinchat.nicknames.moderate",
                "label": "Ban / unban nicknames",
                "group": "Meinchat",
            },
            {
                "key": "meinchat.conversations.inspect",
                "label": "Inspect conversations (moderation)",
                "group": "Meinchat",
            },
            {
                "key": "meinchat.transfers.view",
                "label": "View token-transfer log",
                "group": "Meinchat",
            },
        ]

    def on_enable(self) -> None:
        # S09 — register the plugin's repositories with the DI container so
        # handlers / routes / other plugins can resolve them via
        # `current_app.container.meinchat_<name>_repository()`.
        from flask import current_app

        from vbwd.plugins.di_helpers import register_repositories
        from plugins.meinchat.meinchat.repositories.contact_repository import (
            ContactRepository,
        )
        from plugins.meinchat.meinchat.repositories.conversation_repository import (
            ConversationRepository,
        )
        from plugins.meinchat.meinchat.repositories.message_repository import (
            MessageRepository,
        )
        from plugins.meinchat.meinchat.repositories.nickname_repository import (
            NicknameRepository,
        )
        from plugins.meinchat.meinchat.repositories.token_transfer_repository import (  # noqa: E501
            TokenTransferRepository,
        )

        container = getattr(current_app, "container", None)
        if container is not None:
            register_repositories(
                container,
                {
                    "meinchat_conversation_repository": ConversationRepository,
                    "meinchat_message_repository": MessageRepository,
                    "meinchat_contact_repository": ContactRepository,
                    "meinchat_nickname_repository": NicknameRepository,
                    "meinchat_token_transfer_repository": TokenTransferRepository,
                },
            )

    def on_disable(self) -> None:
        from flask import current_app

        from vbwd.plugins.di_helpers import unregister_repositories

        container = getattr(current_app, "container", None)
        if container is not None:
            unregister_repositories(
                container,
                [
                    "meinchat_conversation_repository",
                    "meinchat_message_repository",
                    "meinchat_contact_repository",
                    "meinchat_nickname_repository",
                    "meinchat_token_transfer_repository",
                ],
            )
