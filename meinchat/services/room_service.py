"""Business logic for multi-party rooms (S86.1, D3).

Membership rules (AD3/AD4):
- The creator is the room's first ``admin``; the seeded members are ``member``.
- **Any** current member may invite (idempotent on a repeat).
- Only an ``admin`` may rename the room or remove *another* member; a member
  may always remove *themselves* (== leave).

Errors are typed and raised (never a silent ``None``/``False`` for an
unsupported op — Liskov, per the engineering requirements). Naming mirrors the
existing 1:1 errors (``ConversationNotFoundError`` /
``NotAConversationMemberError``).
"""
from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from plugins.meinchat.meinchat.models.room import Room
from plugins.meinchat.meinchat.models.room_member import (
    ROOM_ROLE_ADMIN,
    ROOM_ROLE_MEMBER,
    RoomMember,
)
from plugins.meinchat.meinchat.repositories.room_member_repository import (
    RoomMemberRepository,
)
from plugins.meinchat.meinchat.repositories.room_repository import RoomRepository
from plugins.meinchat.meinchat.services.room_protocol import RoomProtocolSelector
from vbwd.utils.datetime_utils import utcnow_aware


class RoomNotFoundError(Exception):
    """No room with the given id."""


class NotARoomMemberError(Exception):
    """The acting user is not a member of the room."""


class RoomPermissionError(Exception):
    """The acting member lacks the role for this operation (admin-only)."""


class RoomService:
    def __init__(
        self,
        room_repo: RoomRepository,
        member_repo: RoomMemberRepository,
        protocol_selector: RoomProtocolSelector,
        role_resolver,
    ) -> None:
        """``role_resolver`` maps a user id to its ``UserRole`` (or ``None`` when
        the user is unknown) — kept as a narrow callable so the service does not
        depend on the core user repository concretely (D — dependency
        inversion)."""
        self._room_repo = room_repo
        self._member_repo = member_repo
        self._protocol_selector = protocol_selector
        self._role_resolver = role_resolver

    # ── write path ──────────────────────────────────────────────────────────

    def create_room(
        self,
        creator_id: UUID,
        member_user_ids: List[UUID],
        *,
        name: Optional[str] = None,
        accepted_protocols: Optional[List[str]] = None,
        widget_slug: Optional[str] = None,
    ) -> Room:
        """Create a room with the creator as ``admin`` and each distinct other
        user as a ``member``. The creator is never also added as a plain member
        row. Protocol is pinned per D4."""
        distinct_member_ids = [
            user_id
            for user_id in _dedupe_preserving_order(member_user_ids)
            if user_id != creator_id
        ]
        all_member_ids = [creator_id, *distinct_member_ids]
        member_roles = {uid: self._role_resolver(uid) for uid in all_member_ids}

        protocol, capabilities = self._protocol_selector.select(
            all_member_ids, member_roles, accepted_protocols
        )

        room = Room()
        room.name = name
        room.created_by_id = creator_id
        room.protocol = protocol
        room.capabilities = capabilities
        room.widget_slug = widget_slug
        self._room_repo.save(room)

        self._add_member(room.id, creator_id, ROOM_ROLE_ADMIN, invited_by_id=None)
        for user_id in distinct_member_ids:
            self._add_member(
                room.id, user_id, ROOM_ROLE_MEMBER, invited_by_id=creator_id
            )
        return room

    def invite(
        self, room_id: UUID, inviter_id: UUID, invitee_user_id: UUID
    ) -> RoomMember:
        """Add ``invitee_user_id`` as a ``member``. Any current member may
        invite (D3). Idempotent: re-inviting an existing member returns the
        existing row without creating a duplicate."""
        self._require_room(room_id)
        if not self.is_member(room_id, inviter_id):
            raise NotARoomMemberError(
                f"user {inviter_id} is not a member of room {room_id}"
            )
        existing = self._member_repo.find(room_id, invitee_user_id)
        if existing is not None:
            return existing
        return self._add_member(
            room_id, invitee_user_id, ROOM_ROLE_MEMBER, invited_by_id=inviter_id
        )

    def remove_member(self, room_id: UUID, actor_id: UUID, target_id: UUID) -> None:
        """Remove ``target_id``. Allowed when the actor is an ``admin`` OR when
        the actor is removing themselves (self-removal == leave)."""
        self._require_room(room_id)
        if actor_id != target_id and self.role_of(room_id, actor_id) != ROOM_ROLE_ADMIN:
            raise RoomPermissionError(
                f"user {actor_id} is not an admin of room {room_id}"
            )
        target = self._member_repo.find(room_id, target_id)
        if target is None:
            raise NotARoomMemberError(
                f"user {target_id} is not a member of room {room_id}"
            )
        self._member_repo.delete(target)

    def leave(self, room_id: UUID, user_id: UUID) -> None:
        """A member removes themselves from the room."""
        self.remove_member(room_id, user_id, user_id)

    def rename(self, room_id: UUID, actor_id: UUID, name: Optional[str]) -> Room:
        """Rename the room. Admin-only."""
        room = self._require_room(room_id)
        if self.role_of(room_id, actor_id) != ROOM_ROLE_ADMIN:
            raise RoomPermissionError(
                f"user {actor_id} is not an admin of room {room_id}"
            )
        room.name = name
        return self._room_repo.save(room)

    # ── read helpers ────────────────────────────────────────────────────────

    def list_for_user(self, user_id: UUID) -> List[Room]:
        return self._room_repo.list_for_user(user_id)

    def get_for_member(self, room_id: UUID, user_id: UUID) -> Room:
        """Return the room only if ``user_id`` is a member; raise otherwise."""
        room = self._require_room(room_id)
        if not self.is_member(room_id, user_id):
            raise NotARoomMemberError(
                f"user {user_id} is not a member of room {room_id}"
            )
        return room

    def members(self, room_id: UUID) -> List[RoomMember]:
        self._require_room(room_id)
        return self._member_repo.list_for_room(room_id)

    def is_member(self, room_id: UUID, user_id: UUID) -> bool:
        return self._member_repo.find(room_id, user_id) is not None

    def role_of(self, room_id: UUID, user_id: UUID) -> str:
        """Return the user's room role; raise if they are not a member."""
        member = self._member_repo.find(room_id, user_id)
        if member is None:
            raise NotARoomMemberError(
                f"user {user_id} is not a member of room {room_id}"
            )
        return member.role

    def peers_of(self, room_id: UUID, user_id: UUID) -> List[UUID]:
        """Every member id except ``user_id`` (delivery fan-out helper)."""
        return [
            member.user_id
            for member in self._member_repo.list_for_room(room_id)
            if member.user_id != user_id
        ]

    # ── internals ─────────────────────────────────────────────────────────────

    def _require_room(self, room_id: UUID) -> Room:
        room = self._room_repo.find_by_id(room_id)
        if room is None:
            raise RoomNotFoundError(f"room {room_id} not found")
        return room

    def _add_member(
        self,
        room_id: UUID,
        user_id: UUID,
        role: str,
        *,
        invited_by_id: Optional[UUID],
    ) -> RoomMember:
        member = RoomMember()
        member.room_id = room_id
        member.user_id = user_id
        member.role = role
        member.invited_by_id = invited_by_id
        member.joined_at = utcnow_aware()
        member.unread_count = 0
        return self._member_repo.save(member)


def _dedupe_preserving_order(user_ids: List[UUID]) -> List[UUID]:
    seen: set = set()
    ordered: List[UUID] = []
    for user_id in user_ids:
        if user_id not in seen:
            seen.add(user_id)
            ordered.append(user_id)
    return ordered
