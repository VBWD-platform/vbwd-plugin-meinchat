"""Resolve (per_window, window_seconds) for (category, platform).

Pure data: takes the merged meinchat plugin config dict, returns numbers.
No Flask, no Redis, no I/O.

Config key shape (flat, taro-style — see plugins/taro/admin-config.json):
    rate_<category>_per_window           # baseline (web + unknown platforms)
    rate_<category>_window_seconds
    rate_<platform>_<category>_per_window  # platform override (ios, …)
    rate_<platform>_<category>_window_seconds

Resolution order, first hit wins:
    1) platform override pair (both keys must be present)
    2) baseline pair (both keys must be present)
    3) legacy flat keys (message_rate_per_minute, attachment_rate_per_hour)
       — back-compat for instances on the pre-S26 config
    4) hardcoded built-in default — fail-safe, never zero
"""
from typing import Dict, Tuple


CATEGORIES = (
    "new_conversation",
    "nickname_search",
    "message_send",
    "attachment_send",
    # S86.3 — public bot-widget "Start Conversation" (keyed by client IP, since
    # the visitor is anonymous until a GUEST is provisioned).
    "widget_guest_start",
)


# Last-resort defaults. Matches the §1 target table in the sprint.
_BASELINE_FALLBACK: Dict[str, Tuple[int, int]] = {
    "new_conversation": (10, 3600),
    "nickname_search": (30, 60),
    "message_send": (30, 60),
    "attachment_send": (6, 3600),
    "widget_guest_start": (5, 3600),
}

# Pre-S26 instances configured these two via flat keys; honour them when the
# new `rate_*` keys are absent so an upgrade doesn't silently reset tuning.
_LEGACY_KEY_MAP: Dict[str, Tuple[str, int]] = {
    "message_send": ("message_rate_per_minute", 60),
    "attachment_send": ("attachment_rate_per_hour", 3600),
}


class UnknownRateLimitCategory(ValueError):
    """Raised when a category outside CATEGORIES is requested."""


class RateLimitPolicy:
    def __init__(self, config: Dict) -> None:
        self._config = config or {}

    def limits_for(self, category: str, platform: str) -> Tuple[int, int]:
        if category not in CATEGORIES:
            raise UnknownRateLimitCategory(f"unknown rate-limit category: {category!r}")
        platform_key = (platform or "web").lower()

        override = self._read_pair(f"rate_{platform_key}_{category}")
        if override is not None:
            return override

        baseline = self._read_pair(f"rate_{category}")
        if baseline is not None:
            return baseline

        legacy = _LEGACY_KEY_MAP.get(category)
        if legacy is not None:
            legacy_key, legacy_window = legacy
            legacy_value = self._config.get(legacy_key)
            if isinstance(legacy_value, int) and legacy_value > 0:
                return (legacy_value, legacy_window)

        return _BASELINE_FALLBACK[category]

    def _read_pair(self, prefix: str):
        per_window = self._config.get(f"{prefix}_per_window")
        window_seconds = self._config.get(f"{prefix}_window_seconds")
        if (
            isinstance(per_window, int)
            and isinstance(window_seconds, int)
            and per_window > 0
            and window_seconds > 0
        ):
            return (per_window, window_seconds)
        return None
