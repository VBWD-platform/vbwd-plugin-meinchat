"""Admin cleanup of meinchat CHAT & GUEST session data.

This is a moderation / housekeeping tool, NOT auth/login session control: it
wipes a user's (or every guest's) meinchat conversations, rooms and widget
guest-sessions, then resets their core token balance to the configured guest
default.

Deletion order is FK-safe by relying on the model cascades:

- deleting a ``Conversation`` cascades its ``Message`` rows (and each message's
  ``MeinchatAttachment`` blobs) via ``ondelete="CASCADE"`` / orphan cascade;
- deleting a ``Room`` cascades its ``RoomMember`` rows and its ``Message`` rows
  (and their attachments) the same way;
- ``MeinchatGuestSession.room_id`` is ``ondelete="SET NULL"``, so deleting a
  room does NOT remove the session row — those are deleted explicitly here.

Balances are the single source of truth in core's ``TokenService``; the reset
routes every credit/debit through it (DRY) with ``ADJUSTMENT`` transactions.
Collaborators arrive via the constructor (DI) so unit tests pass MagicMocks.
"""
from typing import Any, Callable, Dict, List
from uuid import UUID

from plugins.meinchat.meinchat.models.conversation import Conversation
from plugins.meinchat.meinchat.models.guest_session import MeinchatGuestSession
from plugins.meinchat.meinchat.models.room import Room


class SessionCleanupService:
    """Wipe meinchat chat + guest session data for one user or every guest."""

    def __init__(
        self,
        *,
        session: Any,
        token_service: Any,
        guest_session_repo: Any,
        conversation_repo: Any,
        room_repo: Any,
        guest_initial_tokens: int,
        reset_balance: Callable[[UUID, int], int],
    ) -> None:
        self._session = session
        self._token_service = token_service
        self._guest_session_repo = guest_session_repo
        self._conversation_repo = conversation_repo
        self._room_repo = room_repo
        self._guest_initial_tokens = guest_initial_tokens
        self._reset_balance = reset_balance

    # ── bulk: every guest ────────────────────────────────────────────────────
    def clear_all_guest_sessions(self) -> Dict[str, int]:
        """Delete every guest's meinchat data. Idempotent (a second run is a
        clean no-op returning zero rows beyond ``guests``)."""
        guest_user_ids = self._guest_session_repo.all_guest_user_ids()
        conversations_deleted = 0
        rooms_deleted = 0
        sessions_deleted = 0
        for guest_user_id in guest_user_ids:
            conversations_deleted += self._delete_conversations_for(guest_user_id)
            rooms_deleted += self._delete_rooms_for(guest_user_id)
            sessions_deleted += self._delete_guest_sessions_for(guest_user_id)
        return {
            "guests": len(guest_user_ids),
            "conversations": conversations_deleted,
            "rooms": rooms_deleted,
            "sessions": sessions_deleted,
        }

    # ── single user (guest OR registered) ────────────────────────────────────
    def clear_user_sessions(self, user_id: UUID) -> Dict[str, int]:
        """Delete ONE user's meinchat conversations, rooms (created or member)
        and guest-session rows, then reset their core token balance to the
        configured ``guest_initial_tokens`` default. Idempotent."""
        conversations_deleted = self._delete_conversations_for(user_id)
        rooms_deleted = self._delete_rooms_for(user_id)
        sessions_deleted = self._delete_guest_sessions_for(user_id)
        balance = self._reset_balance(user_id, self._guest_initial_tokens)
        return {
            "conversations": conversations_deleted,
            "rooms": rooms_deleted,
            "sessions": sessions_deleted,
            "balance": balance,
        }

    # ── internals ─────────────────────────────────────────────────────────────
    def _delete_conversations_for(self, user_id: UUID) -> int:
        conversations: List[Conversation] = self._conversation_repo.list_for_user(
            user_id
        )
        for conversation in conversations:
            self._session.delete(conversation)
        return len(conversations)

    def _delete_rooms_for(self, user_id: UUID) -> int:
        """Rooms the user is a member of OR created. ``list_for_user`` returns
        the rooms the user belongs to; a room the user created but already left
        is found via ``created_by_id`` so it is never orphaned."""
        rooms: Dict[UUID, Room] = {}
        for room in self._room_repo.list_for_user(user_id):
            rooms[room.id] = room
        for room in (
            self._session.query(Room).filter(Room.created_by_id == user_id).all()
        ):
            rooms[room.id] = room
        for room in rooms.values():
            self._session.delete(room)
        return len(rooms)

    def _delete_guest_sessions_for(self, user_id: UUID) -> int:
        sessions = (
            self._session.query(MeinchatGuestSession)
            .filter(MeinchatGuestSession.guest_user_id == user_id)
            .all()
        )
        for guest_session in sessions:
            self._session.delete(guest_session)
        return len(sessions)
