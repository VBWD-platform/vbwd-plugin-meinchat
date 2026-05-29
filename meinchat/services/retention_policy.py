"""Retention-eligibility policy port + the phase-1 ConfigRetentionPolicy.

The `IRetentionPolicy` port is the documented extension seam that
meinchat-plus plugs into in phase 2 (S28.3a §2.4 / S28.3b): its
`E2eAwareRetentionPolicy` will replace `ConfigRetentionPolicy` via the
registry, exempting undelivered `e2e_v1` rows. Because the port crosses
the meinchat -> meinchat-plus plugin boundary it earns its keep even as
a single implementation today (NO OVERENGINEERING rule §8: a
single-impl port survives only when it crosses the plugin boundary).

Phase 1 ships ONLY `ConfigRetentionPolicy` — see
`s28-phase1-retention-and-config.md` §3. It reads
`messages_retention_days_server` from the config store and treats every
row older than the threshold as eligible (today's behaviour). It does
NOT reference `protocol` or `delivered_to_all_addressed_devices_at`;
those columns are added by S28.3a in phase 2.
"""
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Callable, Dict


_MESSAGES_KEEP_DAYS_KEY = "messages_retention_days_server"


class IRetentionPolicy(ABC):
    """Decides whether a single message row is eligible for pruning."""

    @abstractmethod
    def messages_keep_days(self) -> int:
        """Number of days to retain message rows. Drives the repository's
        candidate query (`find_older_than`); the per-row `should_prune`
        check then filters that candidate set."""

    @abstractmethod
    def should_prune(self, message: Any, now: datetime) -> bool:
        """Return True if `message` should be hard-deleted as of `now`."""


class ConfigRetentionPolicy(IRetentionPolicy):
    """Phase-1 policy: prune any row past the configured day threshold.

    Reads the threshold from the config store on every call so an operator
    config change takes effect on the next daily tick without a restart.
    """

    def __init__(self, config_provider: Callable[[], Dict[str, Any]]) -> None:
        self._config_provider = config_provider

    def messages_keep_days(self) -> int:
        config = self._config_provider() or {}
        days = int(config.get(_MESSAGES_KEEP_DAYS_KEY, 0))
        if days < 0:
            raise ValueError(f"{_MESSAGES_KEEP_DAYS_KEY} must be >= 0, got {days}")
        return days

    def should_prune(self, message: Any, now: datetime) -> bool:
        threshold = now - timedelta(days=self.messages_keep_days())
        return message.sent_at < threshold
