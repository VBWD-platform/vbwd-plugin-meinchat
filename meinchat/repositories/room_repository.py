"""Data access for room rows (S86.1)."""
from typing import List, Optional
from uuid import UUID

from plugins.meinchat.meinchat.models.room import Room
from plugins.meinchat.meinchat.models.room_member import RoomMember


class RoomRepository:
    """Lookup by id + the caller's room inbox list."""

    def __init__(self, session) -> None:
        self._session = session

    def find_by_id(self, room_id) -> Optional[Room]:
        return self._session.get(Room, room_id)

    def list_for_user(self, user_id: UUID) -> List[Room]:
        return (
            self._session.query(Room)
            .join(RoomMember, RoomMember.room_id == Room.id)
            .filter(RoomMember.user_id == user_id)
            .order_by(Room.last_message_at.desc().nullslast())
            .all()
        )

    def save(self, row: Room) -> Room:
        self._session.add(row)
        self._session.flush()
        return row
