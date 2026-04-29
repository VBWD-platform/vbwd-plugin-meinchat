"""Data access for message rows (paginated by sent_at cursor)."""
from typing import List, Optional
from uuid import UUID

from plugins.meinchat.meinchat.models.message import Message


class MessageRepository:
    def __init__(self, session) -> None:
        self._session = session

    def find_by_id(self, message_id) -> Optional[Message]:
        return self._session.query(Message).get(message_id)

    def page(
        self,
        conversation_id: UUID,
        *,
        before: Optional[UUID] = None,
        limit: int = 50,
    ) -> List[Message]:
        """Return up to `limit` messages, newest first. If `before` is set,
        returns messages older than that message's `sent_at`."""
        query = (
            self._session.query(Message)
            .filter(Message.conversation_id == conversation_id)
        )
        if before is not None:
            pivot = self._session.query(Message).get(before)
            if pivot is not None:
                query = query.filter(Message.sent_at < pivot.sent_at)
        return (
            query.order_by(Message.sent_at.desc())
            .limit(min(limit, 200))
            .all()
        )

    def save(self, row: Message) -> Message:
        self._session.add(row)
        self._session.flush()
        return row

    def delete(self, row: Message) -> None:
        self._session.delete(row)
        self._session.flush()
