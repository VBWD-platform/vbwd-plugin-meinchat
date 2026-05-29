"""Business logic for 1-on-1 conversations.

Pair uniqueness is enforced at the DB level via a unique constraint on
(participant_low_id, participant_high_id) plus a check that low < high.
Here we normalise the caller's (user_a, user_b) pair into the same (low,
high) order before any lookup or insert, so A↔B and B↔A always resolve
to the same row.
"""
from typing import Optional, Tuple
from uuid import UUID

from plugins.meinchat.meinchat.models.conversation import Conversation
from plugins.meinchat.meinchat.repositories.conversation_repository import (
    ConversationRepository,
)


class SelfConversationError(Exception):
    """user cannot start a conversation with themselves."""


def order_pair(user_a: UUID, user_b: UUID) -> Tuple[UUID, UUID]:
    """Return (low, high) in UUID byte order. Raise on equal ids."""
    if user_a == user_b:
        raise SelfConversationError("cannot start a conversation with yourself")
    return (user_a, user_b) if user_a < user_b else (user_b, user_a)


class ConversationService:
    def __init__(self, repo: ConversationRepository) -> None:
        self._repo = repo

    # ── write path ──────────────────────────────────────────────────────────

    def start_or_get(self, user_a: UUID, user_b: UUID) -> Conversation:
        low, high = order_pair(user_a, user_b)
        existing = self._repo.find_by_pair(low, high)
        if existing is not None:
            return existing

        conv = Conversation()
        conv.participant_low_id = low
        conv.participant_high_id = high
        conv.unread_low_count = 0
        conv.unread_high_count = 0
        return self._repo.save(conv)

    # ── read helpers ────────────────────────────────────────────────────────

    def find_between(self, user_a: UUID, user_b: UUID) -> Optional[Conversation]:
        # Lookup-only sibling of start_or_get. Used by the start-conversation
        # route to short-circuit the "open existing chat" path so it never
        # touches the new_conversation rate-limit counter.
        low, high = order_pair(user_a, user_b)
        return self._repo.find_by_pair(low, high)

    def get_by_id(self, conv_id: UUID) -> Conversation:
        return self._repo.find_by_id(conv_id)

    def list_for_user(self, user_id: UUID):
        return self._repo.list_for_user(user_id)

    # ── unread accounting ───────────────────────────────────────────────────

    @staticmethod
    def is_member(user_id: UUID, conv: Conversation) -> bool:
        return user_id in (conv.participant_low_id, conv.participant_high_id)

    @staticmethod
    def peer_of(user_id: UUID, conv: Conversation) -> UUID:
        if user_id == conv.participant_low_id:
            return conv.participant_high_id
        return conv.participant_low_id

    @staticmethod
    def unread_for(user_id: UUID, conv: Conversation) -> int:
        if user_id == conv.participant_low_id:
            return conv.unread_low_count
        return conv.unread_high_count

    @staticmethod
    def increment_unread_for(user_id: UUID, conv: Conversation) -> None:
        if user_id == conv.participant_low_id:
            conv.unread_low_count += 1
        else:
            conv.unread_high_count += 1

    @staticmethod
    def clear_unread_for(user_id: UUID, conv: Conversation) -> None:
        if user_id == conv.participant_low_id:
            conv.unread_low_count = 0
        else:
            conv.unread_high_count = 0
