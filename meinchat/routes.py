"""Flat Blueprint for every meinchat API route.

All routes use absolute `/api/v1/…` paths. Kept flat (no nested
blueprints) so Flask-WTF's `csrf.exempt(bp)` applied in `vbwd/app.py`
actually exempts every endpoint below.
"""
import base64
import json
import logging
import os
from typing import Any

from flask import (
    Blueprint,
    Response,
    current_app,
    g,
    jsonify,
    request,
    stream_with_context,
)

from vbwd.extensions import db
from vbwd.models.enums import TokenTransactionType, UserRole
from vbwd.middleware.auth import require_admin, require_auth, require_permission
from vbwd.plugins.payment_route_helpers import check_plugin_enabled
from vbwd.repositories.user_repository import UserRepository

from vbwd.interfaces.file_storage import ManagerBackedFileStorage

from plugins.meinchat import DEFAULT_CONFIG

from plugins.meinchat.meinchat.repositories.contact_repository import (
    ContactRepository,
)
from plugins.meinchat.meinchat.repositories.attachment_repository import (
    AttachmentRepository,
)
from plugins.meinchat.meinchat.repositories.conversation_repository import (
    ConversationRepository,
)
from plugins.meinchat.meinchat.repositories.message_repository import (
    MessageRepository,
)
from plugins.meinchat.meinchat.repositories.token_transfer_repository import (
    TokenTransferRepository,
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
from plugins.meinchat.meinchat.services.attachment_service import (
    AttachmentService,
    AttachmentTooLargeError,
    AttachmentTypeNotAllowedError,
)
from plugins.meinchat.meinchat.services.contact_service import (
    ContactAlreadyExistsError,
    ContactNotFoundError,
    ContactSelfAddError,
    ContactService,
)
from plugins.meinchat.meinchat.services.conversation_service import (
    ConversationService,
    SelfConversationError,
)
from plugins.meinchat.meinchat.services.message_service import (
    AttachmentNotFoundError,
    ConversationNotFoundError,
    MessageBodyTooLongError,
    MessageNotFoundError,
    MessageService,
    NotAConversationMemberError,
    NotARoomMemberError,
    PlainAttachmentError,
    RoomNotFoundError,
)
from plugins.meinchat.meinchat.services.room_protocol import (
    PROTOCOL_PREFERENCE,
    RoomProtocolSelector,
)
from plugins.meinchat.meinchat.services.room_service import (
    NotARoomMemberError as RoomServiceNotMemberError,
    RoomNotFoundError as RoomServiceNotFoundError,
    RoomPermissionError,
    RoomService,
)
from plugins.meinchat.meinchat.services.nickname_service import (
    NicknameBannedError,
    NicknameNotFoundError,
    NicknameService,
    NicknameTakenError,
)
from plugins.meinchat.meinchat.services.token_transfer_service import (
    InsufficientTokensError,
    SelfTransferError,
    TokenTransferService,
)
from plugins.meinchat.meinchat.services.guest_session_service import (
    GuestSessionService,
)
from plugins.meinchat.meinchat.services.guest_token_admin_service import (
    GuestNotFoundError,
    GuestTokenAdminService,
)
from plugins.meinchat.meinchat.services.session_cleanup_service import (
    SessionCleanupService,
)
from plugins.meinchat.meinchat.services.widget_start_service import (
    DisplayNameRequiredError,
    NicknameRequiredError,
    PublicHumanMemberError,
    UnknownMemberError,
    WidgetAuthRequiredError,
    WidgetNotFoundError,
    WidgetStartService,
)
from plugins.meinchat.meinchat.services.widget_room_meter import (
    InsufficientGuestTokensError,
    WidgetRoomMeter,
)
from plugins.meinchat.meinchat.repositories.guest_session_repository import (
    GuestSessionRepository,
)
from plugins.meinchat.meinchat.extensibility import registry
from plugins.meinchat.meinchat.extensibility.cms_widget_reader import (
    ICmsWidgetReader,
    NullCmsWidgetReader,
)
from plugins.meinchat.meinchat.extensibility.errors import RoomPolicyError
from plugins.meinchat.meinchat.extensibility.identity import (
    IDeviceDirectory,
    NullDeviceDirectory,
)
from plugins.meinchat.meinchat.extensibility.lifecycle import (
    IConversationCapabilities,
    IRoomPolicy,
)
from plugins.meinchat.meinchat.extensibility.pipeline import IPostSendHook
from plugins.meinchat.meinchat.services.event_bus_factory import create_event_bus
from plugins.meinchat.meinchat.services.rate_limit_policy import RateLimitPolicy
from plugins.meinchat.meinchat.services.rate_limiter import (
    InMemoryCounterBackend,
    RateLimitExceeded,
    RateLimiter,
    RedisCounterBackend,
)
from plugins.meinchat.meinchat.services.slug_validator import NicknameInvalidError
from plugins.meinchat.meinchat.services.stream_token import (
    StreamTokenExpiredError,
    StreamTokenInvalidError,
    StreamTokenService,
)


meinchat_bp = Blueprint("meinchat", __name__)


def _nickname_service() -> NicknameService:
    config_store = getattr(current_app, "config_store", None)
    grace_days = 30
    if config_store is not None:
        cfg = config_store.get_config("meinchat") or {}
        grace_days = int(cfg.get("nickname_ban_grace_period_days", 30))
    return NicknameService(
        repo=NicknameRepository(db.session),
        ban_grace_period_days=grace_days,
    )


def _contact_service() -> ContactService:
    return ContactService(
        contact_repo=ContactRepository(db.session),
        nickname_repo=NicknameRepository(db.session),
    )


def _conversation_service() -> ConversationService:
    return ConversationService(repo=ConversationRepository(db.session))


def _meinchat_config() -> dict:
    config_store = getattr(current_app, "config_store", None)
    if config_store is None:
        return {}
    return config_store.get_config("meinchat") or {}


# The three guest token-economy knobs (D11). The persisted store holds only the
# admin's OVERRIDES (empty on a fresh install), so the economy reads must fall
# back to the plugin's DEFAULT_CONFIG — otherwise a fresh guest is granted 0
# tokens and the first send 402-gates the widget shut. DRY: the defaults come
# from DEFAULT_CONFIG, never hardcoded here.
_ECONOMY_CONFIG_KEYS = (
    "guest_economy_enabled",
    "guest_initial_tokens",
    "guest_token_cost_per_word",
    "guest_charge_bot_answers",
)


def _economy_config() -> dict:
    """Resolve the guest token-economy knobs: persisted overrides applied over
    the plugin's DEFAULT_CONFIG values. A persisted override wins; an absent key
    falls back to its designed default. Narrow on purpose — only the economy
    keys are defaulted here, leaving retention / rate-limit reads untouched."""
    persisted = _meinchat_config()
    resolved = {key: DEFAULT_CONFIG[key] for key in _ECONOMY_CONFIG_KEYS}
    for key in _ECONOMY_CONFIG_KEYS:
        if key in persisted:
            resolved[key] = persisted[key]
    return resolved


def _attachment_service() -> AttachmentService:
    """Cached on the Flask app so the underlying storage adapter is a
    single instance across the process."""
    cached = getattr(current_app, "_meinchat_attachment_service", None)
    if cached is not None:
        return cached
    cfg = _meinchat_config()
    storage = ManagerBackedFileStorage(current_app.container.filesystem_manager())
    svc = AttachmentService(
        storage=storage,
        max_bytes=int(cfg.get("attachment_max_bytes", 5 * 1024 * 1024)),
        max_dim_px=int(cfg.get("attachment_max_dimension_px", 2048)),
    )
    current_app._meinchat_attachment_service = svc  # type: ignore[attr-defined]
    return svc


def _rate_limiter() -> RateLimiter:
    cached = getattr(current_app, "_meinchat_rate_limiter", None)
    if cached is not None:
        return cached
    try:
        from vbwd.utils.redis_client import redis_client

        redis_client.client.ping()
        backend: Any = RedisCounterBackend(redis_client.client)
    except Exception:
        backend = InMemoryCounterBackend()
    rl = RateLimiter(backend)
    current_app._meinchat_rate_limiter = rl  # type: ignore[attr-defined]
    return rl


def _enforce_rate(category: str):
    """Per-request rate guard. Limits are config-driven (no literals at the
    call site) and platform-aware via the X-Client-Platform header — see
    plugins/meinchat/meinchat/services/rate_limit_policy.py for the
    resolution order.
    """
    platform = (request.headers.get("X-Client-Platform") or "web").lower()
    per_window, window_seconds = RateLimitPolicy(_meinchat_config()).limits_for(
        category, platform
    )
    try:
        _rate_limiter().check(
            category,
            user_id=g.user_id,
            limit=per_window,
            window_seconds=window_seconds,
        )
    except RateLimitExceeded as exc:
        # S33 — structured telemetry: one WARN line per meinchat 429 so a
        # "users hit the cap" report is answerable with grep instead of a
        # screenshot (category + user + retry-after).
        current_app.logger.warning(
            "429 category=%s user_id=%s retry_after_seconds=%d",
            category,
            g.user_id,
            exc.retry_after_seconds,
        )
        response = jsonify({"error": str(exc)})
        response.status_code = 429
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        response.headers["X-Rate-Limit-Category"] = category
        return response
    return None


def _event_bus():
    """Resolve the meinchat event bus once per worker (S38).

    Backend is chosen by the plugin config (`event_bus_backend`:
    auto|redis|memory) via `create_event_bus`, which logs the choice and — in
    `redis` mode — fails loud rather than silently degrading to the
    single-worker in-process bus. Cached on `current_app` for the worker's life.
    """
    cached = getattr(current_app, "_meinchat_event_bus", None)
    if cached is not None:
        return cached
    cfg = _meinchat_config()
    redis_handle = None
    try:
        from vbwd.utils.redis_client import redis_client

        redis_handle = redis_client.client
    except Exception:
        redis_handle = None
    bus = create_event_bus(
        cfg.get("event_bus_backend", "auto"),
        cfg.get("event_bus_channel_prefix", "meinchat:"),
        redis_client=redis_handle,
    )
    current_app._meinchat_event_bus = bus  # type: ignore[attr-defined]
    return bus


def _server_capabilities() -> set:
    """Union of every registered IConversationCapabilities impl, with the
    {"plain"} fallback if the registry is empty (test isolation safety)."""
    caps: set = set()
    for impl in registry.resolve_all(IConversationCapabilities):
        caps |= impl.for_conversation(None)
    return caps or {"plain"}


def _device_directory():
    """Registered device directory, falling back to the null directory."""
    try:
        return registry.resolve_first(IDeviceDirectory)
    except LookupError:
        return NullDeviceDirectory()


def _mark_e2e_delivered(messages, *, caller_user_id, device_id: str) -> None:
    """Fire `IPostSendHook.on_sent(row, fetched_by=<device>)` for every
    returned e2e row, so meinchat-plus records per-device delivery (and flips
    `delivered_to_all_addressed_devices_at` once every addressed device has
    fetched). `device_id` must be one of the CALLER's own active devices —
    a foreign / unknown id is ignored (no marking, no error). No-op when no
    hooks are registered (meinchat-alone). A throwing hook is logged, never
    propagated, and never fails the read."""
    own_devices = {
        str(device.id): device
        for device in _device_directory().lookup_active(caller_user_id)
    }
    device = own_devices.get(device_id)
    if device is None:
        return
    hooks = registry.resolve_all(IPostSendHook)
    if not hooks:
        return
    for row in messages:
        if getattr(row, "protocol", "plain") == "plain":
            continue
        for hook in hooks:
            try:
                hook.on_sent(row, fetched_by=device)
            except Exception as exc:  # delivery tracking must never fail a read
                current_app.logger.error(
                    "meinchat delivery hook %s failed on fetch: %s", hook, exc
                )


# Most-secure-first; negotiation picks the first mutually supported entry.
# Single source of truth shared with the room protocol selector (DRY): the 1:1
# negotiation and room selection rank protocols identically.
_PROTOCOL_PREFERENCE = PROTOCOL_PREFERENCE


class _NegotiationError(Exception):
    """Carries the S28.3a §5 negotiation-failure contract (status/code/hint)."""

    def __init__(self, status: int, code: str, hint: str) -> None:
        super().__init__(hint)
        self.status = status
        self.code = code
        self.hint = hint


def _negotiate_protocol(accepted_protocols, peer_user_id):
    """Return (chosen_protocol, capabilities) or raise _NegotiationError.

    `accepted_protocols` omitted → back-compat plain. Otherwise it must be a
    subset of the instance's enabled protocols and intersect the peer's
    usable set (e2e_v1 needs the peer to have a device key).
    """
    server = _server_capabilities()
    if accepted_protocols is None:
        return "plain", ["plain"]
    if not isinstance(accepted_protocols, list) or not accepted_protocols:
        raise _NegotiationError(
            400, "protocol_not_enabled", "accepted_protocols must be a list."
        )
    not_enabled = [p for p in accepted_protocols if p not in server]
    if not_enabled:
        raise _NegotiationError(
            400,
            "protocol_not_enabled",
            f"Protocol '{not_enabled[0]}' is not enabled on this instance.",
        )
    peer_caps = set(server)
    peer_has_devices = _device_directory().has_any(peer_user_id)
    if "e2e_v1" in peer_caps and not peer_has_devices:
        peer_caps.discard("e2e_v1")
    common = [
        p for p in _PROTOCOL_PREFERENCE if p in accepted_protocols and p in peer_caps
    ]
    if not common:
        if "e2e_v1" in accepted_protocols and not peer_has_devices:
            raise _NegotiationError(
                409,
                "peer_has_no_device_keys",
                "Ask the peer to enable secure chat on a device.",
            )
        raise _NegotiationError(
            409,
            "protocol_negotiation_empty",
            "No protocol accepted by both parties.",
        )
    chosen = common[0]
    return chosen, [chosen]


def _stream_token_service() -> StreamTokenService:
    cached = getattr(current_app, "_meinchat_stream_token_service", None)
    if cached is not None:
        return cached
    cfg = _meinchat_config()
    ttl_minutes = int(cfg.get("sse_stream_token_ttl_minutes", 60))
    svc = StreamTokenService(
        secret_key=current_app.config["JWT_SECRET_KEY"],
        ttl_seconds=ttl_minutes * 60,
    )
    current_app._meinchat_stream_token_service = svc  # type: ignore[attr-defined]
    return svc


def _message_service() -> MessageService:
    return MessageService(
        conv_repo=ConversationRepository(db.session),
        message_repo=MessageRepository(db.session),
        nickname_repo=NicknameRepository(db.session),
        attachment_service=_attachment_service(),
        event_bus=_event_bus(),
        attachment_repo=AttachmentRepository(db.session),
        room_repo=RoomRepository(db.session),
        member_repo=RoomMemberRepository(db.session),
    )


def _resolve_user_role(user_id):
    """Map a user id to its core ``UserRole`` for the room protocol selector.

    Kept as a narrow lookup (not a core-user import in the service) so the room
    selector depends only on a callable (D — dependency inversion). Returns
    ``None`` for an unknown id."""
    user = current_app.container.user_repository().find_by_id(user_id)
    return user.role if user is not None else None


def _room_service() -> RoomService:
    """RoomService wired to the SAME protocol seams the 1:1 path uses: the
    registered capability union (`_server_capabilities`) and the device
    directory's per-user key predicate (`_device_directory().has_any`)."""
    selector = RoomProtocolSelector(
        server_capabilities_provider=_server_capabilities,
        device_has_keys=lambda user_id: _device_directory().has_any(user_id),
    )
    return RoomService(
        room_repo=RoomRepository(db.session),
        member_repo=RoomMemberRepository(db.session),
        protocol_selector=selector,
        role_resolver=_resolve_user_role,
    )


def _enforce_room_policies(creator_id, member_ids, accepted_protocols):
    """Run every registered `IRoomPolicy` against the prospective room. Returns
    a 409 JSON response on the first veto (e.g. an e2e-required room with a
    keyless member), or ``None`` when all policies allow.

    This is the room generalisation of the 1:1 `_negotiate_protocol` veto: the
    selector still pins `plain` when plain is an acceptable fallback, but an
    *e2e-required* room is vetoed instead of silently downgraded."""
    all_member_ids = [creator_id, *[uid for uid in member_ids if uid != creator_id]]
    member_roles = {uid: _resolve_user_role(uid) for uid in all_member_ids}
    nickname_repo = NicknameRepository(db.session)

    def nickname_of(user_id):
        row = nickname_repo.find_by_user_id(user_id)
        return row.nickname if row is not None else None

    for policy in registry.resolve_all(IRoomPolicy):
        try:
            policy.may_start_room(
                all_member_ids,
                member_roles,
                accepted_protocols,
                nickname_of=nickname_of,
            )
        except RoomPolicyError as exc:
            return (
                jsonify({"error": exc.hint, "code": exc.code, "hint": exc.hint}),
                409,
            )
    return None


def _widget_reader() -> ICmsWidgetReader:
    """The registered cms widget reader, falling back to the null reader so
    widget-start answers a clean 404 when cms is absent (D2, Liskov)."""
    try:
        return registry.resolve_first(ICmsWidgetReader)
    except LookupError:
        return NullCmsWidgetReader()


def _guest_session_service() -> GuestSessionService:
    cfg = _meinchat_config()
    return GuestSessionService(
        user_service=current_app.container.user_service(),
        nickname_service=_nickname_service(),
        auth_service=current_app.container.auth_service(),
        session=db.session,
        token_ttl_hours=float(cfg.get("widget_guest_token_ttl_hours", 24)),
    )


def _widget_start_service() -> WidgetStartService:
    """Wire WidgetStartService to the same nickname/role seams the room routes
    use. Member resolution mirrors `start_conversation` (unknown / banned /
    search-hidden → unknown member)."""
    nickname_repo = NicknameRepository(db.session)

    def resolve_nickname_to_user_id(nickname):
        if not isinstance(nickname, str) or not nickname.strip():
            return None
        row = nickname_repo.find_by_nickname_ci(nickname.strip())
        if row is None or row.banned or row.search_hidden:
            return None
        return row.user_id

    def resolve_user_nickname(user_id):
        row = nickname_repo.find_by_user_id(user_id)
        return row.nickname if row is not None else None

    cfg = _meinchat_config()
    economy = _economy_config()
    economy_enabled = bool(economy["guest_economy_enabled"])
    initial_tokens = int(economy["guest_initial_tokens"])

    def grant_initial_tokens(guest_user_id):
        # D11 — credit the guest's initial budget through the CORE TokenService
        # (the single source of truth for balances); meinchat owns no ledger.
        current_app.container.token_service().credit_tokens(
            guest_user_id,
            initial_tokens,
            TokenTransactionType.BONUS,
            description="meinchat widget guest initial grant",
        )

    guest_token_ttl_hours = float(cfg.get("widget_guest_token_ttl_hours", 24))

    def mint_guest_access_token(guest_user_id):
        # D12 — re-mint a short-TTL access token for a RETURNING guest so the FE
        # keeps driving the guest's room as the guest. Mints a token only (no
        # tokens granted), reusing the single core mint path (DRY). Returns None
        # if the guest can no longer be resolved → FE self-heals with a fresh
        # start.
        user = UserRepository(db.session).find_by_id(guest_user_id)
        if user is None:
            return None
        return current_app.container.auth_service().generate_access_token(
            guest_user_id,
            user.email,
            expiration_hours=guest_token_ttl_hours,
        )

    return WidgetStartService(
        widget_reader=_widget_reader(),
        resolve_nickname_to_user_id=resolve_nickname_to_user_id,
        resolve_user_role=_resolve_user_role,
        resolve_user_nickname=resolve_user_nickname,
        room_service=_room_service(),
        guest_session_service=_guest_session_service(),
        guest_session_repo=GuestSessionRepository(db.session),
        guest_session_ttl_hours=guest_token_ttl_hours,
        grant_initial_tokens=grant_initial_tokens,
        guest_initial_tokens=initial_tokens,
        economy_enabled=economy_enabled,
        mint_guest_access_token=mint_guest_access_token,
    )


def _widget_room_meter() -> WidgetRoomMeter:
    """The pre-send balance gate for guest sends in widget rooms (D11). Resolves
    balances through the core TokenService (no meinchat-owned ledger). The
    per-word charge itself lives in the registered ``WidgetRoomChargeHook``."""
    return WidgetRoomMeter(
        token_service=current_app.container.token_service(),
        resolve_user_role=_resolve_user_role,
        economy_enabled=bool(_economy_config()["guest_economy_enabled"]),
    )


def _is_metered_guest_send(room, sender_user_id) -> bool:
    """True when this room send should surface the guest's post-charge balance:
    economy on, a widget room, sender is the room's GUEST member (D11)."""
    if not bool(_economy_config()["guest_economy_enabled"]):
        return False
    if room is None or not getattr(room, "widget_slug", None):
        return False
    return _resolve_user_role(sender_user_id) == UserRole.GUEST


def _guest_token_balance(user_id) -> int:
    """The guest's remaining token balance for the FE to render the buy block."""
    return current_app.container.token_service().get_balance(user_id)


def _guest_token_admin_service() -> GuestTokenAdminService:
    """Admin top-up / reset of an existing widget guest's core token balance.

    Balances route through the core TokenService (single source of truth);
    guests are read from meinchat's GuestSessionRepository and enriched with the
    nickname directory. Mirrors the other request-scoped factories (db.session)."""
    return GuestTokenAdminService(
        token_service=current_app.container.token_service(),
        guest_session_repo=GuestSessionRepository(db.session),
        resolve_user_role=_resolve_user_role,
        nickname_repo=NicknameRepository(db.session),
    )


def _reset_token_balance(user_id, target: int) -> int:
    """Set ``user_id``'s core balance to ``target``, returning the new balance.

    Reuses ``GuestTokenAdminService.reset`` (the single balance-reset path) for
    an existing widget guest; when that path 404s because the target is a
    REGISTERED user (not a guest), the balance is set directly via the core
    TokenService by crediting / debiting the signed delta — so the cleanup
    works for guests and registered users alike (Liskov: same observable
    'balance is now target' contract for both)."""
    try:
        return _guest_token_admin_service().reset(user_id, target)
    except GuestNotFoundError:
        token_service = current_app.container.token_service()
        current_balance = token_service.get_balance(user_id)
        delta = target - current_balance
        if delta > 0:
            token_service.credit_tokens(
                user_id,
                delta,
                transaction_type=TokenTransactionType.ADJUSTMENT,
                description="meinchat admin session-cleanup balance reset",
            )
        elif delta < 0:
            token_service.debit_tokens(
                user_id,
                -delta,
                transaction_type=TokenTransactionType.ADJUSTMENT,
                description="meinchat admin session-cleanup balance reset",
            )
        return token_service.get_balance(user_id)


def _session_cleanup_service() -> SessionCleanupService:
    """Admin cleanup of meinchat chat + guest session data. Deletes route
    through db.session (relying on the FK cascades); the balance reset routes
    through the core TokenService (single source of truth)."""
    return SessionCleanupService(
        session=db.session,
        token_service=current_app.container.token_service(),
        guest_session_repo=GuestSessionRepository(db.session),
        conversation_repo=ConversationRepository(db.session),
        room_repo=RoomRepository(db.session),
        guest_initial_tokens=int(_economy_config()["guest_initial_tokens"]),
        reset_balance=_reset_token_balance,
    )


def _resolve_optional_caller_id():
    """Return the caller's user id from a valid bearer token, or None.

    The widget-start endpoint is NOT `@require_auth` (a public widget allows an
    anonymous visitor). For a `logged_in` widget we still need the caller, so we
    verify the bearer the same way `require_auth` does — without rejecting when
    it is absent."""
    auth_header = request.headers.get("Authorization") or ""
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    from vbwd.services.auth_service import AuthService

    user_repo = UserRepository(db.session)
    user_id = AuthService(user_repository=user_repo).verify_token(parts[1])
    if user_id is None:
        return None
    user = user_repo.find_by_id(user_id)
    if user is None or user.status.value != "ACTIVE":
        return None
    return user_id


def _enforce_widget_guest_start_rate():
    """IP-keyed rate guard for the anonymous public widget-start path. Returns a
    429 response when over quota, else None."""
    platform = (request.headers.get("X-Client-Platform") or "web").lower()
    per_window, window_seconds = RateLimitPolicy(_meinchat_config()).limits_for(
        "widget_guest_start", platform
    )
    client_ip = request.remote_addr or "unknown"
    try:
        _rate_limiter().check(
            "widget_guest_start",
            user_id=f"ip:{client_ip}",
            limit=per_window,
            window_seconds=window_seconds,
        )
    except RateLimitExceeded as exc:
        current_app.logger.warning(
            "429 category=widget_guest_start client_ip=%s retry_after_seconds=%d",
            client_ip,
            exc.retry_after_seconds,
        )
        response = jsonify({"error": str(exc)})
        response.status_code = 429
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        response.headers["X-Rate-Limit-Category"] = "widget_guest_start"
        return response
    return None


_FINGERPRINT_LOGGER_NAME = "meinchat.widget_guest_fingerprint"
_FINGERPRINT_LOG_FILENAME = "widget_guest_fingerprint.log"


def _fingerprint_logger() -> logging.Logger:
    """A dedicated module logger with a file handler for the D12 fingerprint
    candidate log. Configured once per process; the file lives under the app's
    var/log dir (``VBWD_LOG_DIR``), falling back to ``var/log`` under the cwd.
    Log-only — never a DB row, never enforcement."""
    logger = logging.getLogger(_FINGERPRINT_LOGGER_NAME)
    if getattr(logger, "_meinchat_fingerprint_configured", False):
        return logger
    log_dir = os.environ.get("VBWD_LOG_DIR") or os.path.join(os.getcwd(), "var", "log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(
            os.path.join(log_dir, _FINGERPRINT_LOG_FILENAME)
        )
    except OSError:
        # A read-only/missing dir must never break widget-start; degrade to the
        # app logger so the signals are still captured somewhere.
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger._meinchat_fingerprint_configured = True  # type: ignore[attr-defined]
    return logger


def _log_widget_start_fingerprint(widget_slug: str) -> None:
    """Append the server-visible candidate identification signals (D12).

    Best-effort: a logging failure must never break the public start."""
    try:
        from vbwd.middleware.api_key_auth import _client_ip

        _fingerprint_logger().info(
            "widget_start slug=%s ip=%s ua=%r accept_language=%r",
            widget_slug,
            _client_ip(),
            request.headers.get("User-Agent", ""),
            request.headers.get("Accept-Language", ""),
        )
    except Exception:  # noqa: BLE001 — forensic log is never load-bearing
        current_app.logger.debug("widget-start fingerprint log failed", exc_info=True)


def _token_transfer_service() -> TokenTransferService:
    """Pulls the core `TokenService` from the app's DI container so the
    transfer plugin doesn't know how token balances are persisted."""
    container = current_app.container
    token_service = container.token_service()
    return TokenTransferService(
        transfer_repo=TokenTransferRepository(db.session),
        token_service=token_service,
        nickname_repo=NicknameRepository(db.session),
        conversation_service=_conversation_service(),
        message_service=_message_service(),
    )


def _serialize_conversation_for_user(conv, user_id) -> dict:
    """Attach the fields the fe-user inbox row needs (peer nickname,
    caller-specific unread count)."""
    nickname_repo = NicknameRepository(db.session)
    peer_id = ConversationService.peer_of(user_id, conv)
    peer_nickname = nickname_repo.find_by_user_id(peer_id)
    return {
        "id": str(conv.id),
        "peer_user_id": str(peer_id),
        "peer_nickname": peer_nickname.nickname if peer_nickname else None,
        "last_message_at": (
            conv.last_message_at.isoformat() if conv.last_message_at else None
        ),
        "last_message_preview": conv.last_message_preview,
        "unread_count": ConversationService.unread_for(user_id, conv),
        # S28.3b — the pinned protocol lets the client route e2e_v1
        # conversations through the meinchat-plus crypto provider.
        "protocol": conv.protocol,
    }


def _member_unread(member) -> int:
    return member.unread_count if member is not None else 0


def _serialize_room_for_user(room, user_id, members=None) -> dict:
    """Inbox/detail DTO for a room — mirrors `_serialize_conversation_for_user`.

    Carries the room identity + pinned protocol/capabilities, the member roster
    (id + role + nickname), and the CALLER's own unread/last-read fields."""
    if members is None:
        members = RoomMemberRepository(db.session).list_for_room(room.id)
    nickname_repo = NicknameRepository(db.session)
    caller_member = next((m for m in members if m.user_id == user_id), None)
    member_dtos = []
    for member in members:
        nickname_row = nickname_repo.find_by_user_id(member.user_id)
        member_dtos.append(
            {
                "user_id": str(member.user_id),
                "role": member.role,
                "nickname": nickname_row.nickname if nickname_row else None,
            }
        )
    return {
        "id": str(room.id),
        "name": room.name,
        "protocol": room.protocol,
        "capabilities": room.capabilities or [],
        "members": member_dtos,
        "member_count": len(member_dtos),
        "last_message_at": (
            room.last_message_at.isoformat() if room.last_message_at else None
        ),
        "last_message_preview": room.last_message_preview,
        "unread_count": _member_unread(caller_member),
        "last_read_at": (
            caller_member.last_read_at.isoformat()
            if caller_member is not None and caller_member.last_read_at
            else None
        ),
    }


# ── /api/v1/nickname/* ──────────────────────────────────────────────────────


@meinchat_bp.route("/api/v1/nickname/me", methods=["GET"])
@require_auth
def get_my_nickname():
    row = _nickname_service().get_mine(g.user_id)
    return jsonify({"nickname": row.nickname if row else None}), 200


@meinchat_bp.route("/api/v1/nickname/me", methods=["PUT"])
@require_auth
def put_my_nickname():
    data = request.get_json(silent=True) or {}
    nickname = data.get("nickname")
    if not isinstance(nickname, str):
        return jsonify({"error": "nickname is required"}), 400
    try:
        row = _nickname_service().set_nickname(g.user_id, nickname.strip().lower())
        db.session.commit()
        return jsonify(row.to_dict()), 200
    except NicknameInvalidError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except NicknameBannedError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 409
    except NicknameTakenError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 409


@meinchat_bp.route("/api/v1/nickname/search", methods=["GET"])
@require_auth
def search_nicknames():
    blocked = _enforce_rate("nickname_search")
    if blocked is not None:
        return blocked
    prefix = (request.args.get("q") or "").strip().lower()
    if len(prefix) < 1:
        return jsonify({"items": []}), 200
    rows = _nickname_service().search(prefix, caller_user_id=g.user_id, limit=10)
    return (
        jsonify(
            {
                "items": [
                    {"nickname": r.nickname, "user_id": str(r.user_id)} for r in rows
                ]
            }
        ),
        200,
    )


@meinchat_bp.route("/api/v1/nickname/<nickname>/card", methods=["GET"])
@require_auth
def get_nickname_card(nickname: str):
    try:
        card = _nickname_service().get_card(nickname)
        return jsonify(card), 200
    except NicknameNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


# ── /api/v1/contacts/* ──────────────────────────────────────────────────────


@meinchat_bp.route("/api/v1/contacts", methods=["GET"])
@require_auth
def list_contacts():
    rows = _contact_service().list_contacts(g.user_id)
    # The fe-user sidebar needs the peer's nickname per row. One lookup
    # per row is fine for typical <1000-row personal address books; if
    # the list ever grows, batch via an IN() query.
    nickname_repo = NicknameRepository(db.session)
    items = []
    for row in rows:
        peer = nickname_repo.find_by_user_id(row.contact_user_id)
        dto = row.to_dict()
        dto["peer_nickname"] = peer.nickname if peer else None
        items.append(dto)
    return jsonify({"items": items}), 200


@meinchat_bp.route("/api/v1/contacts", methods=["POST"])
@require_auth
def add_contact():
    data = request.get_json(silent=True) or {}
    nickname = data.get("nickname")
    if not isinstance(nickname, str) or not nickname.strip():
        return jsonify({"error": "nickname is required"}), 400
    try:
        row = _contact_service().add_contact(
            g.user_id,
            nickname=nickname.strip().lower(),
            alias=data.get("alias"),
            note=data.get("note"),
            pinned=bool(data.get("pinned", False)),
        )
        db.session.commit()
        return jsonify(row.to_dict()), 201
    except NicknameNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404
    except ContactSelfAddError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except ContactAlreadyExistsError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 409


@meinchat_bp.route("/api/v1/contacts/<contact_id>", methods=["PATCH"])
@require_auth
def update_contact(contact_id: str):
    data = request.get_json(silent=True) or {}
    try:
        row = _contact_service().update_contact(
            g.user_id,
            contact_id,
            alias=data.get("alias"),
            note=data.get("note"),
            pinned=data.get("pinned") if "pinned" in data else None,
        )
        db.session.commit()
        return jsonify(row.to_dict()), 200
    except ContactNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404


@meinchat_bp.route("/api/v1/contacts/<contact_id>", methods=["DELETE"])
@require_auth
def remove_contact(contact_id: str):
    try:
        _contact_service().remove_contact(g.user_id, contact_id)
        db.session.commit()
        return "", 204
    except ContactNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404


# ── /api/v1/messaging/* ─────────────────────────────────────────────────────


@meinchat_bp.route("/api/v1/messaging/limits", methods=["GET"])
@require_auth
def get_limits():
    """Operator-tunable retention/size knobs, read on client cold start.

    Carries operator knobs ONLY — the capability surface
    (`enabled_protocols`) is a separate concern living on
    `/messaging/capabilities` from S28.3a (one endpoint, one concern; DRY).
    Returns the standard `Plugin not enabled` 404 envelope when meinchat is
    disabled per-instance.
    """
    _config, error_response = check_plugin_enabled("meinchat")
    if error_response is not None:
        return error_response
    config = _meinchat_config()
    # Read each knob defensively: DEFAULT_CONFIG (merged at initialize()) is the
    # source of truth, but a runtime config dict from the store can drift (e.g.
    # an instance whose stored config pre-dates S28). Falling back to the
    # documented defaults keeps this endpoint from 500-ing on config drift.
    return (
        jsonify(
            {
                "messages_retention_days_server": int(
                    config.get("messages_retention_days_server", 2)
                ),
                "messages_retention_days_client_suggested": int(
                    config.get("messages_retention_days_client_suggested", 10)
                ),
                "attachments_retention_days_server": int(
                    config.get("attachments_retention_days_server", 2)
                ),
                "ciphertext_max_bytes": int(config.get("ciphertext_max_bytes", 16384)),
            }
        ),
        200,
    )


@meinchat_bp.route("/api/v1/messaging/capabilities", methods=["GET"])
@require_auth
def get_capabilities():
    """Protocol capability discovery (S28.3a §5).

    `{"server": [...]}` — union of all registered capability impls.
    With `?me=true`, also `{"me": [...]}` — the caller's usable subset
    (e2e_v1 requires the caller to have at least one registered device key).
    """
    _config, error_response = check_plugin_enabled("meinchat")
    if error_response is not None:
        return error_response
    server = sorted(_server_capabilities())
    result = {"server": server}
    if request.args.get("me") == "true":
        usable = set(server)
        if "e2e_v1" in usable and not _device_directory().has_any(g.user_id):
            usable.discard("e2e_v1")
        result["me"] = sorted(usable)
    return jsonify(result), 200


@meinchat_bp.route("/api/v1/messaging/conversations", methods=["GET"])
@require_auth
def list_conversations():
    rows = _conversation_service().list_for_user(g.user_id)
    return (
        jsonify(
            {"items": [_serialize_conversation_for_user(c, g.user_id) for c in rows]}
        ),
        200,
    )


@meinchat_bp.route("/api/v1/messaging/conversations", methods=["POST"])
@require_auth
def start_conversation():
    """Body: {peer_nickname}. Returns the existing conversation or a new one.

    Lookup-first: opening an already-existing chat is free (no rate-limit
    counter touched). Only the actual creation of a new conversation row
    counts against the new_conversation quota.
    """
    data = request.get_json(silent=True) or {}
    peer_nickname = data.get("peer_nickname")
    if not isinstance(peer_nickname, str) or not peer_nickname.strip():
        return jsonify({"error": "peer_nickname is required"}), 400

    target = NicknameRepository(db.session).find_by_nickname_ci(peer_nickname.strip())
    if target is None or target.banned or target.search_hidden:
        return jsonify({"error": f"'{peer_nickname}' not found"}), 404

    conversation_service = _conversation_service()
    try:
        existing = conversation_service.find_between(g.user_id, target.user_id)
    except SelfConversationError as exc:
        return jsonify({"error": str(exc)}), 400
    if existing is not None:
        # Protocol is pinned at creation (immutable) — return as-is.
        response = jsonify(_serialize_conversation_for_user(existing, g.user_id))
        response.headers["X-Conversation-Existed"] = "true"
        return response, 200

    # Negotiate the protocol BEFORE spending the new-conversation quota so a
    # failed negotiation doesn't burn the caller's rate-limit budget.
    try:
        chosen_protocol, chosen_caps = _negotiate_protocol(
            data.get("accepted_protocols"), target.user_id
        )
    except _NegotiationError as exc:
        return (
            jsonify({"error": exc.hint, "code": exc.code, "hint": exc.hint}),
            exc.status,
        )

    blocked = _enforce_rate("new_conversation")
    if blocked is not None:
        return blocked

    try:
        conv = conversation_service.start_or_get(g.user_id, target.user_id)
        conv.protocol = chosen_protocol
        conv.capabilities = chosen_caps
        db.session.commit()
        return jsonify(_serialize_conversation_for_user(conv, g.user_id)), 200
    except SelfConversationError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400


@meinchat_bp.route(
    "/api/v1/messaging/conversations/<conv_id>/messages", methods=["GET"]
)
@require_auth
def list_messages(conv_id: str):
    before = request.args.get("before")
    limit = min(int(request.args.get("limit", 50)), 200)
    # Optional: the fetching device id (e2e clients pass their own device so
    # the server can record delivery). Query param or header, caller-owned.
    fetching_device_id = request.args.get("device_id") or request.headers.get(
        "X-Device-Id"
    )
    try:
        msgs = _message_service().list_messages(
            conv_id, caller_user_id=g.user_id, before=before, limit=limit
        )
        if fetching_device_id:
            _mark_e2e_delivered(
                msgs, caller_user_id=g.user_id, device_id=fetching_device_id
            )
            db.session.commit()
        return jsonify({"items": [m.to_dict() for m in msgs]}), 200
    except ConversationNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except NotAConversationMemberError as exc:
        return jsonify({"error": str(exc)}), 403


@meinchat_bp.route(
    "/api/v1/messaging/conversations/<conv_id>/messages", methods=["POST"]
)
@require_auth
def send_message(conv_id: str):
    blocked = _enforce_rate("message_send")
    if blocked is not None:
        return blocked
    data = request.get_json(silent=True) or {}

    # Protocol is pinned on the conversation at creation. For an e2e_v1
    # conversation the client posts an opaque base64 `envelope_b64` (the
    # server never sees plaintext); plain conversations post `body`.
    conv = ConversationRepository(db.session).find_by_id(conv_id)
    protocol = conv.protocol if conv is not None else "plain"
    if protocol != "plain":
        envelope_b64 = data.get("envelope_b64")
        if not isinstance(envelope_b64, str):
            return (
                jsonify({"error": "envelope_b64 is required for this conversation"}),
                400,
            )
        try:
            send_body: Any = base64.b64decode(envelope_b64, validate=True)
        except (ValueError, TypeError):
            return jsonify({"error": "envelope_b64 must be valid base64"}), 400
        # e2e (`envelope`) rows never carry structured `meta` — rich choices
        # ride only the plain path (S70.0).
        meta = None
    else:
        send_body = data.get("body", "")
        # Optional S70.0 structured/interactive content (bot choice cards / a
        # tapped card's action). Validated + size-capped in the service; absent
        # `meta` is the unchanged legacy path.
        meta = data.get("meta")

    try:
        msg = _message_service().send_text(
            conv_id,
            sender_user_id=g.user_id,
            body=send_body,
            protocol_hint=protocol,
            meta=meta,
        )
        db.session.commit()
        return jsonify(msg.to_dict()), 201
    except ConversationNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404
    except NotAConversationMemberError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 403
    except MessageBodyTooLongError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400


@meinchat_bp.route(
    "/api/v1/messaging/conversations/<conv_id>/messages/attachment",
    methods=["POST"],
)
@require_auth
def send_attachment_message(conv_id: str):
    """Upload a single image (multipart/form-data field 'file').

    Optional 'body' form field carries a short caption (≤ 4000 chars).
    """
    blocked = _enforce_rate("attachment_send")
    if blocked is not None:
        return blocked

    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "file is required"}), 400
    raw = upload.read()

    body = request.form.get("body", "")

    try:
        msg = _message_service().send_attachment(
            conv_id,
            sender_user_id=g.user_id,
            raw_image_bytes=raw,
            body=body,
        )
        db.session.commit()
        return jsonify(msg.to_dict()), 201
    except ConversationNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404
    except NotAConversationMemberError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 403
    except AttachmentTooLargeError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 413
    except AttachmentTypeNotAllowedError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 415
    except MessageBodyTooLongError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400


@meinchat_bp.route("/api/v1/messaging/messages/<msg_id>/attachments", methods=["POST"])
@require_auth
def upload_e2e_attachment(msg_id: str):
    """Attach a client-encrypted blob to an existing e2e message (S28.4).

    JSON body: `{kind, ciphertext_b64, envelope_header, mime}`. The server
    stores the opaque ciphertext and records the per-recipient key envelope;
    it never decodes or resizes. Only the message's sender may attach.
    """
    blocked = _enforce_rate("attachment_send")
    if blocked is not None:
        return blocked
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    mime = data.get("mime")
    envelope_header = data.get("envelope_header")
    ciphertext_b64 = data.get("ciphertext_b64")
    if not isinstance(ciphertext_b64, str):
        return jsonify({"error": "ciphertext_b64 is required"}), 400
    if not isinstance(envelope_header, dict) or not envelope_header:
        return jsonify({"error": "envelope_header is required"}), 400
    if not isinstance(mime, str) or not mime:
        return jsonify({"error": "mime is required"}), 400
    try:
        ciphertext = base64.b64decode(ciphertext_b64, validate=True)
    except (ValueError, TypeError):
        return jsonify({"error": "ciphertext_b64 must be valid base64"}), 400
    try:
        row = _message_service().add_e2e_attachment(
            msg_id,
            caller_user_id=g.user_id,
            kind=kind,
            ciphertext=ciphertext,
            envelope_header=envelope_header,
            mime=mime,
        )
        db.session.commit()
        return jsonify(row.to_dict()), 201
    except MessageNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404
    except PlainAttachmentError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except AttachmentTooLargeError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 413
    except AttachmentTypeNotAllowedError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400


@meinchat_bp.route("/api/v1/messaging/attachments/<attachment_id>", methods=["GET"])
@require_auth
def download_attachment(attachment_id: str):
    """Return the raw stored bytes (opaque ciphertext for e2e attachments —
    the client decrypts). Caller must be a participant of the conversation."""
    try:
        blob, mime = _message_service().get_attachment_blob(
            attachment_id, caller_user_id=g.user_id
        )
    except AttachmentNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    return Response(
        blob,
        mimetype="application/octet-stream",
        headers={
            "X-Attachment-Mime": mime,
        },
    )


@meinchat_bp.route("/api/v1/messaging/conversations/<conv_id>/read", methods=["POST"])
@require_auth
def mark_conversation_read(conv_id: str):
    try:
        # S68 — the iOS client appends ?device_token=<hex> so the badge-only
        # push fired by mark_read suppresses the device that just read. Web
        # clients omit it (no APNs token) → no suppression (correct).
        _message_service().mark_read(
            conv_id,
            reader_user_id=g.user_id,
            originating_device_token=request.args.get("device_token"),
        )
        db.session.commit()
        return "", 204
    except ConversationNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404
    except NotAConversationMemberError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 403


@meinchat_bp.route(
    "/api/v1/messaging/conversations/<conv_id>/messages/<msg_id>",
    methods=["DELETE"],
)
@require_auth
def delete_message(conv_id: str, msg_id: str):
    try:
        _message_service().delete_message(msg_id, caller_user_id=g.user_id)
        db.session.commit()
        return "", 204
    except MessageNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404


# ── /api/v1/messaging/rooms/* (S86.1 D5) ────────────────────────────────────


def _resolve_nicknames_or_404(nicknames):
    """Resolve each nickname to a user id, matching the 1:1 `start_conversation`
    rule (unknown / banned / search-hidden → not found). Returns
    (resolved_ids, error_response). On success `error_response` is None."""
    nickname_repo = NicknameRepository(db.session)
    resolved = []
    for nickname in nicknames:
        if not isinstance(nickname, str) or not nickname.strip():
            return None, (jsonify({"error": "nickname is required"}), 400)
        row = nickname_repo.find_by_nickname_ci(nickname.strip())
        if row is None or row.banned or row.search_hidden:
            return None, (jsonify({"error": f"'{nickname}' not found"}), 404)
        resolved.append(row.user_id)
    return resolved, None


@meinchat_bp.route("/api/v1/messaging/rooms", methods=["POST"])
@require_auth
def create_room():
    """Body: {member_nicknames: [...], name?, accepted_protocols?}. Creates a
    room with the caller as admin and each resolved nickname as a member.
    Reuses the `new_conversation` rate-limit quota (rooms are a conversation
    variant — one abuse budget, no new knob)."""
    data = request.get_json(silent=True) or {}
    member_nicknames = data.get("member_nicknames")
    if not isinstance(member_nicknames, list):
        return jsonify({"error": "member_nicknames must be a list"}), 400

    resolved_ids, error = _resolve_nicknames_or_404(member_nicknames)
    if error is not None:
        return error

    accepted_protocols = data.get("accepted_protocols")
    # Run the room-start policies BEFORE spending the rate-limit budget so a
    # failed veto (e.g. an e2e-required room with a keyless member) doesn't
    # burn the caller's quota — mirroring the 1:1 `_negotiate_protocol` order.
    veto = _enforce_room_policies(g.user_id, resolved_ids, accepted_protocols)
    if veto is not None:
        return veto

    blocked = _enforce_rate("new_conversation")
    if blocked is not None:
        return blocked

    room = _room_service().create_room(
        g.user_id,
        resolved_ids,
        name=data.get("name"),
        accepted_protocols=accepted_protocols,
    )
    db.session.commit()
    return jsonify(_serialize_room_for_user(room, g.user_id)), 201


@meinchat_bp.route("/api/v1/messaging/rooms", methods=["GET"])
@require_auth
def list_rooms():
    rooms = _room_service().list_for_user(g.user_id)
    return (
        jsonify({"items": [_serialize_room_for_user(r, g.user_id) for r in rooms]}),
        200,
    )


@meinchat_bp.route("/api/v1/messaging/rooms/<room_id>", methods=["GET"])
@require_auth
def get_room(room_id: str):
    try:
        room = _room_service().get_for_member(room_id, g.user_id)
    except RoomServiceNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except RoomServiceNotMemberError as exc:
        return jsonify({"error": str(exc)}), 403
    return jsonify(_serialize_room_for_user(room, g.user_id)), 200


@meinchat_bp.route("/api/v1/messaging/rooms/<room_id>/members", methods=["GET"])
@require_auth
def list_room_members(room_id: str):
    service = _room_service()
    try:
        service.get_for_member(room_id, g.user_id)
    except RoomServiceNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except RoomServiceNotMemberError as exc:
        return jsonify({"error": str(exc)}), 403
    nickname_repo = NicknameRepository(db.session)
    items = []
    for member in service.members(room_id):
        nickname_row = nickname_repo.find_by_user_id(member.user_id)
        dto = member.to_dict()
        dto["nickname"] = nickname_row.nickname if nickname_row else None
        items.append(dto)
    return jsonify({"items": items}), 200


@meinchat_bp.route("/api/v1/messaging/rooms/<room_id>/invite", methods=["POST"])
@require_auth
def invite_to_room(room_id: str):
    """Body: {nickname}. Any current member may invite (the service enforces
    it). 404 unknown nickname, 403 non-member, 404 unknown room."""
    data = request.get_json(silent=True) or {}
    nickname = data.get("nickname")
    if not isinstance(nickname, str) or not nickname.strip():
        return jsonify({"error": "nickname is required"}), 400
    row = NicknameRepository(db.session).find_by_nickname_ci(nickname.strip())
    if row is None or row.banned or row.search_hidden:
        return jsonify({"error": f"'{nickname}' not found"}), 404
    try:
        member = _room_service().invite(room_id, g.user_id, row.user_id)
        db.session.commit()
    except RoomServiceNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404
    except RoomServiceNotMemberError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 403
    return jsonify(member.to_dict()), 201


@meinchat_bp.route("/api/v1/messaging/rooms/<room_id>/leave", methods=["POST"])
@require_auth
def leave_room(room_id: str):
    try:
        _room_service().leave(room_id, g.user_id)
        db.session.commit()
    except RoomServiceNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404
    except RoomServiceNotMemberError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 403
    return "", 204


@meinchat_bp.route(
    "/api/v1/messaging/rooms/<room_id>/members/<user_id>", methods=["DELETE"]
)
@require_auth
def remove_room_member(room_id: str, user_id: str):
    try:
        _room_service().remove_member(room_id, g.user_id, user_id)
        db.session.commit()
    except RoomServiceNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404
    except RoomPermissionError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 403
    except RoomServiceNotMemberError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404
    return "", 204


@meinchat_bp.route("/api/v1/messaging/rooms/<room_id>/messages", methods=["GET"])
@require_auth
def list_room_messages_route(room_id: str):
    before = request.args.get("before")
    limit = min(int(request.args.get("limit", 50)), 200)
    # Optional fetching device id (e2e rooms): same delivery-tracking contract
    # as the 1:1 list — the caller's own device drives the per-device row.
    fetching_device_id = request.args.get("device_id") or request.headers.get(
        "X-Device-Id"
    )
    try:
        msgs = _message_service().list_room_messages(
            room_id, caller_user_id=g.user_id, before=before, limit=limit
        )
        if fetching_device_id:
            _mark_e2e_delivered(
                msgs, caller_user_id=g.user_id, device_id=fetching_device_id
            )
            db.session.commit()
    except RoomNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except NotARoomMemberError as exc:
        return jsonify({"error": str(exc)}), 403
    return jsonify({"items": [m.to_dict() for m in msgs]}), 200


@meinchat_bp.route("/api/v1/messaging/rooms/<room_id>/messages", methods=["POST"])
@require_auth
def send_room_message(room_id: str):
    blocked = _enforce_rate("message_send")
    if blocked is not None:
        return blocked
    data = request.get_json(silent=True) or {}

    # Protocol is pinned on the room at creation. For an e2e_v1 room the client
    # posts an opaque base64 `envelope_b64` (the server never sees plaintext);
    # plain rooms post `body` (+ optional structured `meta`).
    room = RoomRepository(db.session).find_by_id(room_id)
    protocol = room.protocol if room is not None else "plain"
    if protocol != "plain":
        envelope_b64 = data.get("envelope_b64")
        if not isinstance(envelope_b64, str):
            return (
                jsonify({"error": "envelope_b64 is required for this room"}),
                400,
            )
        try:
            send_body: Any = base64.b64decode(envelope_b64, validate=True)
        except (ValueError, TypeError):
            return jsonify({"error": "envelope_b64 must be valid base64"}), 400
        meta = None
    else:
        send_body = data.get("body", "")
        meta = data.get("meta")

    try:
        # D11 (word-based) — gate the send on a POSITIVE balance BEFORE creating
        # the message, so a guest with no tokens never sends nor triggers the
        # bot. No-op for a logged-in sender, a non-widget room, or economy-off.
        # The per-word charge happens in the registered post-send hook (it bills
        # both this question's words and the bot's answer's words).
        _widget_room_meter().guard_send(room, g.user_id)
        msg = _message_service().send_room_text(
            room_id,
            sender_user_id=g.user_id,
            body=send_body,
            protocol_hint=protocol,
            meta=meta,
        )
        db.session.commit()
        payload = msg.to_dict()
        # Surface the guest's balance AFTER the question charge so the FE can
        # render the remaining-tokens count without a second round-trip.
        if _is_metered_guest_send(room, g.user_id):
            payload["token_balance"] = _guest_token_balance(g.user_id)
        return jsonify(payload), 201
    except InsufficientGuestTokensError:
        db.session.rollback()
        return (
            jsonify(
                {
                    "error": "not enough tokens to continue the dialogue",
                    "code": "insufficient_tokens",
                }
            ),
            402,
        )
    except RoomNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404
    except NotARoomMemberError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 403
    except MessageBodyTooLongError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except (ValueError, TypeError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400


@meinchat_bp.route("/api/v1/messaging/rooms/<room_id>/read", methods=["POST"])
@require_auth
def mark_room_read_route(room_id: str):
    try:
        _message_service().mark_room_read(room_id, reader_user_id=g.user_id)
        db.session.commit()
    except RoomNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404
    except NotARoomMemberError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 403
    return "", 204


# ── /api/v1/messaging/widget/start (S86.3 D5) ───────────────────────────────


@meinchat_bp.route("/api/v1/messaging/widget/start", methods=["POST"])
def widget_start():
    """Create a room for a bot-widget visitor (NO @require_auth — a public
    widget allows an anonymous visitor; a logged_in widget verifies the bearer).

    Body: {widget_slug, display_name?}. The member list + visibility are read
    from the STORED cms widget (server-trusted, D2) — never the body. The IP
    rate limit is applied only on the public (guest-provisioning) path."""
    data = request.get_json(silent=True) or {}
    widget_slug = data.get("widget_slug")
    if not isinstance(widget_slug, str) or not widget_slug.strip():
        return jsonify({"error": "widget_slug is required"}), 400
    widget_slug = widget_slug.strip()

    config = _widget_reader().get_active_widget_config(widget_slug)
    if config is None:
        return jsonify({"error": "widget not found", "code": "widget_not_found"}), 404

    is_public = (config.get("visibility") or "logged_in") == "public"
    if is_public:
        # D12 — forensic fingerprint candidate log (IP / UA / Accept-Language).
        # File-only, no DB, no enforcement; for later overuse analysis.
        _log_widget_start_fingerprint(widget_slug)
        blocked = _enforce_widget_guest_start_rate()
        if blocked is not None:
            return blocked

    caller_user_id = None if is_public else _resolve_optional_caller_id()
    # D12 — a returning public guest presents its own bearer; reuse its room +
    # balance instead of provisioning + re-granting. Only honoured on the public
    # path (a logged_in widget already uses the bearer as the caller).
    presented_guest_user_id = _resolve_optional_caller_id() if is_public else None
    display_name = data.get("display_name")

    try:
        result = _widget_start_service().start(
            widget_slug,
            display_name=display_name,
            caller_user_id=caller_user_id,
            presented_guest_user_id=presented_guest_user_id,
        )
        db.session.commit()
    except WidgetNotFoundError:
        db.session.rollback()
        return jsonify({"error": "widget not found", "code": "widget_not_found"}), 404
    except WidgetAuthRequiredError:
        db.session.rollback()
        return jsonify({"error": "authentication required"}), 401
    except NicknameRequiredError:
        db.session.rollback()
        return (
            jsonify({"error": "a nickname is required", "code": "nickname_required"}),
            409,
        )
    except DisplayNameRequiredError:
        db.session.rollback()
        return jsonify({"error": "display_name is required"}), 400
    except PublicHumanMemberError:
        db.session.rollback()
        return (
            jsonify(
                {
                    "error": "this widget cannot invite a human member publicly",
                    "code": "public_human_member_not_allowed",
                }
            ),
            409,
        )
    except UnknownMemberError as exc:
        db.session.rollback()
        return jsonify({"error": f"'{exc}' not found"}), 404

    payload = {
        "room_id": str(result.room_id),
        "self_nickname": result.self_nickname,
        "members": result.members,
    }
    if result.access_token is not None:
        payload["access_token"] = result.access_token
    # D11 — surface the guest's remaining balance so the FE can render the
    # remaining-tokens count + the "Buy tokens to continue dialogue" block.
    if result.guest_user_id is not None and bool(
        _economy_config()["guest_economy_enabled"]
    ):
        payload["token_balance"] = _guest_token_balance(result.guest_user_id)
        # Surface the admin-configured token-bundles page link so the FE points
        # the out-of-tokens "Buy tokens" button at it. DRY fallback to
        # DEFAULT_CONFIG (never a bare literal) when the admin left it unset.
        payload["buy_tokens_href"] = _meinchat_config().get(
            "buy_tokens_href", DEFAULT_CONFIG["buy_tokens_href"]
        )
    return jsonify(payload), 201


@meinchat_bp.route("/api/v1/messaging/widget/balance", methods=["GET"])
@require_auth
def widget_balance():
    """Return the caller's live token balance (D11). The guest JWT minted by
    ``widget/start`` passes ``@require_auth``, so the FE can refresh the
    remaining-tokens count after a bot answer arrives. Returns ``token_balance``
    to match the ``widget/start`` and room-send response shape."""
    return jsonify({"token_balance": _guest_token_balance(g.user_id)}), 200


# ── SSE: instant message delivery ───────────────────────────────────────────


@meinchat_bp.route("/api/v1/messaging/stream/token", methods=["POST"])
@require_auth
def mint_stream_token():
    token = _stream_token_service().mint(g.user_id)
    cfg = _meinchat_config()
    return (
        jsonify(
            {
                "stream_token": token,
                "ttl_seconds": int(cfg.get("sse_stream_token_ttl_minutes", 60)) * 60,
            }
        ),
        200,
    )


@meinchat_bp.route("/api/v1/messaging/stream", methods=["GET"])
def sse_stream():
    """Long-lived `text/event-stream` response.

    `EventSource` can't set headers, so this endpoint is NOT decorated with
    @require_auth; it verifies the stream_token query parameter instead.
    """
    stream_token = request.args.get("stream_token", "").strip()
    if not stream_token:
        return jsonify({"error": "stream_token is required"}), 401
    try:
        user_id = _stream_token_service().verify(stream_token)
    except StreamTokenExpiredError:
        return jsonify({"error": "stream_token expired"}), 401
    except StreamTokenInvalidError as exc:
        return jsonify({"error": str(exc)}), 401

    cfg = _meinchat_config()
    heartbeat_s = float(cfg.get("sse_heartbeat_seconds", 20))
    # Cap each stream's server-side lifetime so an idle connection is recycled
    # instead of pinning worker/DB resources indefinitely. The browser's
    # EventSource auto-reconnects, so the cap is invisible to the user.
    max_stream_s = float(cfg.get("sse_max_stream_seconds", 600))
    bus = _event_bus()
    # Fan the caller's own channel PLUS each room they belong to into ONE
    # stream (S86.1 D5) — membership read from meinchat_room_member, so a
    # non-member never receives a room's events. One subscription, one stream;
    # the room channels are resolved once at connect time.
    member_rooms = RoomRepository(db.session).list_for_user(user_id)
    channels = [f"user:{user_id}"] + [f"room:{room.id}" for room in member_rooms]
    subscription = bus.subscribe_many(channels, heartbeat_seconds=heartbeat_s)

    @stream_with_context
    def generate():
        # First byte keeps the connection warm immediately so the browser
        # transitions out of the EventSource "connecting" state.
        yield ": meinchat stream connected\n\n"
        # Release the DB connection used by token verification back to the pool
        # before the (long, query-free) wait loop. Under `@stream_with_context`
        # the request — and thus its session — lives for the whole stream, so
        # without this each open stream would also hold a pool connection.
        db.session.remove()
        try:
            for event in subscription.iter_events(timeout=max_stream_s):
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            subscription.close()

    return Response(
        generate(),
        status=200,
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disables nginx buffering
            "Connection": "keep-alive",
        },
    )


# ── /api/v1/token-transfer/* ────────────────────────────────────────────────


@meinchat_bp.route("/api/v1/token-transfer", methods=["POST"])
@require_auth
def create_token_transfer():
    """Body: {to_nickname, amount, note?}. Moves tokens atomically and
    drops a system message into the shared conversation."""
    data = request.get_json(silent=True) or {}
    to_nickname = data.get("to_nickname")
    amount = data.get("amount")
    note = data.get("note")

    if not isinstance(to_nickname, str) or not to_nickname.strip():
        return jsonify({"error": "to_nickname is required"}), 400

    try:
        result = _token_transfer_service().transfer(
            sender_user_id=g.user_id,
            recipient_nickname=to_nickname.strip().lower(),
            amount=amount,
            note=note,
        )
        db.session.commit()
        return (
            jsonify(
                {
                    "transfer_id": result["transfer_id"],
                    "amount": result["amount"],
                    "recipient_nickname": result["recipient_nickname"],
                    "new_balance": result["new_balance"],
                }
            ),
            201,
        )
    except NicknameNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404
    except SelfTransferError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except InsufficientTokensError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 402  # Payment Required
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400


@meinchat_bp.route("/api/v1/token-transfer/history", methods=["GET"])
@require_auth
def list_token_transfers():
    direction = request.args.get("direction", "all")
    try:
        rows = _token_transfer_service().list_history(g.user_id, direction=direction)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"items": [row.to_dict() for row in rows]}), 200


# ── /api/v1/admin/meinchat/* ────────────────────────────────────────────────


@meinchat_bp.route("/api/v1/admin/meinchat/nicknames", methods=["GET"])
@require_auth
@require_admin
@require_permission("meinchat.nicknames.moderate")
def admin_list_nicknames():
    """Paged list including banned + search_hidden rows so the admin
    table can show the full directory and bulk-moderate."""
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = max(1, min(200, int(request.args.get("per_page", 50))))
    except (TypeError, ValueError):
        return jsonify({"error": "page and per_page must be integers"}), 400

    query = (request.args.get("q") or "").strip().lower() or None
    result = NicknameRepository(db.session).list_paged(
        page=page, per_page=per_page, query=query
    )
    return (
        jsonify(
            {
                "items": [row.to_dict() for row in result["items"]],
                "page": result["page"],
                "per_page": result["per_page"],
                "total": result["total"],
            }
        ),
        200,
    )


@meinchat_bp.route("/api/v1/admin/meinchat/nicknames/<user_id>/ban", methods=["POST"])
@require_auth
@require_admin
@require_permission("meinchat.nicknames.moderate")
def admin_ban_nickname(user_id: str):
    """Mark the user's nickname as banned. Self-ban returns 409 to prevent
    accidental admin lockout."""
    if str(g.user_id) == user_id:
        return jsonify({"error": "cannot ban your own nickname"}), 409
    try:
        row = _nickname_service().ban(user_id)
        db.session.commit()
        return jsonify(row.to_dict()), 200
    except NicknameNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404


@meinchat_bp.route("/api/v1/admin/meinchat/nicknames/<user_id>/unban", methods=["POST"])
@require_auth
@require_admin
@require_permission("meinchat.nicknames.moderate")
def admin_unban_nickname(user_id: str):
    try:
        row = _nickname_service().unban(user_id)
        db.session.commit()
        return jsonify(row.to_dict()), 200
    except NicknameNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404


# NOTE: the admin "conversation inspector" route was intentionally REMOVED
# (privacy / product strategy: admins must not read conversation content or
# history). Moderation is limited to nicknames + the transfer audit log.


@meinchat_bp.route("/api/v1/admin/meinchat/transfers", methods=["GET"])
@require_auth
@require_admin
@require_permission("meinchat.transfers.view")
def admin_list_transfers():
    """Paged audit log of every peer-to-peer token transfer.

    Query params: page (default 1, >= 1), per_page (default 50, 1..200).
    """
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = max(1, min(200, int(request.args.get("per_page", 50))))
    except (TypeError, ValueError):
        return jsonify({"error": "page and per_page must be integers"}), 400

    from plugins.meinchat.meinchat.models.token_transfer import (
        TokenTransferRecord,
    )

    query = db.session.query(TokenTransferRecord).order_by(
        TokenTransferRecord.executed_at.desc()
    )
    total = query.count()
    rows = query.offset((page - 1) * per_page).limit(per_page).all()

    nickname_repo = NicknameRepository(db.session)

    def _enrich(record):
        sender_nick = nickname_repo.find_by_user_id(record.sender_user_id)
        recipient_nick = nickname_repo.find_by_user_id(record.recipient_user_id)
        return {
            **record.to_dict(),
            "sender_nickname": sender_nick.nickname if sender_nick else None,
            "recipient_nickname": (recipient_nick.nickname if recipient_nick else None),
        }

    return (
        jsonify(
            {
                "items": [_enrich(row) for row in rows],
                "page": page,
                "per_page": per_page,
                "total": total,
            }
        ),
        200,
    )


# ── guest token balances (top-up / reset existing widget guests) ─────────────


def _parse_guest_token_body():
    """Validate the shared {mode, amount} body for the guest-token writes.

    Returns ``(mode, amount)`` where ``amount`` defaults to the configured
    ``guest_initial_tokens`` when omitted. Raises ``ValueError`` with a
    user-facing message on a bad mode / non-int amount."""
    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode")
    if mode not in ("topup", "reset"):
        raise ValueError("mode must be 'topup' or 'reset'")
    if "amount" in payload and payload["amount"] is not None:
        amount = payload["amount"]
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ValueError("amount must be an integer")
    else:
        amount = int(_economy_config()["guest_initial_tokens"])
    return mode, amount


@meinchat_bp.route("/api/v1/admin/meinchat/guests", methods=["GET"])
@require_auth
@require_admin
@require_permission("meinchat.guests.manage")
def admin_list_guests():
    """Paged, distinct-by-guest listing with each guest's live core token
    balance, so the admin can see who is out of tokens and top them up."""
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = max(1, min(200, int(request.args.get("per_page", 50))))
    except (TypeError, ValueError):
        return jsonify({"error": "page and per_page must be integers"}), 400

    query = (request.args.get("q") or "").strip() or None
    result = _guest_token_admin_service().list_guests(
        page=page, per_page=per_page, query=query
    )
    return jsonify(result), 200


@meinchat_bp.route(
    "/api/v1/admin/meinchat/guests/<guest_user_id>/tokens", methods=["POST"]
)
@require_auth
@require_admin
@require_permission("meinchat.guests.manage")
def admin_change_guest_tokens(guest_user_id: str):
    """Top-up or reset a single guest's balance.

    Body ``{"mode": "topup"|"reset", "amount": <int>}``. ``amount`` defaults to
    the configured ``guest_initial_tokens`` when omitted (so "reset to default"
    is a one-click action). 400 on a bad body, 404 on an unknown guest."""
    try:
        mode, amount = _parse_guest_token_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    service = _guest_token_admin_service()
    try:
        if mode == "topup":
            balance = service.topup(guest_user_id, amount)
        else:
            balance = service.reset(guest_user_id, amount)
        db.session.commit()
    except GuestNotFoundError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400

    return jsonify({"guest_user_id": guest_user_id, "balance": balance}), 200


@meinchat_bp.route("/api/v1/admin/meinchat/guests/tokens", methods=["POST"])
@require_auth
@require_admin
@require_permission("meinchat.guests.manage")
def admin_bulk_change_guest_tokens():
    """Apply a top-up or reset to ALL existing widget guests at once.

    Body ``{"mode": "topup"|"reset", "amount": <int>}`` (amount defaults to the
    configured ``guest_initial_tokens``). Returns the number of guests affected."""
    try:
        mode, amount = _parse_guest_token_body()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    service = _guest_token_admin_service()
    try:
        if mode == "topup":
            affected = service.topup_all(amount)
        else:
            affected = service.reset_all(amount)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400

    return jsonify({"affected": affected}), 200


@meinchat_bp.route("/api/v1/admin/meinchat/sessions/clear-guests", methods=["POST"])
@require_auth
@require_admin
@require_permission("meinchat.guests.manage")
def admin_clear_guest_sessions():
    """Wipe EVERY guest's meinchat data: their conversations + rooms (with
    cascaded messages / attachments / members) and their guest-session rows.
    Idempotent. Returns the deleted counts."""
    try:
        counts = _session_cleanup_service().clear_all_guest_sessions()
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return jsonify(counts), 200


@meinchat_bp.route(
    "/api/v1/admin/meinchat/users/<user_id>/sessions/clear", methods=["POST"]
)
@require_auth
@require_admin
@require_permission("meinchat.guests.manage")
def admin_clear_user_sessions(user_id: str):
    """Wipe ONE user's meinchat conversations + rooms + guest-session rows and
    reset their core token balance to the configured guest default. Works for a
    guest or a registered user. Idempotent. Returns deleted counts + balance."""
    try:
        counts = _session_cleanup_service().clear_user_sessions(user_id)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return jsonify(counts), 200
