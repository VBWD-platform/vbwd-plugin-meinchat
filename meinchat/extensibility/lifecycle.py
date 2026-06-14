"""Conversation lifecycle ports (S28.3a §2.2; rooms S86.2 §D2/D5).

`IConversationPolicy` (multi-impl, all-must-allow) vetoes conversation starts.
`IConversationCapabilities` (multi-impl, union) advertises supported protocols
for a conversation (`for_conversation`) and a room (`for_room`).
`IRoomPolicy` (multi-impl, all-must-allow) vetoes room starts — the N-party
generalisation of `IConversationPolicy`, kept as its own narrow port (ISP) so
1:1 consumers never carry a room-shaped method.
meinchat defaults: allow-all policies + `{"plain"}` capability. meinchat-plus
adds `BothPeersHaveDeviceKeys` / `AllMembersHaveDeviceKeys` and `{"e2e_v1"}`.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional, Protocol, Set, runtime_checkable
from uuid import UUID


@runtime_checkable
class IConversationPolicy(Protocol):
    def may_start(
        self, initiator: Any, peer: Any, accepted_protocols: List[str]
    ) -> None:
        """Raise to veto; return None to allow."""
        ...


@runtime_checkable
class IRoomPolicy(Protocol):
    def may_start_room(
        self,
        member_user_ids: List[UUID],
        member_roles: dict,
        accepted_protocols: Optional[List[str]],
        *,
        nickname_of: Callable[[UUID], Optional[str]],
    ) -> None:
        """Raise to veto a room start; return None to allow."""
        ...


@runtime_checkable
class IConversationCapabilities(Protocol):
    def for_conversation(self, conv: Any) -> Set[str]:
        ...


@runtime_checkable
class IRoomCapabilities(Protocol):
    def for_room(self, room: Any) -> Set[str]:
        ...


class BlockListPolicy:
    """Default policy. meinchat has no block-list table today, so this is an
    allow-all placeholder that populates the policy seam; meinchat-plus
    registers `BothPeersHaveDeviceKeys` alongside it (all-must-allow)."""

    def may_start(
        self, initiator: Any, peer: Any, accepted_protocols: List[str]
    ) -> None:
        return None


class AllowAllRoomPolicy:
    """Default room policy — allow-all. meinchat-plus registers
    `AllMembersHaveDeviceKeys` alongside it (all-must-allow)."""

    def may_start_room(
        self,
        member_user_ids: List[UUID],
        member_roles: dict,
        accepted_protocols: Optional[List[str]],
        *,
        nickname_of: Callable[[UUID], Optional[str]],
    ) -> None:
        return None


class PlainCapability:
    """Default capability: every conversation supports plaintext."""

    def for_conversation(self, conv: Any) -> Set[str]:
        return {"plain"}


class PlainRoomCapability:
    """Default room capability: every room supports plaintext."""

    def for_room(self, room: Any) -> Set[str]:
        return {"plain"}
