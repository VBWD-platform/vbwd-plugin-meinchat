"""Meinchat plugin — nickname + contacts + messaging + token transfer.

Sprint 57 slice one ships the nickname subsystem; contacts / messaging /
token-transfer land in the same plugin in subsequent commits.
"""
from typing import Any, Dict, Optional, TYPE_CHECKING

from flask import current_app

from vbwd.plugins.base import BasePlugin, PluginMetadata


if TYPE_CHECKING:
    from flask import Blueprint


DEFAULT_CONFIG: Dict[str, Any] = {
    # Decision Q5: banned slug becomes reusable this many days after ban.
    "nickname_ban_grace_period_days": 30,
    # ── S28.0 retention / size knobs (read by /messaging/limits, the S28.1
    # server prune, and the S28.2 client-cache TTL resolver) ────────────────
    "messages_retention_days_server": 2,
    "messages_retention_days_client_suggested": 10,
    "attachments_retention_days_server": 2,
    "ciphertext_max_bytes": 16384,
    # S28.1 — cron for the daily server-retention prune (5-field crontab,
    # UTC). Default: daily at 03:00 UTC. Overridable per instance.
    "retention_prune_cron": "0 3 * * *",
    # Pre-S26 flat keys — kept as fallback for instances upgrading from an
    # older config. RateLimitPolicy reads these only when the new
    # `rate_message_send_*` / `rate_attachment_send_*` pair is missing.
    "message_rate_per_minute": 30,
    "attachment_rate_per_hour": 6,
    "attachment_max_bytes": 25 * 1024 * 1024,
    "attachment_max_dimension_px": 4096,
    "sse_heartbeat_seconds": 20,
    "sse_stream_token_ttl_minutes": 60,
    # S-incident: cap each SSE stream's server-side lifetime so an idle stream
    # is recycled (the browser auto-reconnects) instead of pinning a worker.
    "sse_max_stream_seconds": 600,
    # S38 — SSE fan-out backend. auto: prefer Redis, warn+degrade to in-process
    # if down (dev). redis: require Redis, fail loud (recommended for prod).
    # memory: always in-process (single worker only).
    "event_bus_backend": "auto",
    "event_bus_channel_prefix": "meinchat:",
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
    # ── S86.3 bot-widget guest start ────────────────────────────────────────
    # Public "Start Conversation" is IP-rate-limited (anti-abuse) and provisions
    # a short-lived GUEST. The token economy (D11) + session reuse (D12) land in
    # a later slice; this slice only needs the start rate-limit + token TTL.
    "rate_widget_guest_start_per_window": 5,
    "rate_widget_guest_start_window_seconds": 3600,
    "widget_guest_token_ttl_hours": 24,
    # ── S86.3 — guest token economy (REVISED D11, word-based) ───────────────
    # 1 token = 1 word. On a NEW public start the guest is granted
    # `guest_initial_tokens` via the core TokenService. A POST-send hook then
    # debits `word_count × guest_token_cost_per_word` from the room's guest for
    # EVERY message (the guest's question AND the bot's answer). The pre-send
    # gate blocks (402) when the guest's balance is not positive; the in-flight
    # round may spend to zero, the NEXT send is gated and the FE shows a "Buy
    # tokens" block. Set `guest_economy_enabled` false to disable granting +
    # metering entirely.
    "guest_economy_enabled": True,
    "guest_initial_tokens": 20,
    "guest_token_cost_per_word": 1,
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
                "key": "meinchat.transfers.view",
                "label": "View token-transfer log",
                "group": "Meinchat",
            },
        ]

    def on_enable(self) -> None:
        # S09 — register the plugin's repositories with the DI container so
        # handlers / routes / other plugins can resolve them via
        # `current_app.container.meinchat_<name>_repository()`.
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
        from plugins.meinchat.meinchat.repositories.room_repository import (
            RoomRepository,
        )
        from plugins.meinchat.meinchat.repositories.room_member_repository import (
            RoomMemberRepository,
        )
        from plugins.meinchat.meinchat.repositories.guest_session_repository import (
            GuestSessionRepository,
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
                    "meinchat_room_repository": RoomRepository,
                    "meinchat_room_member_repository": RoomMemberRepository,
                    "meinchat_guest_session_repository": GuestSessionRepository,
                    "meinchat_token_transfer_repository": TokenTransferRepository,
                },
            )

        # S28.3a — register the extension-seam defaults so meinchat-alone
        # behaviour is preserved and downstream plugins (meinchat-plus) can
        # override/extend via the registry. Defaults must be present so the
        # capability union still includes {"plain"} when meinchat-plus adds
        # {"e2e_v1"}.
        self._register_extension_defaults()

        # S66 — APNs push: register the meinchat push hook with the core
        # dispatcher. The hook is gnostic (Message → payload); core only
        # transports. Without VBWD_APNS_* config the underlying client is a
        # warned-once no-op, so dev/CI keeps working.
        self._register_push_hook()

        # S28.1 — daily server-retention prune. Tests pass TESTING=True; the
        # scheduler must NOT spawn there or it leaks threads between tests and
        # exhausts PG connection slots (booking + subscription scheduler
        # post-mortem; feedback_ci_precommit_lessons).
        if not current_app.config.get("TESTING"):
            self._register_retention_job(current_app)

    def _register_extension_defaults(self) -> None:
        from plugins.meinchat.meinchat.extensibility import registry
        from plugins.meinchat.meinchat.extensibility.identity import (
            IDeviceDirectory,
            NullDeviceDirectory,
        )
        from plugins.meinchat.meinchat.extensibility.lifecycle import (
            AllowAllRoomPolicy,
            BlockListPolicy,
            IConversationCapabilities,
            IConversationPolicy,
            IRoomCapabilities,
            IRoomPolicy,
            PlainCapability,
            PlainRoomCapability,
        )
        from plugins.meinchat.meinchat.extensibility.pipeline import (
            IBodyCodec,
            IdentityBodyCodec,
        )
        from plugins.meinchat.meinchat.extensibility.cms_widget_reader import (
            ICmsWidgetReader,
            NullCmsWidgetReader,
        )

        registry.register(IBodyCodec, IdentityBodyCodec())
        registry.register(IConversationPolicy, BlockListPolicy())
        registry.register(IRoomPolicy, AllowAllRoomPolicy())
        registry.register(IConversationCapabilities, PlainCapability())
        registry.register(IRoomCapabilities, PlainRoomCapability())
        registry.register(IDeviceDirectory, NullDeviceDirectory())
        # S86.3 D2 — cms is a SOFT dependency: the null reader is the default
        # (cms absent/disabled → widget-start 404s cleanly). The cms-backed
        # adapter is registered last (resolve_first = last-write-wins) ONLY when
        # cms is importable, so meinchat enables + passes its gate with cms
        # absent and never hard-depends on it.
        registry.register(ICmsWidgetReader, NullCmsWidgetReader())
        self._register_cms_widget_reader()
        # S86.3 D11 (word-based) — the per-word charge runs as a post-send hook
        # so it can bill BOTH the guest's question and the bot's answer (the
        # answer's word count is only known once the bot replies).
        self._register_widget_charge_hook()

    def _register_cms_widget_reader(self) -> None:
        """Register the cms-backed widget reader ONLY when cms is importable.

        A guarded import keeps cms a SOFT dependency (D2): if the cms package is
        absent the null default (already registered) stands, and widget-start
        answers a clean 404 — meinchat still enables and passes its gate."""
        import logging

        try:
            import plugins.cms  # noqa: F401
        except ImportError:
            logging.getLogger(__name__).info(
                "[meinchat] cms not present — widget-start uses the null "
                "widget reader (widget endpoints answer 404)"
            )
            return
        from plugins.meinchat.meinchat.extensibility import registry
        from plugins.meinchat.meinchat.extensibility.cms_widget_reader import (
            CmsWidgetReader,
            ICmsWidgetReader,
        )
        from vbwd.extensions import db

        registry.register(ICmsWidgetReader, CmsWidgetReader(lambda: db.session))

    def _register_widget_charge_hook(self) -> None:
        """Register the word-based per-word charge as an ``IPostSendHook`` (D11).

        The hook bills the room's GUEST member ``word_count × cost_per_word`` for
        every message sent in a widget room — the guest's question AND the bot's
        answer. Collaborators are zero-/one-arg providers built off the current
        request session + config, so the hook (registered once here) stays a
        single instance while each call resolves fresh state. A no-op when the
        economy is off / there is no guest member / the room is not a widget."""
        from flask import current_app

        from plugins.meinchat.meinchat.extensibility import registry
        from plugins.meinchat.meinchat.extensibility.pipeline import IPostSendHook
        from plugins.meinchat.meinchat.repositories.room_repository import (
            RoomRepository,
        )
        from plugins.meinchat.meinchat.repositories.room_member_repository import (
            RoomMemberRepository,
        )
        from plugins.meinchat.meinchat.services.widget_room_meter import (
            WidgetRoomChargeHook,
        )
        from vbwd.extensions import db

        def economy_config() -> dict:
            config_store = getattr(current_app, "config_store", None)
            if config_store is None:
                return {}
            return config_store.get_config("meinchat") or {}

        def resolve_user_role(user_id):
            user = current_app.container.user_repository().find_by_id(user_id)
            return user.role if user is not None else None

        registry.register(
            IPostSendHook,
            WidgetRoomChargeHook(
                token_service_provider=lambda: current_app.container.token_service(),
                room_provider=lambda room_id: RoomRepository(db.session).find_by_id(
                    room_id
                ),
                members_provider=lambda room_id: RoomMemberRepository(
                    db.session
                ).list_for_room(room_id),
                resolve_user_role=resolve_user_role,
                # Persisted overrides win; absent keys fall back to the plugin's
                # DEFAULT_CONFIG (DRY) — NOT a local literal — so a fresh install
                # charges the designed per-word cost instead of 0.
                economy_enabled_provider=lambda: bool(
                    economy_config().get(
                        "guest_economy_enabled",
                        DEFAULT_CONFIG["guest_economy_enabled"],
                    )
                ),
                cost_per_word_provider=lambda: int(
                    economy_config().get(
                        "guest_token_cost_per_word",
                        DEFAULT_CONFIG["guest_token_cost_per_word"],
                    )
                ),
            ),
        )

    def _register_push_hook(self) -> None:
        from vbwd.services import push_dispatcher_registry
        from plugins.meinchat.meinchat.services.push_notification_hook import (
            build_push_hook,
        )

        push_dispatcher_registry.register_push_handler(
            "meinchat", build_push_hook("meinchat")
        )

    def _register_retention_job(self, app) -> None:
        from plugins.meinchat.meinchat.scheduler import start_retention_scheduler

        cron_expression = self.get_config(
            "retention_prune_cron", DEFAULT_CONFIG["retention_prune_cron"]
        )
        start_retention_scheduler(app, cron_expression=cron_expression)

    def register_event_handlers(self, event_bus) -> None:
        """S60 — bridge the CMS ``contact_form.received`` event into meinchat.

        Optional integration: meinchat declares no hard dependency on cms; it
        simply subscribes to the event cms (and the email plugin) already
        publish. When the payload's meinchat block is disabled/absent the
        handler is a no-op, so the contact form behaves exactly as before.
        """
        import logging

        logger = logging.getLogger(__name__)
        plugin = self

        def _on_contact_form_received(event_name, payload):
            """Per-event bridge: build session-bound services from the CURRENT
            request session and COMMIT.

            A boot-captured session is never committed by the contact POST's
            request lifecycle, so building the handler once at boot silently
            drops every delivery. Rebuilding per event off ``db.session`` (the
            request-scoped session) and committing here is what persists the
            message. Best-effort — never breaks the contact POST.
            """
            from vbwd.extensions import db
            from plugins.meinchat.meinchat.handlers.contact_form_handler import (
                ContactFormHandler,
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
            from plugins.meinchat.meinchat.services.bot_sender_provisioner import (
                BotSenderProvisioner,
            )
            from plugins.meinchat.meinchat.services.conversation_service import (
                ConversationService,
            )
            from plugins.meinchat.meinchat.services.message_service import (
                MessageService,
            )
            from plugins.meinchat.meinchat.services.nickname_service import (
                NicknameService,
            )
            from plugins.meinchat.meinchat.routes import _event_bus as _meinchat_sse_bus

            session = db.session
            try:
                nickname_repository = NicknameRepository(session)
                conversation_repository = ConversationRepository(session)
                message_repository = MessageRepository(session)
                ban_grace_days = plugin.get_config(
                    "nickname_ban_grace_period_days",
                    DEFAULT_CONFIG["nickname_ban_grace_period_days"],
                )
                nickname_service = NicknameService(
                    nickname_repository, ban_grace_period_days=ban_grace_days
                )
                conversation_service = ConversationService(conversation_repository)
                # Use meinchat's SSE/Redis bus (the one the SSE endpoint reads),
                # NOT the core event bus — otherwise send_text's "message" event
                # never reaches the recipient's live stream and the bot message
                # only shows on refresh (unlike real-user messages).
                message_service = MessageService(
                    conversation_repository,
                    message_repository,
                    nickname_repository,
                    event_bus=_meinchat_sse_bus(),
                )
                user_service = plugin._resolve_core_user_service()
                user_repository = plugin._resolve_core_user_repository(session)
                if user_service is None or user_repository is None:
                    logger.warning(
                        "[meinchat] core user service/repository unavailable — "
                        "contact-form delivery skipped"
                    )
                    return
                provisioner = BotSenderProvisioner(
                    user_service=user_service,
                    user_repository=user_repository,
                    nickname_service=nickname_service,
                    session=session,
                )
                handler = ContactFormHandler(
                    provisioner=provisioner,
                    user_repository=user_repository,
                    nickname_repository=nickname_repository,
                    conversation_service=conversation_service,
                    message_service=message_service,
                )
                handler.handle(event_name, payload)
                session.commit()
            except Exception as error:  # noqa: BLE001 — never break the contact POST
                logger.warning(
                    "[meinchat] contact-form delivery failed: %s", error, exc_info=True
                )
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001
                    pass

        event_bus.subscribe("contact_form.received", _on_contact_form_received)
        logger.info(
            "[meinchat] Subscribed to contact_form.received "
            "(bot-sender bridge, per-event session)"
        )

    @staticmethod
    def _resolve_core_user_service():
        container = getattr(current_app, "container", None)
        if container is None:
            return None
        try:
            return container.user_service()
        except Exception:  # noqa: BLE001 — optional bridge; degrade gracefully
            return None

    @staticmethod
    def _resolve_core_user_repository(session):
        from vbwd.repositories.user_repository import UserRepository

        return UserRepository(session)

    def on_disable(self) -> None:
        from flask import current_app

        from vbwd.plugins.di_helpers import unregister_repositories
        from vbwd.services import push_dispatcher_registry

        # S66 — remove the push hook so a disabled plugin never pushes.
        push_dispatcher_registry.unregister_push_handler("meinchat")

        # S38 — stop the per-worker SSE Redis listener thread, if one was
        # started, so disabling the plugin doesn't leak a background thread.
        bus = getattr(current_app, "_meinchat_event_bus", None)
        if bus is not None and hasattr(bus, "stop"):
            bus.stop()
        current_app._meinchat_event_bus = None  # type: ignore[attr-defined]

        container = getattr(current_app, "container", None)
        if container is not None:
            unregister_repositories(
                container,
                [
                    "meinchat_conversation_repository",
                    "meinchat_message_repository",
                    "meinchat_contact_repository",
                    "meinchat_nickname_repository",
                    "meinchat_room_repository",
                    "meinchat_room_member_repository",
                    "meinchat_guest_session_repository",
                    "meinchat_token_transfer_repository",
                ],
            )
