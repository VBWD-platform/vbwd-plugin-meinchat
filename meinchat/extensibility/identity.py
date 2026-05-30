"""Device directory port (S28.3a §2.3).

meinchat default is `NullDeviceDirectory` (no device keys). meinchat-plus
replaces it with a directory backed by `meinchat_plus_user_device_key`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, runtime_checkable
from uuid import UUID


class DirectoryNotEnabledError(Exception):
    """register/revoke called on the null directory — needs meinchat-plus."""


@dataclass(frozen=True)
class Device:
    """A user's registered device public key."""

    id: UUID
    user_id: UUID
    pubkey: bytes
    alg: str
    label: Optional[str] = None


@runtime_checkable
class IDeviceDirectory(Protocol):
    def register(
        self, user_id: UUID, pubkey: bytes, alg: str, label: Optional[str]
    ) -> Device:
        ...

    def lookup_active(self, user_id: UUID) -> List[Device]:
        ...

    def revoke(self, device_id: UUID) -> None:
        ...

    def has_any(self, user_id: UUID) -> bool:
        ...


class NullDeviceDirectory:
    """Default: meinchat-alone has no device keys."""

    def register(
        self, user_id: UUID, pubkey: bytes, alg: str, label: Optional[str] = None
    ) -> Device:
        raise DirectoryNotEnabledError(
            "device directory requires the meinchat-plus plugin"
        )

    def lookup_active(self, user_id: UUID) -> List[Device]:
        return []

    def revoke(self, device_id: UUID) -> None:
        raise DirectoryNotEnabledError(
            "device directory requires the meinchat-plus plugin"
        )

    def has_any(self, user_id: UUID) -> bool:
        return False
