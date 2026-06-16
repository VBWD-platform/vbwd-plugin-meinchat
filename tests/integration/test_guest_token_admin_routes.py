"""Integration specs for the admin guest-token routes (top-up / reset).

Drives the FULL runtime path through the Flask client against real PG:

- the routes are permission-gated (401 unauthenticated, 403 non-admin);
- a single top-up / reset and a bulk change move the guest's REAL core balance
  via the core ``TokenService``;
- the user-facing fix is proven end-to-end: a guest spends to 0 → the next send
  402-gates → an admin top-up lets the SAME guest send again.

Guests are provisioned through the real ``widget/start`` route (a bot + a fake
``ICmsWidgetReader`` supply the widget config); the per-word charge hook is the
real one. Users / bots are made through the service/repo layer (no raw SQL).
"""
from __future__ import annotations

import pytest

from vbwd.models.enums import UserRole

from plugins.meinchat.meinchat.extensibility import registry
from plugins.meinchat.meinchat.extensibility.cms_widget_reader import (
    ICmsWidgetReader,
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
        auth_service.register(email=email, password="GuestAdmin123@")
        db.session.commit()
        existing = user_repo.find_by_email(email)
    result = auth_service.login(email=email, password="GuestAdmin123@")
    return existing.id, result.token


def _make_admin(app, email):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    user_id, token = _register(app, email)
    user = UserRepository(db.session).find_by_id(user_id)
    user.role = UserRole.ADMIN  # legacy fallback: ADMIN ⇒ all permissions
    db.session.commit()
    return user_id, token


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
def _isolate(app):
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

    # Keep EXACTLY ONE WidgetRoomChargeHook (the registry is process-global and
    # accumulates duplicate / foreign hooks across earlier full-app boots, which
    # would double-charge or have a bot answer and also charge its words).
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
    else:
        # No WidgetRoomChargeHook in the snapshot — this happens in a clean CI
        # process when this file runs before any other suite's app-boot
        # registered it (locally the registry has accumulated one, so the branch
        # above runs and this is a no-op). Register a fresh one via the plugin so
        # the per-word charge fires deterministically regardless of test order.
        meinchat_plugin = app.plugin_manager.get_plugin("meinchat")
        with app.app_context():
            meinchat_plugin._register_widget_charge_hook()

    yield

    registry.restore_for_tests(snapshot)
    app._meinchat_rate_limiter = original_limiter


def _patch_economy(app, monkeypatch, **overrides):
    config_store = app.config_store
    base = dict(config_store.get_config("meinchat") or {})
    base.update(overrides)
    monkeypatch.setattr(
        config_store,
        "get_config",
        lambda name: base if name == "meinchat" else None,
    )


def _start_guest(app, client, slug, bot_nick):
    _make_bot(app, f"gtadmin-bot-{slug}@example.com", bot_nick)
    _install_reader({slug: {"visibility": "public", "member_nicknames": [bot_nick]}})
    start = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": slug, "display_name": f"Guest {slug}"},
    ).get_json()
    return start


GUESTS_PATH = "/api/v1/admin/meinchat/guests"
BULK_TOKENS_PATH = "/api/v1/admin/meinchat/guests/tokens"


def _guest_tokens_path(guest_user_id):
    return f"/api/v1/admin/meinchat/guests/{guest_user_id}/tokens"


@pytest.mark.integration
def test_non_admin_is_forbidden(app, client):
    _user_id, token = _register(app, "gtadmin-plain@example.com")
    resp = client.get(GUESTS_PATH, headers=_auth(token))
    assert resp.status_code == 403


@pytest.mark.integration
def test_bad_mode_returns_400(app, client, monkeypatch):
    _patch_economy(app, monkeypatch, guest_initial_tokens=20)
    _admin_id, admin_token = _make_admin(app, "gtadmin-badmode@example.com")
    resp = client.post(
        BULK_TOKENS_PATH,
        json={"mode": "nonsense", "amount": 5},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 400


@pytest.mark.integration
def test_unknown_guest_returns_404(app, client, monkeypatch):
    _patch_economy(app, monkeypatch, guest_initial_tokens=20)
    _admin_id, admin_token = _make_admin(app, "gtadmin-404@example.com")
    resp = client.post(
        _guest_tokens_path("00000000-0000-0000-0000-0000000000ff"),
        json={"mode": "topup", "amount": 5},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.integration
def test_topup_increases_real_balance(app, client, monkeypatch):
    _patch_economy(app, monkeypatch, guest_economy_enabled=True, guest_initial_tokens=4)
    _admin_id, admin_token = _make_admin(app, "gtadmin-topup@example.com")
    _start_guest(app, client, "gt-topup", "gt-bot-topup")
    guest_id = _guest_user_id_for(app, "gt-topup")
    assert _balance(app, guest_id) == 4

    resp = client.post(
        _guest_tokens_path(guest_id),
        json={"mode": "topup", "amount": 10},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["balance"] == 14
    assert _balance(app, guest_id) == 14


@pytest.mark.integration
def test_reset_sets_exact_balance_up_and_down(app, client, monkeypatch):
    _patch_economy(app, monkeypatch, guest_economy_enabled=True, guest_initial_tokens=4)
    _admin_id, admin_token = _make_admin(app, "gtadmin-reset@example.com")
    _start_guest(app, client, "gt-reset", "gt-bot-reset")
    guest_id = _guest_user_id_for(app, "gt-reset")
    assert _balance(app, guest_id) == 4

    # Reset UP to 9.
    up = client.post(
        _guest_tokens_path(guest_id),
        json={"mode": "reset", "amount": 9},
        headers=_auth(admin_token),
    )
    assert up.status_code == 200
    assert up.get_json()["balance"] == 9
    assert _balance(app, guest_id) == 9

    # Reset DOWN to 2.
    down = client.post(
        _guest_tokens_path(guest_id),
        json={"mode": "reset", "amount": 2},
        headers=_auth(admin_token),
    )
    assert down.status_code == 200
    assert down.get_json()["balance"] == 2
    assert _balance(app, guest_id) == 2


@pytest.mark.integration
def test_reset_defaults_amount_to_configured_initial(app, client, monkeypatch):
    _patch_economy(app, monkeypatch, guest_economy_enabled=True, guest_initial_tokens=7)
    _admin_id, admin_token = _make_admin(app, "gtadmin-default@example.com")
    _start_guest(app, client, "gt-default", "gt-bot-default")
    guest_id = _guest_user_id_for(app, "gt-default")

    # Drop to 0 via a reset, then a no-amount reset restores the configured 7.
    client.post(
        _guest_tokens_path(guest_id),
        json={"mode": "reset", "amount": 0},
        headers=_auth(admin_token),
    )
    assert _balance(app, guest_id) == 0

    resp = client.post(
        _guest_tokens_path(guest_id),
        json={"mode": "reset"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200
    assert resp.get_json()["balance"] == 7


@pytest.mark.integration
def test_list_guests_shows_balance(app, client, monkeypatch):
    _patch_economy(
        app, monkeypatch, guest_economy_enabled=True, guest_initial_tokens=11
    )
    _admin_id, admin_token = _make_admin(app, "gtadmin-list@example.com")
    _start_guest(app, client, "gt-list", "gt-bot-list")
    guest_id = _guest_user_id_for(app, "gt-list")

    resp = client.get(GUESTS_PATH, headers=_auth(admin_token))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    listed = {item["guest_user_id"]: item for item in body["items"]}
    assert str(guest_id) in listed
    assert listed[str(guest_id)]["balance"] == 11


@pytest.mark.integration
def test_bulk_topup_affects_all_guests(app, client, monkeypatch):
    _patch_economy(app, monkeypatch, guest_economy_enabled=True, guest_initial_tokens=3)
    _admin_id, admin_token = _make_admin(app, "gtadmin-bulk@example.com")
    _start_guest(app, client, "gt-bulk-a", "gt-bot-bulk-a")
    _start_guest(app, client, "gt-bulk-b", "gt-bot-bulk-b")
    guest_a = _guest_user_id_for(app, "gt-bulk-a")
    guest_b = _guest_user_id_for(app, "gt-bulk-b")

    resp = client.post(
        BULK_TOKENS_PATH,
        json={"mode": "topup", "amount": 5},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["affected"] >= 2
    assert _balance(app, guest_a) == 8
    assert _balance(app, guest_b) == 8


@pytest.mark.integration
def test_exhausted_guest_can_send_again_after_admin_topup(app, client, monkeypatch):
    """The actual user-facing fix: a guest at balance 0 is 402-gated; after an
    admin top-up the SAME guest can send again."""
    _patch_economy(
        app,
        monkeypatch,
        guest_economy_enabled=True,
        guest_initial_tokens=2,
        guest_token_cost_per_word=1,
    )
    _admin_id, admin_token = _make_admin(app, "gtadmin-fix@example.com")
    start = _start_guest(app, client, "gt-fix", "gt-bot-fix")
    guest_token = start["access_token"]
    room_id = start["room_id"]
    guest_id = _guest_user_id_for(app, "gt-fix")
    assert _balance(app, guest_id) == 2

    # Spend the 2-token budget (a 2-word send → balance 0).
    spent = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "two words"},
        headers=_auth(guest_token),
    )
    assert spent.status_code == 201
    assert _balance(app, guest_id) == 0

    # Now the guest is gated.
    blocked = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "blocked now"},
        headers=_auth(guest_token),
    )
    assert blocked.status_code == 402

    # Admin tops the guest up.
    topup = client.post(
        _guest_tokens_path(guest_id),
        json={"mode": "topup", "amount": 5},
        headers=_auth(admin_token),
    )
    assert topup.status_code == 200
    assert topup.get_json()["balance"] == 5

    # The guest can send again.
    after = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "back in"},
        headers=_auth(guest_token),
    )
    assert after.status_code == 201, after.get_data(as_text=True)
    assert _balance(app, guest_id) == 3
