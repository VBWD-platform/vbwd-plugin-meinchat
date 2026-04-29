"""Data access for conversation rows."""
from typing import List, Optional
from uuid import UUID

from plugins.meinchat.meinchat.models.conversation import Conversation


class ConversationRepository:
    """Pair-keyed lookup + caller's inbox list."""

    def __init__(self, session) -> None:
        self._session = session

    def find_by_id(self, conv_id) -> Optional[Conversation]:
        return self._session.query(Conversation).get(conv_id)

    def find_by_pair(self, low_id, high_id) -> Optional[Conversation]:
        return (
            self._session.query(Conversation)
            .filter(Conversation.participant_low_id == low_id)
            .filter(Conversation.participant_high_id == high_id)
            .one_or_none()
        )

    def list_for_user(self, user_id: UUID) -> List[Conversation]:
        return (
            self._session.query(Conversation)
            .filter(
                (Conversation.participant_low_id == user_id)
                | (Conversation.participant_high_id == user_id)
            )
            .order_by(Conversation.last_message_at.desc().nullslast())
            .all()
        )

    def save(self, row: Conversation) -> Conversation:
        self._session.add(row)
        self._session.flush()
        return row
