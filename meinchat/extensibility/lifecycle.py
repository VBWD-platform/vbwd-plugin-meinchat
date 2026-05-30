"""Conversation lifecycle ports (S28.3a §2.2).

`IConversationPolicy` (multi-impl, all-must-allow) vetoes conversation starts.
`IConversationCapabilities` (multi-impl, union) advertises supported protocols.
meinchat defaults: allow-all policy + `{"plain"}` capability. meinchat-plus
adds `BothPeersHaveDeviceKeys` and `{"e2e_v1"}`.
"""
from __future__ import annotations

from typing import Any, List, Protocol, Set, runtime_checkable


@runtime_checkable
class IConversationPolicy(Protocol):
    def may_start(
        self, initiator: Any, peer: Any, accepted_protocols: List[str]
    ) -> None:
        """Raise to veto; return None to allow."""
        ...


@runtime_checkable
class IConversationCapabilities(Protocol):
    def for_conversation(self, conv: Any) -> Set[str]:
        ...


class BlockListPolicy:
    """Default policy. meinchat has no block-list table today, so this is an
    allow-all placeholder that populates the policy seam; meinchat-plus
    registers `BothPeersHaveDeviceKeys` alongside it (all-must-allow)."""

    def may_start(
        self, initiator: Any, peer: Any, accepted_protocols: List[str]
    ) -> None:
        return None


class PlainCapability:
    """Default capability: every conversation supports plaintext."""

    def for_conversation(self, conv: Any) -> Set[str]:
        return {"plain"}
