"""S86.3 — `POST /messaging/widget/start` against real Postgres + Flask client.

The cms widget config is supplied by a FAKE `ICmsWidgetReader` registered into
the in-process port registry (clean DI; meinchat does not hard-depend on cms).
Users + nicknames + bot roles are created through the service/repo layer (no raw
INSERT), per feedback_no_direct_db_for_test_data.
"""
from __future__ import annotations

import pytest

from vbwd.models.enums import UserRole

from plugins.meinchat.meinchat.extensibility import registry
from plugins.meinchat.meinchat.extensibility.cms_widget_reader import (
    ICmsWidgetReader,
)


class _FakeWidgetReader:
    """Maps slug → stored widget config (the server-trusted member list)."""

    def __init__(self, configs):
        self._configs = configs

    def get_active_widget_config(self, slug):
        return self._configs.get(slug)


def _install_reader(configs):
    reader = _FakeWidgetReader(configs)
    registry.register(ICmsWidgetReader, reader)
    return reader


def _register(app, email):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    user_repo = UserRepository(db.session)
    auth_service = app.container.auth_service()
    existing = user_repo.find_by_email(email)
    if existing is not None:
        result = auth_service.login(email=email, password="WidgetStart123@")
        return existing.id, result.token
    auth_service.register(email=email, password="WidgetStart123@")
    db.session.commit()
    user = user_repo.find_by_email(email)
    result = auth_service.login(email=email, password="WidgetStart123@")
    return user.id, result.token


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


@pytest.fixture(autouse=True)
def _reset_reader(app):
    # Inject a fresh in-memory rate-limiter per test. The app fixture is session
    # scoped AND prod uses Redis here, so a real counter keyed on 127.0.0.1
    # would accumulate across tests and trip the default widget_guest_start
    # quota. An in-memory backend keeps each test independent (mirrors the
    # existing start-conversation rate-limit spec).
    from plugins.meinchat.meinchat.services.rate_limiter import (
        InMemoryCounterBackend,
        RateLimiter,
    )

    original = getattr(app, "_meinchat_rate_limiter", None)
    app._meinchat_rate_limiter = RateLimiter(InMemoryCounterBackend())
    yield
    registry.reset_for_tests(ICmsWidgetReader)
    app._meinchat_rate_limiter = original


@pytest.mark.integration
def test_unknown_widget_returns_404(app, client):
    _install_reader({})
    resp = client.post(
        "/api/v1/messaging/widget/start", json={"widget_slug": "missing"}
    )
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "widget_not_found"


@pytest.mark.integration
def test_logged_in_without_auth_returns_401(app, client):
    _make_bot(app, "ws-bot1@example.com", "ws-assistant1")
    _install_reader(
        {
            "li-widget": {
                "visibility": "logged_in",
                "member_nicknames": ["ws-assistant1"],
            }
        }
    )
    resp = client.post(
        "/api/v1/messaging/widget/start", json={"widget_slug": "li-widget"}
    )
    assert resp.status_code == 401


@pytest.mark.integration
def test_logged_in_without_nickname_returns_409_nickname_required(app, client):
    caller_id, caller_token = _register(app, "ws-nonick@example.com")
    _make_bot(app, "ws-bot2@example.com", "ws-assistant2")
    _install_reader(
        {
            "li-widget2": {
                "visibility": "logged_in",
                "member_nicknames": ["ws-assistant2"],
            }
        }
    )
    resp = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "li-widget2"},
        headers=_auth(caller_token),
    )
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "nickname_required"


@pytest.mark.integration
def test_public_without_display_name_returns_400(app, client):
    _make_bot(app, "ws-bot3@example.com", "ws-assistant3")
    _install_reader(
        {
            "pub-widget": {
                "visibility": "public",
                "member_nicknames": ["ws-assistant3"],
            }
        }
    )
    resp = client.post(
        "/api/v1/messaging/widget/start", json={"widget_slug": "pub-widget"}
    )
    assert resp.status_code == 400


@pytest.mark.integration
def test_public_human_member_returns_409(app, client):
    human_id, _token = _register(app, "ws-human@example.com")
    _set_nickname(app, human_id, "ws-human")
    _install_reader(
        {
            "pub-human": {
                "visibility": "public",
                "member_nicknames": ["ws-human"],
            }
        }
    )
    resp = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "pub-human", "display_name": "Visitor"},
    )
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "public_human_member_not_allowed"


@pytest.mark.integration
def test_unknown_member_returns_404(app, client):
    _install_reader(
        {
            "pub-ghost": {
                "visibility": "public",
                "member_nicknames": ["ws-nobody"],
            }
        }
    )
    resp = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "pub-ghost", "display_name": "Visitor"},
    )
    assert resp.status_code == 404


@pytest.mark.integration
def test_public_start_provisions_guest_and_creates_room(app, client):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    _make_bot(app, "ws-bot-happy@example.com", "ws-assistant-happy")
    _install_reader(
        {
            "pub-happy": {
                "visibility": "public",
                "member_nicknames": ["ws-assistant-happy"],
            }
        }
    )

    resp = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "pub-happy", "display_name": "Jane Visitor"},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["access_token"]
    room_id = body["room_id"]
    self_nickname = body["self_nickname"]
    assert self_nickname.startswith("jane-visitor-")

    # The created user has role GUEST and cannot password-login.
    guest_member = next(m for m in body["members"] if m["is_admin"])
    assert guest_member["nickname"] == self_nickname
    bot_member = next(m for m in body["members"] if not m["is_admin"])
    assert bot_member["nickname"] == "ws-assistant-happy"

    # A guest-session row links guest ↔ widget ↔ room.
    from plugins.meinchat.meinchat.models.guest_session import (
        MeinchatGuestSession,
    )

    rows = (
        db.session.query(MeinchatGuestSession)
        .filter(MeinchatGuestSession.widget_slug == "pub-happy")
        .all()
    )
    assert len(rows) == 1
    assert str(rows[0].room_id) == room_id
    guest_user = UserRepository(db.session).find_by_id(rows[0].guest_user_id)
    assert guest_user.role == UserRole.GUEST

    # The guest cannot log in interactively (login guard rejects GUEST).
    login = app.container.auth_service().login(
        email=guest_user.email, password="whatever"
    )
    assert login.success is False

    # The guest JWT authorises a follow-up read on ITS OWN room.
    own = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "hi"},
        headers=_auth(body["access_token"]),
    )
    assert own.status_code == 201, own.get_data(as_text=True)


@pytest.mark.integration
def test_guest_jwt_is_403_on_a_foreign_room(app, client):
    _make_bot(app, "ws-bot-foreign@example.com", "ws-assistant-foreign")
    _install_reader(
        {
            "pub-foreign": {
                "visibility": "public",
                "member_nicknames": ["ws-assistant-foreign"],
            }
        }
    )
    first = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "pub-foreign", "display_name": "Guest One"},
    ).get_json()
    second = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "pub-foreign", "display_name": "Guest Two"},
    ).get_json()

    # Guest One's token must NOT be able to read Guest Two's room.
    resp = client.get(
        f"/api/v1/messaging/rooms/{second['room_id']}",
        headers=_auth(first["access_token"]),
    )
    assert resp.status_code == 403


@pytest.mark.integration
def test_logged_in_start_creates_room_with_caller_admin(app, client):
    caller_id, caller_token = _register(app, "ws-caller@example.com")
    _set_nickname(app, caller_id, "ws-caller")
    _make_bot(app, "ws-bot-li@example.com", "ws-assistant-li")
    _install_reader(
        {
            "li-happy": {
                "visibility": "logged_in",
                "member_nicknames": ["ws-assistant-li"],
            }
        }
    )
    resp = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "li-happy"},
        headers=_auth(caller_token),
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "access_token" not in body
    assert body["self_nickname"] == "ws-caller"
    admin = next(m for m in body["members"] if m["is_admin"])
    assert admin["nickname"] == "ws-caller"


@pytest.mark.integration
def test_public_path_consults_widget_guest_start_rate_limit(app, client, monkeypatch):
    """The public path must consult the `widget_guest_start` category. Drive the
    limit to zero so the second start is rejected with 429."""
    _make_bot(app, "ws-bot-rate@example.com", "ws-assistant-rate")
    _install_reader(
        {
            "pub-rate": {
                "visibility": "public",
                "member_nicknames": ["ws-assistant-rate"],
            }
        }
    )
    # Force the quota to one so the second start trips (the autouse fixture
    # already installed a fresh in-memory limiter for this test).
    config_store = app.config_store
    original = config_store.get_config("meinchat") or {}
    patched = {
        **original,
        "rate_widget_guest_start_per_window": 1,
        "rate_widget_guest_start_window_seconds": 3600,
    }
    monkeypatch.setattr(
        config_store,
        "get_config",
        lambda name: patched if name == "meinchat" else original,
    )

    first = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "pub-rate", "display_name": "Rate One"},
    )
    assert first.status_code == 201, first.get_data(as_text=True)
    second = client.post(
        "/api/v1/messaging/widget/start",
        json={"widget_slug": "pub-rate", "display_name": "Rate Two"},
    )
    assert second.status_code == 429
    assert second.headers.get("X-Rate-Limit-Category") == "widget_guest_start"
