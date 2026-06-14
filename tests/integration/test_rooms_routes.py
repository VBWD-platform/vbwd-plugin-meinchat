"""S86.1 Slice 1b — room HTTP routes against real Postgres + the Flask client.

Drives the membership-gated `/api/v1/messaging/rooms*` surface end-to-end:
create → send → list → per-member read; non-member 403s; any member can
invite; non-admin remove fails; unknown nickname 404s; a bot member pins
`plain`. Users + nicknames are created through the service layer (no raw
INSERT), per feedback_no_direct_db_for_test_data.
"""
from __future__ import annotations

import pytest

from vbwd.models.enums import UserRole


def _register(app, email):
    """Register a user, return (user_id, bearer_token)."""
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    user_repo = UserRepository(db.session)
    auth_service = app.container.auth_service()
    existing = user_repo.find_by_email(email)
    if existing is None:
        result = auth_service.register(email=email, password="RoomRoute123@")
        db.session.commit()
        return existing_id_and_token(app, email, result.token)
    # Re-login to mint a fresh token for an already-seeded user.
    result = auth_service.login(email=email, password="RoomRoute123@")
    return existing.id, result.token


def existing_id_and_token(app, email, token):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    user = UserRepository(db.session).find_by_email(email)
    return user.id, token


def _set_nickname(app, user_id, nickname):
    from vbwd.extensions import db
    from plugins.meinchat.meinchat.models.user_nickname import UserNickname
    from plugins.meinchat.meinchat.repositories.nickname_repository import (
        NicknameRepository,
    )

    repo = NicknameRepository(db.session)
    existing = repo.find_by_user_id(user_id)
    if existing is not None:
        return existing.nickname
    row = UserNickname()
    row.user_id = user_id
    row.nickname = nickname
    db.session.add(row)
    db.session.commit()
    return nickname


def _set_bot_role(app, user_id):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    repo = UserRepository(db.session)
    user = repo.find_by_id(user_id)
    user.role = UserRole.BOT
    db.session.commit()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.integration
def test_three_member_room_create_send_list_read(app, client):
    creator_id, creator_token = _register(app, "rt-creator@example.com")
    alice_id, alice_token = _register(app, "rt-alice@example.com")
    bob_id, bob_token = _register(app, "rt-bob@example.com")
    _set_nickname(app, creator_id, "rt-creator")
    _set_nickname(app, alice_id, "rt-alice")
    _set_nickname(app, bob_id, "rt-bob")

    create = client.post(
        "/api/v1/messaging/rooms",
        json={"member_nicknames": ["rt-alice", "rt-bob"], "name": "trio"},
        headers=_auth(creator_token),
    )
    assert create.status_code == 201, create.get_data(as_text=True)
    room = create.get_json()
    room_id = room["id"]
    assert room["protocol"] == "plain"
    assert room["name"] == "trio"

    send = client.post(
        f"/api/v1/messaging/rooms/{room_id}/messages",
        json={"body": "hello room"},
        headers=_auth(creator_token),
    )
    assert send.status_code == 201, send.get_data(as_text=True)
    assert send.get_json()["room_id"] == room_id

    listing = client.get(
        f"/api/v1/messaging/rooms/{room_id}/messages", headers=_auth(alice_token)
    )
    assert listing.status_code == 200
    assert [m["body"] for m in listing.get_json()["items"]] == ["hello room"]

    # Each member sees their own unread before reading.
    alice_room = client.get(
        f"/api/v1/messaging/rooms/{room_id}", headers=_auth(alice_token)
    ).get_json()
    assert alice_room["unread_count"] == 1

    read = client.post(
        f"/api/v1/messaging/rooms/{room_id}/read", headers=_auth(alice_token)
    )
    assert read.status_code == 204

    alice_after = client.get(
        f"/api/v1/messaging/rooms/{room_id}", headers=_auth(alice_token)
    ).get_json()
    assert alice_after["unread_count"] == 0
    # Bob still has his own unread (per-member accounting).
    bob_room = client.get(
        f"/api/v1/messaging/rooms/{room_id}", headers=_auth(bob_token)
    ).get_json()
    assert bob_room["unread_count"] == 1

    # The room shows up in each member's inbox list.
    mine = client.get("/api/v1/messaging/rooms", headers=_auth(bob_token))
    assert any(item["id"] == room_id for item in mine.get_json()["items"])


@pytest.mark.integration
def test_non_member_is_forbidden_on_every_room_route(app, client):
    creator_id, creator_token = _register(app, "rt-owner@example.com")
    member_id, _member_token = _register(app, "rt-member@example.com")
    stranger_id, stranger_token = _register(app, "rt-stranger@example.com")
    _set_nickname(app, member_id, "rt-member")
    _set_nickname(app, stranger_id, "rt-stranger")

    room_id = client.post(
        "/api/v1/messaging/rooms",
        json={"member_nicknames": ["rt-member"]},
        headers=_auth(creator_token),
    ).get_json()["id"]

    forbidden_calls = [
        ("get", f"/api/v1/messaging/rooms/{room_id}", None),
        ("get", f"/api/v1/messaging/rooms/{room_id}/members", None),
        ("get", f"/api/v1/messaging/rooms/{room_id}/messages", None),
        ("post", f"/api/v1/messaging/rooms/{room_id}/messages", {"body": "x"}),
        ("post", f"/api/v1/messaging/rooms/{room_id}/read", None),
        (
            "post",
            f"/api/v1/messaging/rooms/{room_id}/invite",
            {"nickname": "rt-member"},
        ),
    ]
    for method, path, body in forbidden_calls:
        response = getattr(client, method)(
            path, json=body, headers=_auth(stranger_token)
        )
        assert response.status_code == 403, f"{method} {path} -> {response.status_code}"


@pytest.mark.integration
def test_any_member_can_invite_but_non_admin_cannot_remove(app, client):
    creator_id, creator_token = _register(app, "rt-c2@example.com")
    member_id, member_token = _register(app, "rt-m2@example.com")
    invitee_id, _invitee_token = _register(app, "rt-i2@example.com")
    _set_nickname(app, member_id, "rt-m2")
    _set_nickname(app, invitee_id, "rt-i2")

    room_id = client.post(
        "/api/v1/messaging/rooms",
        json={"member_nicknames": ["rt-m2"]},
        headers=_auth(creator_token),
    ).get_json()["id"]

    # A plain member invites a third user — allowed.
    invite = client.post(
        f"/api/v1/messaging/rooms/{room_id}/invite",
        json={"nickname": "rt-i2"},
        headers=_auth(member_token),
    )
    assert invite.status_code == 201, invite.get_data(as_text=True)

    members = client.get(
        f"/api/v1/messaging/rooms/{room_id}/members", headers=_auth(member_token)
    ).get_json()["items"]
    nicknames = {m["nickname"] for m in members}
    assert {"rt-m2", "rt-i2"} <= nicknames

    # A plain member tries to remove the invitee — forbidden (admin only).
    remove = client.delete(
        f"/api/v1/messaging/rooms/{room_id}/members/{invitee_id}",
        headers=_auth(member_token),
    )
    assert remove.status_code == 403


@pytest.mark.integration
def test_unknown_nickname_on_create_and_invite_returns_404(app, client):
    creator_id, creator_token = _register(app, "rt-c3@example.com")
    member_id, _member_token = _register(app, "rt-m3@example.com")
    _set_nickname(app, member_id, "rt-m3")

    create = client.post(
        "/api/v1/messaging/rooms",
        json={"member_nicknames": ["does-not-exist"]},
        headers=_auth(creator_token),
    )
    assert create.status_code == 404

    room_id = client.post(
        "/api/v1/messaging/rooms",
        json={"member_nicknames": ["rt-m3"]},
        headers=_auth(creator_token),
    ).get_json()["id"]
    invite = client.post(
        f"/api/v1/messaging/rooms/{room_id}/invite",
        json={"nickname": "ghost-nick"},
        headers=_auth(creator_token),
    )
    assert invite.status_code == 404


@pytest.mark.integration
def test_create_pins_plain_when_a_bot_is_a_member(app, client):
    """A bot room that ACCEPTS plain pins plain (the realistic widget case).
    Defaulted/plain-fallback requests are never vetoed (D3)."""
    creator_id, creator_token = _register(app, "rt-c4@example.com")
    bot_id, _bot_token = _register(app, "rt-bot4@example.com")
    _set_nickname(app, bot_id, "rt-bot4")
    _set_bot_role(app, bot_id)

    # No accepted_protocols → plain is acceptable → pins plain.
    create = client.post(
        "/api/v1/messaging/rooms",
        json={"member_nicknames": ["rt-bot4"]},
        headers=_auth(creator_token),
    )
    assert create.status_code == 201, create.get_data(as_text=True)
    assert create.get_json()["protocol"] == "plain"

    # e2e listed alongside plain is still fine — plain is an acceptable
    # fallback, so the bot room downgrades to plain (no veto).
    create_fallback = client.post(
        "/api/v1/messaging/rooms",
        json={
            "member_nicknames": ["rt-bot4"],
            "accepted_protocols": ["e2e_v1", "plain"],
        },
        headers=_auth(creator_token),
    )
    assert create_fallback.status_code == 201, create_fallback.get_data(as_text=True)
    assert create_fallback.get_json()["protocol"] == "plain"


@pytest.mark.integration
def test_e2e_only_room_with_a_bot_member_is_vetoed(app, client):
    """S86.2 D3: an *e2e-required* room (no plain fallback) with a bot member is
    vetoed with a clear error rather than silently downgraded to plain — the
    privacy foot-gun the 1:1 path already refuses (requires meinchat_plus, which
    registers AllMembersHaveDeviceKeys)."""
    creator_id, creator_token = _register(app, "rt-c5@example.com")
    bot_id, _bot_token = _register(app, "rt-bot5@example.com")
    _set_nickname(app, bot_id, "rt-bot5")
    _set_bot_role(app, bot_id)

    create = client.post(
        "/api/v1/messaging/rooms",
        json={"member_nicknames": ["rt-bot5"], "accepted_protocols": ["e2e_v1"]},
        headers=_auth(creator_token),
    )
    assert create.status_code == 409, create.get_data(as_text=True)
    assert create.get_json()["code"] == "bot_member_cannot_e2e"
