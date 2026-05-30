"""S28.1 §3.1 — unit specs for RetentionService + ConfigRetentionPolicy.

Phase 1 scope (s28-phase1-retention-and-config.md §3): only the
``ConfigRetentionPolicy`` ships. The E2E-aware policy (protocol +
delivered_to_all_addressed_devices_at columns) and its specs (#9-#12)
are deferred to S28.3b — see the `@pytest.mark.skip` placeholders at the
bottom of this module so they are not lost.

Unit tests use ``MagicMock`` / fakes + ``InMemoryFileStorage``; no DB.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from vbwd.interfaces.file_storage import InMemoryFileStorage
from plugins.meinchat.meinchat.services.retention_policy import (
    ConfigRetentionPolicy,
)
from plugins.meinchat.meinchat.services.retention_service import (
    RetentionResult,
    RetentionService,
)


UTC = timezone.utc
_NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)


def _message(*, sent_at, attachment_urls=None):
    """A lightweight stand-in for a Message row (the service touches `id`,
    `sent_at`, and the `attachments` child rows' `storage_url`)."""
    return SimpleNamespace(
        id=uuid4(),
        sent_at=sent_at,
        attachments=[
            SimpleNamespace(storage_url=url) for url in (attachment_urls or [])
        ],
    )


class _FakeMessageRepo:
    """In-memory message repo honouring the IMessageRepository contract used
    by RetentionService: `find_older_than` + `delete_by_ids`."""

    def __init__(self, rows):
        self._rows = {row.id: row for row in rows}

    def find_older_than(self, threshold):
        return [r for r in self._rows.values() if r.sent_at < threshold]

    def delete_by_ids(self, ids):
        deleted = [i for i in ids if i in self._rows]
        for i in deleted:
            del self._rows[i]
        return deleted

    # convenience for assertions
    def remaining_ids(self):
        return set(self._rows.keys())


def _policy(days):
    return ConfigRetentionPolicy(
        config_provider=lambda: {"messages_retention_days_server": days}
    )


def _service(repo, *, days=2, storage=None, clock=None):
    return RetentionService(
        message_repo=repo,
        attachment_storage=storage or InMemoryFileStorage(base_url="/uploads"),
        retention_policy=_policy(days),
        clock=clock or (lambda: _NOW),
    )


# ── #1 ──────────────────────────────────────────────────────────────────────
def test_deletes_only_messages_older_than_threshold():
    older = _message(sent_at=_NOW - timedelta(days=5))
    at_boundary = _message(sent_at=_NOW - timedelta(days=2, minutes=1))
    recent = _message(sent_at=_NOW - timedelta(days=1))
    repo = _FakeMessageRepo([older, at_boundary, recent])

    result = _service(repo, days=2).prune_messages()

    assert result.deleted_count == 2
    assert repo.remaining_ids() == {recent.id}


# ── #2 ──────────────────────────────────────────────────────────────────────
def test_off_by_one_minute_at_threshold():
    just_past = _message(sent_at=_NOW - timedelta(days=2, minutes=1))
    just_inside = _message(sent_at=_NOW - timedelta(days=2) + timedelta(minutes=1))
    repo = _FakeMessageRepo([just_past, just_inside])

    result = _service(repo, days=2).prune_messages()

    assert result.deleted_count == 1
    assert repo.remaining_ids() == {just_inside.id}


# ── #3 ──────────────────────────────────────────────────────────────────────
def test_days_zero_deletes_everything():
    rows = [
        _message(sent_at=_NOW - timedelta(days=5)),
        _message(sent_at=_NOW - timedelta(minutes=1)),
    ]
    repo = _FakeMessageRepo(rows)

    result = _service(repo, days=0).prune_messages()

    assert result.deleted_count == 2
    assert repo.remaining_ids() == set()


# ── #4 ──────────────────────────────────────────────────────────────────────
def test_days_negative_raises():
    repo = _FakeMessageRepo([])
    with pytest.raises(ValueError):
        _service(repo, days=-1).prune_messages()


# ── #5 ──────────────────────────────────────────────────────────────────────
def test_idempotent_re_run_is_noop():
    rows = [_message(sent_at=_NOW - timedelta(days=5))]
    repo = _FakeMessageRepo(rows)
    service = _service(repo, days=2)

    first = service.prune_messages()
    second = service.prune_messages()

    assert first.deleted_count == 1
    assert second.deleted_count == 0


# ── #6 ──────────────────────────────────────────────────────────────────────
def test_returns_deleted_ids():
    older = _message(sent_at=_NOW - timedelta(days=5))
    recent = _message(sent_at=_NOW - timedelta(hours=1))
    repo = _FakeMessageRepo([older, recent])

    result = _service(repo, days=2).prune_messages()

    assert result.deleted_ids == [older.id]


# ── #7 ──────────────────────────────────────────────────────────────────────
def test_attachment_storage_failure_is_logged_not_raised():
    older = _message(
        sent_at=_NOW - timedelta(days=5),
        attachment_urls=[
            "/uploads/meinchat/attachments/u/x.webp",
            "/uploads/meinchat/attachments/u/x.thumb.webp",
        ],
    )
    repo = _FakeMessageRepo([older])
    storage = MagicMock(spec=InMemoryFileStorage)
    storage.delete.side_effect = OSError("disk gone")

    service = RetentionService(
        message_repo=repo,
        attachment_storage=storage,
        retention_policy=_policy(2),
        clock=lambda: _NOW,
    )

    result = service.prune_attachments()

    # Storage blew up but the call did NOT raise; errors are counted.
    assert result.errors >= 1
    # The row delete is the source of truth — prune_messages still removes it.
    assert service.prune_messages().deleted_count == 1
    assert repo.remaining_ids() == set()


# ── #8 ──────────────────────────────────────────────────────────────────────
def test_clock_is_injected():
    # With a clock anchored 10 days in the future, a -5d-from-real-now row is
    # well past a 2-day threshold and must be pruned; with the default real
    # clock the SAME absolute timestamp (far future) would be kept.
    fixed_now = _NOW
    row = _message(sent_at=fixed_now - timedelta(days=3))
    repo = _FakeMessageRepo([row])

    kept = _service(repo, days=2, clock=lambda: fixed_now - timedelta(days=5))
    assert kept.prune_messages().deleted_count == 0
    assert repo.remaining_ids() == {row.id}

    pruned = _service(repo, days=2, clock=lambda: fixed_now)
    assert pruned.prune_messages().deleted_count == 1


# ── attachment cleanup happy path (supports §3.2 spec 3 at unit level) ───────
def test_prune_attachments_removes_child_table_blobs():
    # S28.4 — all attachments (plain + e2e, fullres + thumb) live in the
    # meinchat_attachment child table; the prune deletes their storage blobs.
    storage = InMemoryFileStorage(base_url="/uploads")
    storage.save(b"orig", "meinchat/attachments/u/x.webp")
    storage.save(b"thumb", "meinchat/attachments/u/x.thumb.webp")
    older = _message(
        sent_at=_NOW - timedelta(days=5),
        attachment_urls=[
            "/uploads/meinchat/attachments/u/x.webp",
            "/uploads/meinchat/attachments/u/x.thumb.webp",
        ],
    )
    repo = _FakeMessageRepo([older])

    result = _service(repo, days=2, storage=storage).prune_attachments()

    assert result.errors == 0
    assert not storage.exists("meinchat/attachments/u/x.webp")
    assert not storage.exists("meinchat/attachments/u/x.thumb.webp")


def test_result_shape_carries_skipped_undelivered_field():
    # Phase-1 placeholder field that S28.3b's E2eAwareRetentionPolicy will
    # populate without changing the result shape.
    result = RetentionResult(
        deleted_ids=[],
        deleted_count=0,
        skipped_undelivered_count=0,
        skipped_count=0,
        errors=0,
    )
    assert result.skipped_undelivered_count == 0
    with pytest.raises(Exception):
        result.deleted_count = 5  # frozen dataclass — immutable


# ── #9-#12 DEFERRED to phase 2 / S28.3b ──────────────────────────────────────
# These require the `protocol` + `delivered_to_all_addressed_devices_at`
# columns (added by S28.3a) and the meinchat-plus plugin (S28.3b), neither of
# which exist in phase 1. Kept as skipped placeholders so the contract is not
# lost. See s28-phase1-retention-and-config.md §3.
@pytest.mark.skip(
    reason="phase 2 / S28.3b: requires protocol + "
    "delivered_to_all_addressed_devices_at columns and meinchat-plus"
)
def test_e2e_undelivered_row_survives_prune():  # spec #9
    ...


@pytest.mark.skip(
    reason="phase 2 / S28.3b: requires protocol + "
    "delivered_to_all_addressed_devices_at columns and meinchat-plus"
)
def test_e2e_delivered_row_pruned_normally():  # spec #10
    ...


@pytest.mark.skip(
    reason="phase 2 / S28.3b: requires protocol + "
    "delivered_to_all_addressed_devices_at columns and meinchat-plus"
)
def test_undelivered_count_is_reported_separately():  # spec #11
    ...


@pytest.mark.skip(
    reason="phase 2 / S28.3b: IncompatibleRetentionConfigError requires "
    "meinchat-plus (does not exist in phase 1)"
)
def test_zero_days_and_e2e_enabled_refused_at_plugin_enable():  # spec #12
    ...
