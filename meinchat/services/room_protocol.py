"""Room protocol selection (S86.1, D4) — generalises the 1:1 negotiation.

A 1:1 conversation negotiates a protocol from the instance's enabled
capabilities and the *peer's* device-key state (`_negotiate_protocol` in
routes.py). A room generalises "the peer" to "every member": a protocol that
needs device keys (e.g. `e2e_v1`) is only selectable when **all** members have
them. With a bot member, or when nothing grants a secure protocol (the
meinchat-alone Null device directory), the outcome is always `plain`.

S86.2 hook: this selector reads the SAME extension seams the 1:1 path uses —
the `IConversationCapabilities` union (server capabilities) and the
`IDeviceDirectory` (per-member device keys). S86.2 registers an e2e capability +
a real device directory there; no change is needed here for `e2e_v1` to become
selectable once every member has keys. S86.1 ships only the `plain` outcome.
"""
from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Set, Tuple
from uuid import UUID

from vbwd.models.enums import UserRole

# Most-secure-first; selection picks the first protocol all members support.
PROTOCOL_PREFERENCE = ["e2e_v1", "plain"]
PLAIN = "plain"

# Protocols whose precondition is "every member has at least one device key".
_DEVICE_KEY_PROTOCOLS = frozenset({"e2e_v1"})


class RoomProtocolSelector:
    """Pins a room's protocol from its membership and the registered seams.

    Collaborators are injected so the service is testable without the Flask
    app / registry, and so S86.2 can supply a real device directory + an
    expanded capability set through the same constructor.
    """

    def __init__(
        self,
        server_capabilities_provider: Callable[[], Set[str]],
        device_has_keys: Callable[[UUID], bool],
    ) -> None:
        self._server_capabilities_provider = server_capabilities_provider
        self._device_has_keys = device_has_keys

    def select(
        self,
        member_user_ids: Iterable[UUID],
        member_roles: dict,
        accepted_protocols: Optional[List[str]],
    ) -> Tuple[str, List[str]]:
        """Return ``(protocol, capabilities)`` pinned for the room.

        - Any member whose role is ``BOT`` forces ``plain`` (D4).
        - Otherwise the chosen protocol is the most-preferred one that is
          enabled on the instance, accepted by the caller (when they passed a
          list), and supported by **all** members.
        """
        member_ids = list(member_user_ids)
        if any(member_roles.get(uid) == UserRole.BOT for uid in member_ids):
            return PLAIN, [PLAIN]

        server = self._server_capabilities_provider() or {PLAIN}
        candidates = [
            protocol for protocol in PROTOCOL_PREFERENCE if protocol in server
        ]
        if accepted_protocols:
            candidates = [p for p in candidates if p in accepted_protocols]

        for protocol in candidates:
            if self._all_members_support(protocol, member_ids):
                return protocol, [protocol]
        return PLAIN, [PLAIN]

    def _all_members_support(self, protocol: str, member_ids: List[UUID]) -> bool:
        if protocol not in _DEVICE_KEY_PROTOCOLS:
            return True
        return all(self._device_has_keys(uid) for uid in member_ids)
