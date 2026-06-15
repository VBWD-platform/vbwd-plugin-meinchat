"""S86.3 — word-based guest token economy (REVISED D11) + session reuse (D12)
against real PG.

1 token = 1 word. Drives the FULL runtime path through the Flask client:

- a public ``widget/start`` grants the guest its initial tokens (via the core
  TokenService);
- a guest's M-word room send debits exactly M tokens (the post-send charge hook);
- once the balance hits 0 the NEXT send is blocked with ``insufficient_tokens``
  (402) and creates no message — the in-flight round may spend to zero, the next
  send is gated;
- a returning guest's presented bearer reuses its room + balance without a
  re-grant;
- ``GET /api/v1/messaging/widget/balance`` returns the live balance for the
  guest token.

These integration bots are plain ``UserRole.BOT`` users with no reply provider,
so only the GUEST's own question words are charged here (the bot-answer charge is
covered by the unit hook test). The cms widget config is supplied by a fake
``ICmsWidgetReader`` (clean DI). Users/nicknames/bot roles are made through the
service/repo layer (no raw SQL).
"""
from __future__ import annotations

import importlib.util

import pytest

from vbwd.models.enums import UserRole

from plugins.meinchat.meinchat.extensibility import registry
from plugins.meinchat.meinchat.extensibility.cms_widget_reader import (
    ICmsWidgetReader,
)


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("plugins.bot_meinchat") is None,
    reason="token-economy counts require bot_meinchat's answer-word charge hook; covered by the full --full gate",
)


class _FakeWidgetReader:
    def __init__(self, configs):
        self._configs = configs

    def get_active_widget_config(self, slug):
        return self._configs.get(slug)


def _install_reader(configs):
    registry.register(ICmsWidgetReader, _FakeWidgetReader(configs))


def _register(app, email):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    user_repo = UserRepository(db.session)
    auth_service = app.container.auth_service()
    existing = user_repo.find_by_email(email)
    if existing is None:
        auth_service.register(email=email, password="WidgetEcon123@")
        db.session.commit()
        existing = user_repo.find_by_email(email)
    result = auth_service.login(email=email, password="WidgetEcon123@")
    return existing.id, result.token


def _set_nickname(app, user_id, nickname):
    from vbwd.extensions import db
    from plugins.meinchat.meinchat.models.user_nickname import UserNickname
    from plugins.meinchat.meinchat.repositories.nickname_repository import (
        NicknameRepository,
    )

    repo = NicknameRepository(db.session)
    if repo.find_by_user_id(user_id) is not None:
        return
    row = UserNickname()
    row.user_id = user_id
    row.nickname = nickname
    db.session.add(row)
    db.session.commit()


def _make_bot(app, email, nickname):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    user_id, _token = _register(app, email)
    _set_nickname(app, user_id, nickname)
    user = UserRepository(db.session).find_by_id(user_id)
    user.role = UserRole.BOT
    db.session.commit()
    return user_id


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _balance(app, user_id):
    return app.container.token_service().get_balance(user_id)


def _guest_user_id_for(app, widget_slug):
    from vbwd.extensions import db
    from plugins.meinchat.meinchat.models.guest_session import MeinchatGuestSession

    rows = (
        db.session.query(MeinchatGuestSession)
        .filter(MeinchatGuestSession.widget_slug == widget_slug)
        .all()
    )
    return rows[-1].guest_user_id if rows else None


@pytest.fixture(autouse=True)
def _isolate(app, monkeypatch):
    from plugins.meinchat.meinchat.extensibility.pipeline import IPostSendHook
    from plugins.meinchat.meinchat.services.rate_limiter import (
        InMemoryCounterBackend,
        RateLimiter,
    )
    from plugins.meinchat.meinchat.services.widget_room_meter import (
        WidgetRoomChargeHook,
    )

    original_limiter = getattr(app, "_meinchat_rate_limiter", None)
    app._meinchat_rate_limiter = RateLimiter(InMemoryCounterBackend())

    # These specs assert the GUEST-only per-word charge (bots have no reply
    # provider here). The ``IPostSendHook`` registry is process-global, so when
    # other plugins' full-app boots ran earlier in the same pytest process it
    # accumulates (a) ``bot_meinchat``'s ``MeinchatInboundHook`` (the bot would
    # then answer and its answer words would ALSO be charged) and (b) DUPLICATE
    # ``WidgetRoomChargeHook`` registrations (each session app boot re-runs
    # meinchat's ``on_enable``), which would charge each message twice. Both
    # corrupt the balance arithmetic. Snapshot the registry, then keep EXACTLY
    # ONE ``WidgetRoomChargeHook`` and drop every other hook for the duration of
    # the test; restore the real registry on teardown so no later suite is
    # disturbed.
    snapshot = registry.snapshot_for_tests()
    registry.reset_for_tests(IPostSendHook)
    charge_hook = next(
        (
            hook
            for hook in snapshot.get(IPostSendHook, [])
            if isinstance(hook, WidgetRoomChargeHook)
        ),
        None,
    )
    if charge_hook is not None:
        registry.register(IPostSendHook, charge_hook)

    yield

    # Restore the exact registry state captured at setup — this drops the test's
    # ``_install_reader`` registration AND re-instates every foreign hook we
    # removed, so no later suite is disturbed.
    registry.restore_for_tests(snapshot)
    app._meinchat_rate_limiter = original_limiter


def _patch_economy(app, monkeypatch, **overrides):
    """Override only the economy knobs, keeping the rest of meinchat's config."""
    config_store = app.config_store
    base = dict(config_store.get_config("meinchat") or {})
    base.update(overrides)
    monkeypatch.setattr(
        config_store,
        "get_config",
        lambda name: base if name == "meinchat" else (None),
    )


@pytest.mark.integration
def test_new_public_start_grants_initial_tokens(app, client, monkeypatch):
    _patch_economy(
        app,
        monkeypatch,
        guest_economy_enabled=True,
        guest_initial_tokens=20,
        guest_token_cost_per_word=1,
    )
    _make_bot(app, "econ-bot-grant@example.com", "econ-assistant-grant")
    _install_reader(
        {
            "econ-grant": {
                "visibility": "public",
                "member_nicknames": ["econ-assistant-grant"],
            }
        }
    )
    resp = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-grant", "display_name": "Granted Guest"},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["token_balance"] == 20
    guest_id = _guest_user_id_for(app, "econ-grant")
    assert _balance(app, guest_id) == 20


@pytest.mark.integration
def test_economy_disabled_grants_nothing(app, client, monkeypatch):
    _patch_economy(
        app, monkeypatch, guest_economy_enabled=False, guest_initial_tokens=20
    )
    _make_bot(app, "econ-bot-off@example.com", "econ-assistant-off")
    _install_reader(
        {
            "econ-off": {
                "visibility": "public",
                "member_nicknames": ["econ-assistant-off"],
            }
        }
    )
    resp = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-off", "display_name": "Free Guest"},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "token_balance" not in body
    guest_id = _guest_user_id_for(app, "econ-off")
    assert _balance(app, guest_id) == 0


@pytest.mark.integration
def test_guest_send_debits_per_word_and_reaches_buy_block(app, client, monkeypatch):
    _patch_economy(
        app,
        monkeypatch,
        guest_economy_enabled=True,
        guest_initial_tokens=5,
        guest_token_cost_per_word=1,
    )
    _make_bot(app, "econ-bot-spend@example.com", "econ-assistant-spend")
    _install_reader(
        {
            "econ-spend": {
                "visibility": "public",
                "member_nicknames": ["econ-assistant-spend"],
            }
        }
    )
    start = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-spend", "display_name": "Spender"},
    ).get_json()
    token = start["access_token"]
    room_id = start["room_id"]
    guest_id = _guest_user_id_for(app, "econ-spend")
    assert _balance(app, guest_id) == 5

    # A 3-word question debits 3; the send response surfaces the balance (2).
    first = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "how much shipping"},
        headers=_auth(token),
    )
    assert first.status_code == 201, first.get_data(as_text=True)
    assert first.get_json()["token_balance"] == 2
    assert _balance(app, guest_id) == 2

    # A 2-word question debits 2 → balance 0 (spend-until-empty is allowed
    # because the gate only checks balance > 0 BEFORE the send).
    second = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "thanks much"},
        headers=_auth(token),
    )
    assert second.status_code == 201
    assert second.get_json()["token_balance"] == 0
    assert _balance(app, guest_id) == 0

    # Balance is now 0 → the NEXT send is gated (402), no message created.
    blocked = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "another question"},
        headers=_auth(token),
    )
    assert blocked.status_code == 402
    assert blocked.get_json()["code"] == "insufficient_tokens"
    assert _balance(app, guest_id) == 0


@pytest.mark.integration
def test_overlong_send_clamps_to_zero_then_gates(app, client, monkeypatch):
    """A send while balance > 0 is allowed even if its word count exceeds the
    balance — the charge clamps to 0 and the NEXT send is gated."""
    _patch_economy(
        app,
        monkeypatch,
        guest_economy_enabled=True,
        guest_initial_tokens=2,
        guest_token_cost_per_word=1,
    )
    _make_bot(app, "econ-bot-clamp@example.com", "econ-assistant-clamp")
    _install_reader(
        {
            "econ-clamp": {
                "visibility": "public",
                "member_nicknames": ["econ-assistant-clamp"],
            }
        }
    )
    start = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-clamp", "display_name": "Clamper"},
    ).get_json()
    token = start["access_token"]
    room_id = start["room_id"]
    guest_id = _guest_user_id_for(app, "econ-clamp")

    # balance=2, a 5-word send → clamps to 0 (not negative), still 201.
    resp = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "one two three four five"},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    assert resp.get_json()["token_balance"] == 0
    assert _balance(app, guest_id) == 0

    blocked = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "again"},
        headers=_auth(token),
    )
    assert blocked.status_code == 402


@pytest.mark.integration
def test_zero_balance_send_creates_no_message(app, client, monkeypatch):
    _patch_economy(
        app,
        monkeypatch,
        guest_economy_enabled=True,
        guest_initial_tokens=0,
        guest_token_cost_per_word=1,
    )
    _make_bot(app, "econ-bot-nomsg@example.com", "econ-assistant-nomsg")
    _install_reader(
        {
            "econ-nomsg": {
                "visibility": "public",
                "member_nicknames": ["econ-assistant-nomsg"],
            }
        }
    )
    start = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-nomsg", "display_name": "Broke"},
    ).get_json()
    token = start["access_token"]
    room_id = start["room_id"]

    # initial=0 → balance is not positive → blocked on the very first send.
    blocked = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "hi"},
        headers=_auth(token),
    )
    assert blocked.status_code == 402

    listing = client.get(
        f"/api/v1/messaging/rooms/{room_id}/messages", headers=_auth(token)
    )
    assert listing.status_code == 200
    assert listing.get_json()["items"] == []


@pytest.mark.integration
def test_logged_in_sender_is_not_charged(app, client, monkeypatch):
    _patch_economy(
        app,
        monkeypatch,
        guest_economy_enabled=True,
        guest_token_cost_per_word=5,
    )
    caller_id, caller_token = _register(app, "econ-caller@example.com")
    _set_nickname(app, caller_id, "econ-caller")
    _make_bot(app, "econ-bot-li@example.com", "econ-assistant-li")
    _install_reader(
        {
            "econ-li": {
                "visibility": "logged_in",
                "member_nicknames": ["econ-assistant-li"],
            }
        }
    )
    start = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-li"},
        headers=_auth(caller_token),
    ).get_json()
    room_id = start["room_id"]

    # The logged-in caller has zero tokens but is never metered (not a GUEST).
    resp = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "free message here"},
        headers=_auth(caller_token),
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    assert "token_balance" not in resp.get_json()
    assert _balance(app, caller_id) == 0


@pytest.mark.integration
def test_non_widget_room_send_is_not_charged(app, client, monkeypatch):
    _patch_economy(
        app,
        monkeypatch,
        guest_economy_enabled=True,
        guest_token_cost_per_word=5,
    )
    caller_id, caller_token = _register(app, "econ-plain-caller@example.com")
    _set_nickname(app, caller_id, "econ-plain-caller")
    peer_id, _peer_token = _register(app, "econ-plain-peer@example.com")
    _set_nickname(app, peer_id, "econ-plain-peer")
    _install_reader({})

    # A normal (non-widget) room created by the caller.
    created = client.post(
        "/api/v1/messaging/rooms",
        json={"member_nicknames": ["econ-plain-peer"]},
        headers=_auth(caller_token),
    )
    assert created.status_code == 201, created.get_data(as_text=True)
    room_id = created.get_json()["id"]

    resp = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "hello there friend"},
        headers=_auth(caller_token),
    )
    assert resp.status_code == 201
    assert _balance(app, caller_id) == 0


@pytest.mark.integration
def test_widget_balance_endpoint_returns_live_balance(app, client, monkeypatch):
    _patch_economy(
        app,
        monkeypatch,
        guest_economy_enabled=True,
        guest_initial_tokens=6,
        guest_token_cost_per_word=1,
    )
    _make_bot(app, "econ-bot-bal@example.com", "econ-assistant-bal")
    _install_reader(
        {
            "econ-bal": {
                "visibility": "public",
                "member_nicknames": ["econ-assistant-bal"],
            }
        }
    )
    start = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-bal", "display_name": "Checker"},
    ).get_json()
    token = start["access_token"]
    room_id = start["room_id"]

    balance_before = client.get(
        "/api/v1/messaging/widget/balance", headers=_auth(token)
    )
    assert balance_before.status_code == 200, balance_before.get_data(as_text=True)
    assert balance_before.get_json()["token_balance"] == 6

    # Spend 2 words, then the live balance endpoint reflects it.
    client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "hello world"},
        headers=_auth(token),
    )
    balance_after = client.get("/api/v1/messaging/widget/balance", headers=_auth(token))
    assert balance_after.get_json()["token_balance"] == 4


@pytest.mark.integration
def test_widget_balance_endpoint_requires_auth(app, client, monkeypatch):
    resp = client.get("/api/v1/messaging/widget/balance")
    assert resp.status_code == 401


@pytest.mark.integration
def test_presenting_guest_bearer_reuses_room_without_regrant(app, client, monkeypatch):
    _patch_economy(
        app,
        monkeypatch,
        guest_economy_enabled=True,
        guest_initial_tokens=10,
        guest_token_cost_per_word=1,
    )
    _make_bot(app, "econ-bot-reuse@example.com", "econ-assistant-reuse")
    _install_reader(
        {
            "econ-reuse": {
                "visibility": "public",
                "member_nicknames": ["econ-assistant-reuse"],
            }
        }
    )
    first = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-reuse", "display_name": "Returning"},
    ).get_json()
    guest_id = _guest_user_id_for(app, "econ-reuse")
    assert _balance(app, guest_id) == 10

    # Spend a 2-word question so the reuse must preserve the SAME balance.
    client.post(
        f"/api/v1/messaging/rooms/{first['room_id']}/messages",
        json={"body": "first question"},
        headers=_auth(first["access_token"]),
    )
    assert _balance(app, guest_id) == 8

    # Present the SAME guest bearer → reuse the room, no re-grant.
    again = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-reuse", "display_name": "Ignored"},
        headers=_auth(first["access_token"]),
    )
    assert again.status_code == 201, again.get_data(as_text=True)
    body = again.get_json()
    assert body["room_id"] == first["room_id"]
    # D12 — no NEW guest is provisioned and no tokens are re-granted, but a
    # returning guest still gets a usable re-minted access_token so the FE keeps
    # driving the guest's room as the guest (never the app session). Re-minting
    # mints no tokens, so the grant stays idempotent.
    assert body["access_token"]
    assert body["token_balance"] == 8
    assert _balance(app, guest_id) == 8

    # The re-minted token must actually authorise the SAME guest room (proving
    # the FE no longer needs to fall back to the app session on reuse).
    listed = client.get(
        f"/api/v1/messaging/rooms/{first['room_id']}/messages",
        headers=_auth(body["access_token"]),
    )
    assert listed.status_code == 200, listed.get_data(as_text=True)


@pytest.mark.integration
def test_absent_bearer_provisions_fresh_guest_and_grants(app, client, monkeypatch):
    _patch_economy(app, monkeypatch, guest_economy_enabled=True, guest_initial_tokens=7)
    _make_bot(app, "econ-bot-fresh@example.com", "econ-assistant-fresh")
    _install_reader(
        {
            "econ-fresh": {
                "visibility": "public",
                "member_nicknames": ["econ-assistant-fresh"],
            }
        }
    )
    first = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-fresh", "display_name": "Guest A"},
    ).get_json()
    second = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-fresh", "display_name": "Guest B"},
    ).get_json()
    # Two distinct guests, each granted independently.
    assert first["room_id"] != second["room_id"]
    assert first["self_nickname"] != second["self_nickname"]
    assert second["token_balance"] == 7


@pytest.mark.integration
def test_fingerprint_is_logged_to_file_not_db(app, client, monkeypatch, tmp_path):
    monkeypatch.setenv("VBWD_LOG_DIR", str(tmp_path))
    # Force a fresh logger so it picks up the patched VBWD_LOG_DIR.
    import logging

    from plugins.meinchat.meinchat.routes import _FINGERPRINT_LOGGER_NAME

    logger = logging.getLogger(_FINGERPRINT_LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    if hasattr(logger, "_meinchat_fingerprint_configured"):
        delattr(logger, "_meinchat_fingerprint_configured")

    _patch_economy(app, monkeypatch, guest_economy_enabled=True)
    _make_bot(app, "econ-bot-fp@example.com", "econ-assistant-fp")
    _install_reader(
        {"econ-fp": {"visibility": "public", "member_nicknames": ["econ-assistant-fp"]}}
    )

    from plugins.meinchat.meinchat.models.guest_session import MeinchatGuestSession
    from vbwd.extensions import db

    before = db.session.query(MeinchatGuestSession).count()

    resp = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-fp", "display_name": "Tracked"},
        headers={
            "User-Agent": "FingerprintUA/9.9",
            "Accept-Language": "de-DE,de;q=0.9",
        },
    )
    assert resp.status_code == 201

    log_file = tmp_path / "widget_guest_fingerprint.log"
    assert log_file.exists()
    contents = log_file.read_text(encoding="utf-8")
    assert "econ-fp" in contents
    assert "FingerprintUA/9.9" in contents
    assert "de-DE" in contents

    # The fingerprint is log-only: exactly ONE new guest-session row (the
    # provisioned guest), and no separate fingerprint table/rows.
    after = db.session.query(MeinchatGuestSession).count()
    assert after == before + 1


# ── Fresh-install config resolution (the REAL `_meinchat_config()` path) ──────
# These deliberately DO NOT patch `config_store.get_config` away. On a fresh
# install the persisted meinchat override is `{}`, so the economy reads MUST fall
# back to the plugin's DEFAULT_CONFIG (enabled / 20 initial / 1 per word). The
# pre-existing economy tests all monkeypatch `get_config` to a fully-populated
# dict, so they never exercised this fallback and missed the out-of-the-box
# "guest granted 0 tokens → first send 402" breakage.


def _force_persisted_config(app, monkeypatch, persisted):
    """Make the real config_store return exactly ``persisted`` for meinchat
    (an empty dict models a fresh install; a partial dict models an admin who
    set only some knobs). Other plugins still read normally."""
    config_store = app.config_store
    monkeypatch.setattr(
        config_store,
        "get_config",
        lambda name: dict(persisted) if name == "meinchat" else None,
    )


@pytest.mark.integration
def test_fresh_install_widget_start_grants_default_initial_tokens(
    app, client, monkeypatch
):
    """Empty persisted config → DEFAULT_CONFIG initial grant (20), not 0."""
    _force_persisted_config(app, monkeypatch, {})
    _make_bot(app, "econ-bot-fresh@example.com", "econ-assistant-fresh")
    _install_reader(
        {
            "econ-fresh": {
                "visibility": "public",
                "member_nicknames": ["econ-assistant-fresh"],
            }
        }
    )
    resp = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-fresh", "display_name": "Fresh Guest"},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["token_balance"] == 20
    guest_id = _guest_user_id_for(app, "econ-fresh")
    assert _balance(app, guest_id) == 20


@pytest.mark.integration
def test_widget_start_surfaces_configured_buy_tokens_href(app, client, monkeypatch):
    """When the admin sets `buy_tokens_href`, widget/start surfaces it so the FE
    can point the out-of-tokens "Buy tokens" button at the configured page."""
    _patch_economy(
        app,
        monkeypatch,
        guest_economy_enabled=True,
        guest_initial_tokens=20,
        guest_token_cost_per_word=1,
        buy_tokens_href="/pricing",
    )
    _make_bot(app, "econ-bot-href@example.com", "econ-assistant-href")
    _install_reader(
        {
            "econ-href": {
                "visibility": "public",
                "member_nicknames": ["econ-assistant-href"],
            }
        }
    )
    resp = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-href", "display_name": "Href Guest"},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["buy_tokens_href"] == "/pricing"


@pytest.mark.integration
def test_fresh_install_widget_start_defaults_buy_tokens_href(app, client, monkeypatch):
    """Empty persisted config → DEFAULT_CONFIG `buy_tokens_href` (/tokens)."""
    _force_persisted_config(app, monkeypatch, {})
    _make_bot(app, "econ-bot-href-def@example.com", "econ-assistant-href-def")
    _install_reader(
        {
            "econ-href-def": {
                "visibility": "public",
                "member_nicknames": ["econ-assistant-href-def"],
            }
        }
    )
    resp = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-href-def", "display_name": "Default Href Guest"},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["buy_tokens_href"] == "/tokens"


@pytest.mark.integration
def test_admin_override_initial_tokens_wins_over_default(app, client, monkeypatch):
    """A persisted override for one knob wins; the absent knobs still default."""
    _force_persisted_config(app, monkeypatch, {"guest_initial_tokens": 5})
    _make_bot(app, "econ-bot-override@example.com", "econ-assistant-override")
    _install_reader(
        {
            "econ-override": {
                "visibility": "public",
                "member_nicknames": ["econ-assistant-override"],
            }
        }
    )
    resp = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "econ-override", "display_name": "Override Guest"},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    # Override applied (5), economy still enabled by default (token_balance present).
    assert body["token_balance"] == 5
    guest_id = _guest_user_id_for(app, "econ-override")
    assert _balance(app, guest_id) == 5
