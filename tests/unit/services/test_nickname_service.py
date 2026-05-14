"""Tests for NicknameService — set/change, search, card lookup, ban reclaim."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from plugins.meinchat.meinchat.services.nickname_service import (
    NicknameBannedError,
    NicknameNotFoundError,
    NicknameService,
    NicknameTakenError,
)
from plugins.meinchat.meinchat.services.slug_validator import NicknameInvalidError


@pytest.fixture
def repo():
    r = MagicMock()
    r.find_by_nickname_ci.return_value = None
    r.find_by_user_id.return_value = None
    r.save.side_effect = lambda row: row
    return r


@pytest.fixture
def service(repo):
    return NicknameService(repo=repo, ban_grace_period_days=30)


def _nickname_row(
    user_id, nickname, *, banned=False, banned_at=None, search_hidden=False
):
    from plugins.meinchat.meinchat.models.user_nickname import UserNickname

    row = UserNickname()
    row.id = uuid4()
    row.user_id = user_id
    row.nickname = nickname
    row.nickname_ci = nickname.lower()
    row.set_at = datetime.now(timezone.utc)
    row.banned = banned
    row.banned_at = banned_at
    row.search_hidden = search_hidden
    return row


class TestSetNickname:
    def test_sets_nickname_for_new_user(self, service, repo):
        user_id = uuid4()

        result = service.set_nickname(user_id, "alice")

        assert result.nickname == "alice"
        repo.save.assert_called()

    def test_rejects_invalid_nickname(self, service):
        with pytest.raises(NicknameInvalidError):
            service.set_nickname(uuid4(), "Alice")  # uppercase → invalid

    def test_rejects_taken_nickname(self, service, repo):
        other_user_id = uuid4()
        repo.find_by_nickname_ci.return_value = _nickname_row(other_user_id, "alice")

        with pytest.raises(NicknameTakenError):
            service.set_nickname(uuid4(), "alice")

    def test_updates_existing_row_when_same_user_renames(self, service, repo):
        user_id = uuid4()
        existing = _nickname_row(user_id, "alice")
        repo.find_by_user_id.return_value = existing

        result = service.set_nickname(user_id, "alice_v2")

        assert result.nickname == "alice_v2"
        # No new row — the same existing row is mutated + saved.
        assert result is existing

    def test_free_no_cooldown_back_to_back_rename_allowed(self, service, repo):
        """Decision Q1: free, no cooldown."""
        user_id = uuid4()
        existing = _nickname_row(user_id, "alice")
        repo.find_by_user_id.return_value = existing

        service.set_nickname(user_id, "alice_v2")
        service.set_nickname(user_id, "alice_v3")  # immediately again, no error

        assert existing.nickname == "alice_v3"


class TestBanGraceReclaim:
    def test_recent_ban_blocks_reclaim(self, service, repo):
        """A banned slug within grace period cannot be claimed."""
        recently_banned = _nickname_row(
            uuid4(),
            "spammer",
            banned=True,
            banned_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        repo.find_by_nickname_ci.return_value = recently_banned

        with pytest.raises(NicknameBannedError):
            service.set_nickname(uuid4(), "spammer")

    def test_expired_ban_releases_slug_and_deletes_old_row(self, service, repo):
        """Past-grace banned row is deleted lazily; new user claims the slug."""
        expired = _nickname_row(
            uuid4(),
            "spammer",
            banned=True,
            banned_at=datetime.now(timezone.utc) - timedelta(days=31),
        )
        repo.find_by_nickname_ci.return_value = expired

        new_user_id = uuid4()
        result = service.set_nickname(new_user_id, "spammer")

        assert result.nickname == "spammer"
        assert result.user_id == new_user_id
        repo.delete.assert_called_once_with(expired)

    def test_grace_period_is_configurable(self, repo):
        """A plugin-config override shortens or lengthens the grace window."""
        service = NicknameService(repo=repo, ban_grace_period_days=7)
        expired_8d = _nickname_row(
            uuid4(),
            "spammer",
            banned=True,
            banned_at=datetime.now(timezone.utc) - timedelta(days=8),
        )
        repo.find_by_nickname_ci.return_value = expired_8d

        # 7-day window → 8-day-old ban is expired → reclaim works.
        result = service.set_nickname(uuid4(), "spammer")
        assert result.nickname == "spammer"


class TestSearchNicknames:
    def test_delegates_to_repo(self, service, repo):
        caller_id = uuid4()
        repo.search_prefix.return_value = [_nickname_row(uuid4(), "alice")]

        results = service.search("ali", caller_user_id=caller_id)

        assert len(results) == 1
        repo.search_prefix.assert_called_once_with(
            "ali", exclude_user_id=caller_id, limit=10
        )


class TestGetCard:
    def test_returns_card_dict(self, service, repo):
        row = _nickname_row(uuid4(), "alice")
        repo.find_by_nickname_ci.return_value = row

        card = service.get_card("alice")

        assert card["nickname"] == "alice"
        assert card["user_id"] == str(row.user_id)

    def test_unknown_nickname_raises_not_found(self, service, repo):
        repo.find_by_nickname_ci.return_value = None
        with pytest.raises(NicknameNotFoundError):
            service.get_card("ghost")

    def test_banned_nickname_raises_not_found(self, service, repo):
        row = _nickname_row(uuid4(), "alice", banned=True)
        repo.find_by_nickname_ci.return_value = row
        with pytest.raises(NicknameNotFoundError):
            service.get_card("alice")

    def test_hidden_nickname_raises_not_found(self, service, repo):
        row = _nickname_row(uuid4(), "alice", search_hidden=True)
        repo.find_by_nickname_ci.return_value = row
        with pytest.raises(NicknameNotFoundError):
            service.get_card("alice")
