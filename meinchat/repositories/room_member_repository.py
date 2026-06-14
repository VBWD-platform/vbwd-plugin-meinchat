"""Data access for room membership rows (S86.1)."""
from typing import List, Optional
from uuid import UUID

from plugins.meinchat.meinchat.models.room_member import RoomMember


class RoomMemberRepository:
    """Membership lookups + per-room roster."""

    def __init__(self, session) -> None:
        self._session = session

    def find(self, room_id: UUID, user_id: UUID) -> Optional[RoomMember]:
        return (
            self._session.query(RoomMember)
            .filter(RoomMember.room_id == room_id)
            .filter(RoomMember.user_id == user_id)
            .one_or_none()
        )

    def list_for_room(self, room_id: UUID) -> List[RoomMember]:
        return (
            self._session.query(RoomMember)
            .filter(RoomMember.room_id == room_id)
            .order_by(RoomMember.joined_at.asc())
            .all()
        )

    def save(self, row: RoomMember) -> RoomMember:
        self._session.add(row)
        self._session.flush()
        return row

    def delete(self, row: RoomMember) -> None:
        self._session.delete(row)
        self._session.flush()
