"""Integration specs for the meinchat session-cleanup routes (S-sessions).

The cleanup feature lets an admin wipe meinchat CHAT & GUEST session data:

- ``POST /api/v1/admin/meinchat/sessions/clear-guests`` deletes EVERY guest's
  conversations + rooms (+ cascaded messages/attachments/members) and their
  ``meinchat_guest_session`` rows; idempotent.
- ``POST /api/v1/admin/meinchat/users/<user_id>/sessions/clear`` does the same
  for ONE user (guest OR registered) and resets that user's core token balance
  to the configured ``guest_initial_tokens`` default.

Both routes are ``@require_auth @require_admin
@require_permission("meinchat.guests.manage")``. Data is seeded through the
real widget-start route and the service/repo layer (no raw SQL).
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
    user.role = UserRole.ADMIN
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


def _count_rooms(app, user_id):
    from vbwd.extensions import db
    from plugins.meinchat.meinchat.models.room_member import RoomMember

    return db.session.query(RoomMember).filter(RoomMember.user_id == user_id).count()


def _count_guest_sessions(app, guest_user_id):
    from vbwd.extensions import db
    from plugins.meinchat.meinchat.models.guest_session import MeinchatGuestSession

    return (
        db.session.query(MeinchatGuestSession)
        .filter(MeinchatGuestSession.guest_user_id == guest_user_id)
        .count()
    )


def _count_room_messages(app, room_id):
    from vbwd.extensions import db
    from plugins.meinchat.meinchat.models.message import Message

    return db.session.query(Message).filter(Message.room_id == room_id).count()


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
    _make_bot(app, f"sclean-bot-{slug}@example.com", bot_nick)
    _install_reader({slug: {"visibility": "public", "member_nicknames": [bot_nick]}})
    start = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": slug, "display_name": f"Guest {slug}"},
    ).get_json()
    return start


CLEAR_GUESTS_PATH = "/api/v1/admin/meinchat/sessions/clear-guests"


def _clear_user_path(user_id):
    return f"/api/v1/admin/meinchat/users/{user_id}/sessions/clear"


@pytest.mark.integration
def test_clear_guests_requires_admin(app, client):
    _user_id, token = _register(app, "sclean-plain@example.com")
    resp = client.post(CLEAR_GUESTS_PATH, headers=_auth(token))
    assert resp.status_code == 403


@pytest.mark.integration
def test_clear_user_requires_admin(app, client):
    user_id, token = _register(app, "sclean-plain-user@example.com")
    resp = client.post(_clear_user_path(user_id), headers=_auth(token))
    assert resp.status_code == 403


@pytest.mark.integration
def test_clear_all_guest_sessions_wipes_guest_data(app, client, monkeypatch):
    _patch_economy(app, monkeypatch, guest_economy_enabled=True, guest_initial_tokens=4)
    _admin_id, admin_token = _make_admin(app, "sclean-all@example.com")
    start = _start_guest(app, client, "sc-all", "sc-bot-all")
    guest_token = start["access_token"]
    room_id = start["room_id"]
    guest_id = _guest_user_id_for(app, "sc-all")

    # The guest sends a message so a room + message + member exist.
    sent = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "hello there"},
        headers=_auth(guest_token),
    )
    assert sent.status_code == 201
    assert _count_room_messages(app, room_id) >= 1
    assert _count_guest_sessions(app, guest_id) >= 1

    resp = client.post(CLEAR_GUESTS_PATH, headers=_auth(admin_token))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    counts = resp.get_json()
    assert counts["guests"] >= 1
    assert counts["sessions"] >= 1
    assert counts["rooms"] >= 1

    # Everything the guest owned is gone.
    assert _count_room_messages(app, room_id) == 0
    assert _count_guest_sessions(app, guest_id) == 0
    assert _count_rooms(app, guest_id) == 0


@pytest.mark.integration
def test_clear_all_guest_sessions_is_idempotent(app, client, monkeypatch):
    _patch_economy(app, monkeypatch, guest_economy_enabled=True, guest_initial_tokens=4)
    _admin_id, admin_token = _make_admin(app, "sclean-idem@example.com")
    _start_guest(app, client, "sc-idem", "sc-bot-idem")

    first = client.post(CLEAR_GUESTS_PATH, headers=_auth(admin_token))
    assert first.status_code == 200
    # Re-running is a clean no-op (no rows left, no error).
    second = client.post(CLEAR_GUESTS_PATH, headers=_auth(admin_token))
    assert second.status_code == 200
    assert second.get_json()["sessions"] == 0


@pytest.mark.integration
def test_clear_one_users_sessions_and_resets_balance(app, client, monkeypatch):
    _patch_economy(app, monkeypatch, guest_economy_enabled=True, guest_initial_tokens=6)
    _admin_id, admin_token = _make_admin(app, "sclean-one@example.com")
    start = _start_guest(app, client, "sc-one", "sc-bot-one")
    guest_token = start["access_token"]
    room_id = start["room_id"]
    guest_id = _guest_user_id_for(app, "sc-one")

    client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "spend two"},
        headers=_auth(guest_token),
    )
    assert _balance(app, guest_id) < 6

    resp = client.post(_clear_user_path(guest_id), headers=_auth(admin_token))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["sessions"] >= 1
    assert body["rooms"] >= 1
    assert body["balance"] == 6

    assert _count_room_messages(app, room_id) == 0
    assert _count_guest_sessions(app, guest_id) == 0
    assert _balance(app, guest_id) == 6


@pytest.mark.integration
def test_clear_registered_user_with_no_data_is_clean_noop(app, client, monkeypatch):
    _patch_economy(app, monkeypatch, guest_economy_enabled=True, guest_initial_tokens=5)
    _admin_id, admin_token = _make_admin(app, "sclean-noop@example.com")
    plain_id, _token = _register(app, "sclean-noop-target@example.com")

    resp = client.post(_clear_user_path(plain_id), headers=_auth(admin_token))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["conversations"] == 0
    assert body["rooms"] == 0
    assert body["sessions"] == 0
    # A registered (non-guest) user's balance is reset to the configured default.
    assert body["balance"] == 5
    assert _balance(app, plain_id) == 5
