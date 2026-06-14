"""Unit tests for WidgetStartService (S86.3, D2/D4/D5).

All collaborators are injected fakes — no DB, clean DI. Drives every branch of
the widget-start flow: the cms reader resolves (or doesn't) the server-trusted
config; logged_in vs public visibility; the D4 human-member gate; unknown
members; and the guest-session persistence side effect.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from vbwd.models.enums import UserRole

from plugins.meinchat.meinchat.extensibility.cms_widget_reader import (
    NullCmsWidgetReader,
)
from plugins.meinchat.meinchat.models.room_member import (
    ROOM_ROLE_ADMIN,
    ROOM_ROLE_MEMBER,
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


class _FakeWidgetReader:
    def __init__(self, config):
        self._config = config

    def get_active_widget_config(self, slug):
        return self._config


class _FakeRoomService:
    """Records create_room calls and returns a room whose members it can list."""

    def __init__(self):
        self.created = []
        self._members = []

    def create_room(self, creator_id, member_user_ids, *, widget_slug=None, **kwargs):
        self.created.append(
            {
                "creator_id": creator_id,
                "member_user_ids": list(member_user_ids),
                "widget_slug": widget_slug,
            }
        )
        room_id = uuid4()
        self._members = [SimpleNamespace(user_id=creator_id, role=ROOM_ROLE_ADMIN)] + [
            SimpleNamespace(user_id=uid, role=ROOM_ROLE_MEMBER)
            for uid in member_user_ids
        ]
        return SimpleNamespace(id=room_id)

    def members(self, room_id):
        return self._members


class _FakeGuestSessionService:
    def __init__(self, user_id, nickname, token):
        self._guest = SimpleNamespace(
            user_id=user_id, nickname=nickname, access_token=token
        )
        self.calls = []

    def provision(self, display_name):
        self.calls.append(display_name)
        return self._guest


class _FakeGuestSessionRepo:
    def __init__(self, existing=None):
        self.saved = []
        # Map (guest_user_id, widget_slug) -> existing session row.
        self._existing = dict(existing or {})

    def save(self, row):
        self.saved.append(row)
        return row

    def find_for_widget(self, guest_user_id, widget_slug):
        return self._existing.get((guest_user_id, widget_slug))


def _build_service(
    *,
    config,
    nickname_to_id=None,
    roles=None,
    nicknames=None,
    room_service=None,
    guest_service=None,
    guest_repo=None,
    grant_calls=None,
    guest_initial_tokens=20,
    economy_enabled=True,
    mint_guest_access_token=None,
):
    nickname_to_id = nickname_to_id or {}
    roles = roles or {}
    nicknames = nicknames or {}

    def grant_initial_tokens(user_id):
        if grant_calls is not None:
            grant_calls.append(user_id)

    return WidgetStartService(
        widget_reader=_FakeWidgetReader(config),
        resolve_nickname_to_user_id=lambda nick: nickname_to_id.get(nick),
        resolve_user_role=lambda uid: roles.get(uid),
        resolve_user_nickname=lambda uid: nicknames.get(uid),
        room_service=room_service or _FakeRoomService(),
        guest_session_service=guest_service,
        guest_session_repo=guest_repo or _FakeGuestSessionRepo(),
        guest_session_ttl_hours=24,
        grant_initial_tokens=grant_initial_tokens,
        guest_initial_tokens=guest_initial_tokens,
        economy_enabled=economy_enabled,
        mint_guest_access_token=mint_guest_access_token,
    )


def test_unknown_widget_raises_not_found():
    service = _build_service(config=None)
    with pytest.raises(WidgetNotFoundError):
        service.start("missing", display_name="Visitor", caller_user_id=None)


def test_null_reader_yields_widget_not_found():
    """Direct Liskov check: the registry default refuses every slug."""
    service = WidgetStartService(
        widget_reader=NullCmsWidgetReader(),
        resolve_nickname_to_user_id=lambda nick: None,
        resolve_user_role=lambda uid: None,
        resolve_user_nickname=lambda uid: None,
        room_service=_FakeRoomService(),
        guest_session_service=None,
        guest_session_repo=_FakeGuestSessionRepo(),
        guest_session_ttl_hours=24,
    )
    with pytest.raises(WidgetNotFoundError):
        service.start("any", display_name="x", caller_user_id=uuid4())


def test_logged_in_without_caller_raises_auth_required():
    service = _build_service(
        config={"visibility": "logged_in", "member_nicknames": ["assistant"]}
    )
    with pytest.raises(WidgetAuthRequiredError):
        service.start("w", display_name=None, caller_user_id=None)


def test_logged_in_caller_without_nickname_raises_nickname_required():
    caller_id = uuid4()
    service = _build_service(
        config={"visibility": "logged_in", "member_nicknames": ["assistant"]},
        nicknames={caller_id: None},
    )
    with pytest.raises(NicknameRequiredError):
        service.start("w", display_name=None, caller_user_id=caller_id)


def test_logged_in_unknown_member_raises_unknown_member():
    caller_id = uuid4()
    service = _build_service(
        config={"visibility": "logged_in", "member_nicknames": ["ghost"]},
        nicknames={caller_id: "caller"},
        nickname_to_id={},
    )
    with pytest.raises(UnknownMemberError):
        service.start("w", display_name=None, caller_user_id=caller_id)


def test_logged_in_happy_creates_room_with_caller_admin():
    caller_id = uuid4()
    bot_id = uuid4()
    room_service = _FakeRoomService()
    service = _build_service(
        config={"visibility": "logged_in", "member_nicknames": ["assistant"]},
        nicknames={caller_id: "caller", bot_id: "assistant"},
        nickname_to_id={"assistant": bot_id},
        roles={bot_id: UserRole.BOT},
        room_service=room_service,
    )
    result = service.start("w", display_name=None, caller_user_id=caller_id)

    assert result.access_token is None
    assert result.self_nickname == "caller"
    assert room_service.created[0]["creator_id"] == caller_id
    assert room_service.created[0]["member_user_ids"] == [bot_id]
    assert room_service.created[0]["widget_slug"] == "w"
    admin = next(m for m in result.members if m["nickname"] == "caller")
    assert admin["is_admin"] is True


def test_public_without_display_name_raises():
    service = _build_service(
        config={"visibility": "public", "member_nicknames": ["assistant"]}
    )
    with pytest.raises(DisplayNameRequiredError):
        service.start("w", display_name="  ", caller_user_id=None)


def test_public_human_member_is_rejected():
    human_id = uuid4()
    service = _build_service(
        config={"visibility": "public", "member_nicknames": ["realperson"]},
        nickname_to_id={"realperson": human_id},
        roles={human_id: UserRole.USER},
    )
    with pytest.raises(PublicHumanMemberError):
        service.start("w", display_name="Visitor", caller_user_id=None)


def test_public_unknown_member_raises():
    service = _build_service(
        config={"visibility": "public", "member_nicknames": ["ghost"]},
        nickname_to_id={},
    )
    with pytest.raises(UnknownMemberError):
        service.start("w", display_name="Visitor", caller_user_id=None)


def test_public_happy_provisions_guest_and_persists_session():
    bot_id = uuid4()
    guest_id = uuid4()
    room_service = _FakeRoomService()
    guest_service = _FakeGuestSessionService(guest_id, "visitor-ab12", "guest.jwt")
    guest_repo = _FakeGuestSessionRepo()
    service = _build_service(
        config={"visibility": "public", "member_nicknames": ["assistant"]},
        nickname_to_id={"assistant": bot_id},
        roles={bot_id: UserRole.BOT},
        nicknames={guest_id: "visitor-ab12", bot_id: "assistant"},
        room_service=room_service,
        guest_service=guest_service,
        guest_repo=guest_repo,
    )
    result = service.start("w", display_name="Visitor", caller_user_id=None)

    assert result.access_token == "guest.jwt"
    assert result.self_nickname == "visitor-ab12"
    assert guest_service.calls == ["Visitor"]
    # Room created with the GUEST as creator (admin) + the bot member.
    assert room_service.created[0]["creator_id"] == guest_id
    assert room_service.created[0]["member_user_ids"] == [bot_id]
    # A guest-session row was persisted linking guest ↔ widget ↔ room.
    assert len(guest_repo.saved) == 1
    saved = guest_repo.saved[0]
    assert saved.guest_user_id == guest_id
    assert saved.widget_slug == "w"
    assert saved.room_id == result.room_id
    assert saved.display_name == "Visitor"


# ── D11: grant on activation ────────────────────────────────────────────────


def _public_service_with_grant(grant_calls, *, economy_enabled=True, guest_repo=None):
    bot_id = uuid4()
    guest_id = uuid4()
    guest_service = _FakeGuestSessionService(guest_id, "visitor-ab12", "guest.jwt")
    service = _build_service(
        config={"visibility": "public", "member_nicknames": ["assistant"]},
        nickname_to_id={"assistant": bot_id},
        roles={bot_id: UserRole.BOT, guest_id: UserRole.GUEST},
        nicknames={guest_id: "visitor-ab12", bot_id: "assistant"},
        guest_service=guest_service,
        guest_repo=guest_repo,
        grant_calls=grant_calls,
        economy_enabled=economy_enabled,
    )
    return service, guest_id


def test_new_public_start_grants_initial_tokens_to_the_guest():
    grant_calls = []
    service, guest_id = _public_service_with_grant(grant_calls)
    service.start("w", display_name="Visitor", caller_user_id=None)
    assert grant_calls == [guest_id]


def test_economy_disabled_grants_nothing():
    grant_calls = []
    service, _guest_id = _public_service_with_grant(grant_calls, economy_enabled=False)
    service.start("w", display_name="Visitor", caller_user_id=None)
    assert grant_calls == []


# ── D12: session reuse (no new provision, no re-grant) ──────────────────────


def test_presenting_a_valid_guest_session_reuses_room_without_regrant():
    bot_id = uuid4()
    returning_guest_id = uuid4()
    existing_room_id = uuid4()
    existing_session = SimpleNamespace(
        guest_user_id=returning_guest_id,
        widget_slug="w",
        room_id=existing_room_id,
    )
    guest_repo = _FakeGuestSessionRepo(
        existing={(returning_guest_id, "w"): existing_session}
    )
    room_service = _FakeRoomService()
    guest_service = _FakeGuestSessionService(uuid4(), "should-not-be-used", "nope")
    grant_calls = []
    mint_calls = []

    def mint_guest_access_token(user_id):
        mint_calls.append(user_id)
        return "reused-guest.jwt"

    service = _build_service(
        config={"visibility": "public", "member_nicknames": ["assistant"]},
        nickname_to_id={"assistant": bot_id},
        roles={bot_id: UserRole.BOT, returning_guest_id: UserRole.GUEST},
        nicknames={returning_guest_id: "returning-guest", bot_id: "assistant"},
        room_service=room_service,
        guest_service=guest_service,
        guest_repo=guest_repo,
        grant_calls=grant_calls,
        mint_guest_access_token=mint_guest_access_token,
    )

    result = service.start(
        "w",
        display_name="Visitor",
        caller_user_id=None,
        presented_guest_user_id=returning_guest_id,
    )

    # Reuse: the EXISTING room is returned, no new guest provisioned, no re-grant.
    assert result.room_id == existing_room_id
    assert result.self_nickname == "returning-guest"
    assert guest_service.calls == []
    assert room_service.created == []
    assert grant_calls == []
    assert guest_repo.saved == []
    # The bug fix: a returning guest gets a usable re-minted token so the FE
    # never has to fall back to the app session for the guest's room calls (D12).
    assert mint_calls == [returning_guest_id]
    assert result.access_token == "reused-guest.jwt"


def test_reuse_without_a_minter_returns_no_token_but_still_reuses_room():
    """Liskov / DI default: the minter is optional. Without it the reuse path
    still returns the existing room (it just cannot re-mint a token)."""
    bot_id = uuid4()
    returning_guest_id = uuid4()
    existing_room_id = uuid4()
    existing_session = SimpleNamespace(
        guest_user_id=returning_guest_id,
        widget_slug="w",
        room_id=existing_room_id,
    )
    guest_repo = _FakeGuestSessionRepo(
        existing={(returning_guest_id, "w"): existing_session}
    )
    service = _build_service(
        config={"visibility": "public", "member_nicknames": ["assistant"]},
        nickname_to_id={"assistant": bot_id},
        roles={bot_id: UserRole.BOT, returning_guest_id: UserRole.GUEST},
        nicknames={returning_guest_id: "returning-guest", bot_id: "assistant"},
        guest_repo=guest_repo,
        mint_guest_access_token=None,
    )

    result = service.start(
        "w",
        display_name="Visitor",
        caller_user_id=None,
        presented_guest_user_id=returning_guest_id,
    )

    assert result.room_id == existing_room_id
    assert result.access_token is None


def test_unknown_presented_guest_falls_back_to_fresh_provision_and_grant():
    grant_calls = []
    service, guest_id = _public_service_with_grant(grant_calls)
    # A presented id with no matching guest-session row → provision fresh.
    result = service.start(
        "w",
        display_name="Visitor",
        caller_user_id=None,
        presented_guest_user_id=uuid4(),
    )
    assert result.access_token == "guest.jwt"
    assert grant_calls == [guest_id]
