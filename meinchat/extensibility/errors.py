"""Shared extension-seam errors (S28.3a §9 DRY).

`NoDeviceKeysError` lives here in meinchat (the base) and is reused by both
`BothPeersHaveDeviceKeys` (S28.3b) and the attachment codec (S28.4).
`RoomPolicyError` is the room-start veto contract (S86.2) — meinchat owns the
`IRoomPolicy` port, so the error it carries lives here too (the route catches
it without importing the plugin that raises it).
"""


class NoDeviceKeysError(Exception):
    """An e2e operation needs device keys but the user has none active."""


class RoomPolicyError(Exception):
    """A room-start veto from an `IRoomPolicy` impl (code + UI hint)."""

    def __init__(self, *, code: str, hint: str) -> None:
        super().__init__(hint)
        self.code = code
        self.hint = hint
