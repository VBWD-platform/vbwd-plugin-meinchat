"""Business logic for nicknames: set, change, search, lookup, ban reclaim."""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from plugins.meinchat.meinchat.models.user_nickname import UserNickname
from plugins.meinchat.meinchat.repositories.nickname_repository import (
    NicknameRepository,
)
from plugins.meinchat.meinchat.services.slug_validator import validate_nickname


class NicknameTakenError(Exception):
    """The requested nickname is already owned by another user."""


class NicknameBannedError(Exception):
    """The nickname is currently banned; grace period not yet elapsed."""


class NicknameNotFoundError(Exception):
    """No visible nickname matches (unknown, banned, or search_hidden)."""


class NicknameService:
    """One user ↔ one nickname, keyed by case-insensitive slug."""

    def __init__(
        self,
        repo: NicknameRepository,
        ban_grace_period_days: int = 30,
    ) -> None:
        self._repo = repo
        self._ban_grace_period = timedelta(days=ban_grace_period_days)

    # ── write path ──────────────────────────────────────────────────────────

    def set_nickname(self, user_id: UUID, nickname: str) -> UserNickname:
        """Set or change the caller's nickname.

        Decision Q1 — free, no cooldown, no token cost.
        Decision Q5 — banned slugs reclaimable after grace period: if the
        slug is taken by a banned row past its grace window, delete that
        row before assigning.
        """
        validate_nickname(nickname)

        conflicting = self._repo.find_by_nickname_ci(nickname)
        if conflicting is not None and conflicting.user_id != user_id:
            if conflicting.banned and self._ban_grace_elapsed(conflicting):
                self._repo.delete(conflicting)
            else:
                if conflicting.banned:
                    raise NicknameBannedError(
                        f"'{nickname}' is banned and not yet reclaimable"
                    )
                raise NicknameTakenError(f"'{nickname}' is already taken")

        existing = self._repo.find_by_user_id(user_id)
        if existing is not None:
            existing.nickname = nickname
            existing.set_at = datetime.now(timezone.utc)
            return self._repo.save(existing)

        row = UserNickname()
        row.user_id = user_id
        row.nickname = nickname
        row.set_at = datetime.now(timezone.utc)
        row.banned = False
        row.search_hidden = False
        return self._repo.save(row)

    # ── read paths ──────────────────────────────────────────────────────────

    def get_mine(self, user_id: UUID) -> Optional[UserNickname]:
        return self._repo.find_by_user_id(user_id)

    def search(
        self,
        prefix: str,
        caller_user_id: UUID,
        limit: int = 10,
    ) -> List[UserNickname]:
        return self._repo.search_prefix(
            prefix, exclude_user_id=caller_user_id, limit=limit
        )

    def get_card(self, nickname: str) -> Dict[str, Any]:
        row = self._repo.find_by_nickname_ci(nickname)
        if row is None or row.banned or row.search_hidden:
            raise NicknameNotFoundError(f"'{nickname}' not found")
        return {
            "user_id": str(row.user_id),
            "nickname": row.nickname,
            "joined_at": row.set_at.isoformat() if row.set_at else None,
        }

    # ── admin ──────────────────────────────────────────────────────────────

    def ban(self, user_id: UUID) -> UserNickname:
        row = self._repo.find_by_user_id(user_id)
        if row is None:
            raise NicknameNotFoundError(f"user {user_id} has no nickname")
        row.banned = True
        row.banned_at = datetime.now(timezone.utc)
        return self._repo.save(row)

    def unban(self, user_id: UUID) -> UserNickname:
        row = self._repo.find_by_user_id(user_id)
        if row is None:
            raise NicknameNotFoundError(f"user {user_id} has no nickname")
        row.banned = False
        row.banned_at = None
        return self._repo.save(row)

    # ── private ────────────────────────────────────────────────────────────

    def _ban_grace_elapsed(self, row: UserNickname) -> bool:
        if row.banned_at is None:
            return False
        banned_at = row.banned_at
        if banned_at.tzinfo is None:
            banned_at = banned_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - banned_at > self._ban_grace_period
