"""Server-side message retention prune (S28.1, phase 1).

Hard-deletes `message` rows whose `sent_at` is older than the configured
threshold AND which the active `IRetentionPolicy.should_prune` predicate
deems eligible, plus the matching attachment objects from `IFileStorage`.
Idempotent — re-running is a no-op.

The eligibility predicate lives in the policy (SOLID-S): swapping in
meinchat-plus's `E2eAwareRetentionPolicy` is a pure registration change,
no edit here. Repo + storage + clock + policy are injected (SOLID-D).
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Optional
from uuid import UUID

from vbwd.interfaces.file_storage import IFileStorage
from plugins.meinchat.meinchat.services.retention_policy import IRetentionPolicy


UTC = timezone.utc
logger = logging.getLogger(__name__)

_ATTACHMENT_MARKER = "meinchat/"


@dataclass(frozen=True)
class RetentionResult:
    """Outcome of a prune pass. `skipped_undelivered_count` stays 0 in
    phase 1 (no E2E rows) but the field is kept so phase 2's E2E-aware
    policy can populate it without changing the result shape."""

    deleted_ids: List[UUID] = field(default_factory=list)
    deleted_count: int = 0
    skipped_undelivered_count: int = 0
    skipped_count: int = 0
    errors: int = 0


def _url_to_storage_path(url: Optional[str]) -> Optional[str]:
    """Recover the storage-relative path from a public attachment URL.

    Attachment paths always carry the `meinchat/` prefix, so cutting the
    URL at that marker is unambiguous and backend-independent. Mirrors
    `message_service._url_to_storage_path` (same convention; kept local to
    avoid importing the message service into the prune path)."""
    if not url:
        return None
    idx = url.find(_ATTACHMENT_MARKER)
    return url[idx:] if idx >= 0 else url.lstrip("/")


class RetentionService:
    """Loop-and-delete prune. Does one thing (SOLID-S)."""

    def __init__(
        self,
        *,
        message_repo: Any,
        attachment_storage: IFileStorage,
        retention_policy: IRetentionPolicy,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        logger: logging.Logger = logger,
    ) -> None:
        self._message_repo = message_repo
        self._attachment_storage = attachment_storage
        self._retention_policy = retention_policy
        self._clock = clock
        self._logger = logger

    def prune_messages(self) -> RetentionResult:
        """Hard-delete eligible message rows. Safe to re-run (idempotent)."""
        eligible, skipped_count = self._eligible_rows()
        deleted_ids = (
            self._message_repo.delete_by_ids([row.id for row in eligible])
            if eligible
            else []
        )
        if deleted_ids:
            self._logger.info(
                "[meinchat] retention prune deleted %d message row(s)",
                len(deleted_ids),
            )
        return RetentionResult(
            deleted_ids=deleted_ids,
            deleted_count=len(deleted_ids),
            skipped_count=skipped_count,
        )

    def prune_attachments(self) -> RetentionResult:
        """Best-effort delete of attachment objects for rows being pruned.

        Storage `.delete()` failures are caught, counted in `errors`, and
        NEVER propagated — the message-row delete is the source of truth."""
        eligible, _ = self._eligible_rows()
        errors = 0
        for row in eligible:
            for url in (
                getattr(row, "attachment_url", None),
                getattr(row, "attachment_thumb_url", None),
            ):
                path = _url_to_storage_path(url)
                if path is None:
                    continue
                try:
                    self._attachment_storage.delete(path)
                except Exception as exc:  # best-effort cleanup — never propagate
                    errors += 1
                    self._logger.warning(
                        "[meinchat] retention failed to delete attachment %s: %s",
                        path,
                        exc,
                    )
        return RetentionResult(errors=errors)

    def _eligible_rows(self):
        """Candidate rows (older than threshold) filtered by the policy's
        per-row `should_prune`. Returns (eligible_rows, skipped_count)."""
        now = self._clock()
        keep_days = self._retention_policy.messages_keep_days()
        threshold = now - timedelta(days=keep_days)
        candidates = self._message_repo.find_older_than(threshold)
        eligible = [
            row for row in candidates if self._retention_policy.should_prune(row, now)
        ]
        return eligible, len(candidates) - len(eligible)
