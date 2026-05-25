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
    # Placeholders for subsequent slices:
    "message_rate_per_minute": 30,
    "attachment_rate_per_hour": 6,
    "attachment_max_bytes": 5 * 1024 * 1024,
    "attachment_max_dimension_px": 2048,
    "sse_heartbeat_seconds": 20,
    "sse_stream_token_ttl_minutes": 60,
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
        pass

    def on_disable(self) -> None:
        pass
