"""Flat Blueprint for every meinchat API route.

All routes use absolute `/api/v1/…` paths. Kept flat (no nested
blueprints) so Flask-WTF's `csrf.exempt(bp)` applied in `vbwd/app.py`
actually exempts every endpoint below.
"""
import base64
import json
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
from vbwd.middleware.auth import require_admin, require_auth, require_permission
from vbwd.plugins.payment_route_helpers import check_plugin_enabled

from vbwd.interfaces.file_storage import ManagerBackedFileStorage

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
    PlainAttachmentError,
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
from plugins.meinchat.meinchat.extensibility import registry
from plugins.meinchat.meinchat.extensibility.identity import (
    IDeviceDirectory,
    NullDeviceDirectory,
)
from plugins.meinchat.meinchat.extensibility.lifecycle import (
    IConversationCapabilities,
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
_PROTOCOL_PREFERENCE = ["e2e_v1", "plain"]


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
    )


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
        _message_service().mark_read(conv_id, reader_user_id=g.user_id)
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
    subscription = bus.subscribe(f"user:{user_id}", heartbeat_seconds=heartbeat_s)

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
