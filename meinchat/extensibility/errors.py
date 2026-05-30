"""Shared extension-seam errors (S28.3a §9 DRY).

`NoDeviceKeysError` lives here in meinchat (the base) and is reused by both
`BothPeersHaveDeviceKeys` (S28.3b) and the attachment codec (S28.4).
"""


class NoDeviceKeysError(Exception):
    """An e2e operation needs device keys but the user has none active."""
