"""Data access for user_nickname rows."""
from typing import List, Optional
from uuid import UUID

from plugins.meinchat.meinchat.models.user_nickname import UserNickname


class NicknameRepository:
    """Thin wrapper over the SQLAlchemy session for UserNickname."""

    def __init__(self, session) -> None:
        self._session = session

    def find_by_user_id(self, user_id) -> Optional[UserNickname]:
        return (
            self._session.query(UserNickname)
            .filter(UserNickname.user_id == user_id)
            .one_or_none()
        )

    def find_by_nickname_ci(self, nickname: str) -> Optional[UserNickname]:
        return (
            self._session.query(UserNickname)
            .filter(UserNickname.nickname_ci == nickname.lower())
            .one_or_none()
        )

    def search_prefix(
        self,
        prefix: str,
        exclude_user_id: Optional[UUID] = None,
        limit: int = 10,
    ) -> List[UserNickname]:
        """Case-insensitive prefix search.

        Hits the `text_pattern_ops` index on nickname_ci. Callers filter
        out banned / search_hidden here so the service layer stays thin.
        """
        query = (
            self._session.query(UserNickname)
            .filter(UserNickname.nickname_ci.like(f"{prefix.lower()}%"))
            .filter(UserNickname.banned.is_(False))
            .filter(UserNickname.search_hidden.is_(False))
        )
        if exclude_user_id is not None:
            query = query.filter(UserNickname.user_id != exclude_user_id)
        return query.order_by(UserNickname.nickname_ci).limit(limit).all()

    def list_paged(
        self,
        *,
        page: int = 1,
        per_page: int = 50,
        query: Optional[str] = None,
    ) -> dict:
        """Admin-side paged listing — includes banned + search_hidden rows
        (the user-facing prefix search hides them; admin inspector must
        see them to ban/unban). Optional `query` does a substring filter
        on `nickname_ci`."""
        from sqlalchemy import func

        base = self._session.query(UserNickname)
        if query:
            base = base.filter(UserNickname.nickname_ci.like(f"%{query.lower()}%"))
        total = base.with_entities(func.count(UserNickname.id)).scalar() or 0
        rows = (
            base.order_by(UserNickname.nickname_ci)
            .offset(max(0, (page - 1)) * per_page)
            .limit(per_page)
            .all()
        )
        return {
            "items": rows,
            "page": page,
            "per_page": per_page,
            "total": int(total),
        }

    def save(self, row: UserNickname) -> UserNickname:
        self._session.add(row)
        self._session.flush()
        return row

    def delete(self, row: UserNickname) -> None:
        self._session.delete(row)
        self._session.flush()
