"""Data access for message rows (paginated by sent_at cursor)."""
from datetime import datetime
from typing import List, Optional, Sequence
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
        return self._page(Message.conversation_id == conversation_id, before, limit)

    def page_room(
        self,
        room_id: UUID,
        *,
        before: Optional[UUID] = None,
        limit: int = 50,
    ) -> List[Message]:
        """Room sibling of `page` — same cursor pagination, parent = room_id
        (S86.1 D2). One pagination implementation, two parent filters (DRY)."""
        return self._page(Message.room_id == room_id, before, limit)

    def _page(self, parent_filter, before: Optional[UUID], limit: int) -> List[Message]:
        query = self._session.query(Message).filter(parent_filter)
        if before is not None:
            pivot = self._session.query(Message).get(before)
            if pivot is not None:
                query = query.filter(Message.sent_at < pivot.sent_at)
        return query.order_by(Message.sent_at.desc()).limit(min(limit, 200)).all()

    def save(self, row: Message) -> Message:
        self._session.add(row)
        self._session.flush()
        return row

    def delete(self, row: Message) -> None:
        self._session.delete(row)
        self._session.flush()

    # ── retention prune (S28.1) ─────────────────────────────────────────────

    def find_older_than(self, threshold: datetime) -> List[Message]:
        """Return all message rows whose `sent_at` is strictly older than
        `threshold` (the retention candidate set for the prune)."""
        return self._session.query(Message).filter(Message.sent_at < threshold).all()

    def delete_by_ids(self, ids: Sequence[UUID]) -> List[UUID]:
        """Hard-delete the given rows via `DELETE … RETURNING id`. Returns the
        ids actually removed so attachment cleanup can fan out. No-op (empty
        list) when `ids` is empty — keeps the prune idempotent."""
        if not ids:
            return []
        deleted = (
            self._session.query(Message.id).filter(Message.id.in_(list(ids))).all()
        )
        deleted_ids = [row_id for (row_id,) in deleted]
        if deleted_ids:
            self._session.query(Message).filter(Message.id.in_(deleted_ids)).delete(
                synchronize_session=False
            )
            self._session.flush()
        return deleted_ids
