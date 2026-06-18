"""Unit specs for ``SessionCleanupService`` with MagicMock collaborators.

Proves the deletion + reset contract without a DB:

- clearing all guests iterates every distinct guest id and deletes their
  conversations, rooms and guest-session rows;
- clearing one user deletes that user's data and resets the balance via the
  injected ``reset_balance`` callable;
- a user with no data is a clean no-op returning zero counts;
- the returned counts reflect exactly what was deleted.
"""
from unittest.mock import MagicMock
from uuid import uuid4

from plugins.meinchat.meinchat.services.session_cleanup_service import (
    SessionCleanupService,
)


def _row_with_id(row_id):
    row = MagicMock()
    row.id = row_id
    return row


def _build(
    *,
    guest_ids=None,
    conversations=None,
    rooms_member=None,
    rooms_created=None,
    guest_sessions=None,
    guest_initial_tokens=5,
):
    session = MagicMock()
    query = MagicMock()
    session.query.return_value = query
    filtered = MagicMock()
    query.filter.return_value = filtered
    # _delete_rooms_for queries created rooms; _delete_guest_sessions_for
    # queries guest_session rows. Both go through query().filter().all().
    filtered.all.side_effect = [rooms_created or [], guest_sessions or []]

    token_service = MagicMock()
    guest_session_repo = MagicMock()
    guest_session_repo.all_guest_user_ids.return_value = guest_ids or []
    conversation_repo = MagicMock()
    conversation_repo.list_for_user.return_value = conversations or []
    room_repo = MagicMock()
    room_repo.list_for_user.return_value = rooms_member or []
    reset_balance = MagicMock(return_value=guest_initial_tokens)

    service = SessionCleanupService(
        session=session,
        token_service=token_service,
        guest_session_repo=guest_session_repo,
        conversation_repo=conversation_repo,
        room_repo=room_repo,
        guest_initial_tokens=guest_initial_tokens,
        reset_balance=reset_balance,
    )
    return service, session, reset_balance


def test_clear_user_deletes_data_and_resets_balance():
    user_id = uuid4()
    conversation = _row_with_id(uuid4())
    room = _row_with_id(uuid4())
    guest_session = _row_with_id(uuid4())
    service, session, reset_balance = _build(
        conversations=[conversation],
        rooms_member=[room],
        guest_sessions=[guest_session],
        guest_initial_tokens=7,
    )

    counts = service.clear_user_sessions(user_id)

    session.delete.assert_any_call(conversation)
    session.delete.assert_any_call(room)
    session.delete.assert_any_call(guest_session)
    reset_balance.assert_called_once_with(user_id, 7)
    assert counts == {
        "conversations": 1,
        "rooms": 1,
        "sessions": 1,
        "balance": 7,
    }


def test_clear_user_with_no_data_is_clean_noop():
    user_id = uuid4()
    service, session, reset_balance = _build(guest_initial_tokens=4)

    counts = service.clear_user_sessions(user_id)

    session.delete.assert_not_called()
    reset_balance.assert_called_once_with(user_id, 4)
    assert counts == {
        "conversations": 0,
        "rooms": 0,
        "sessions": 0,
        "balance": 4,
    }


def test_clear_all_guest_sessions_iterates_every_guest():
    guest_a = uuid4()
    service, session, _reset = _build(guest_ids=[guest_a])

    counts = service.clear_all_guest_sessions()

    assert counts["guests"] == 1
    # No seeded rows for this guest → zero deletions, no error (idempotent shape).
    assert counts["conversations"] == 0
    assert counts["rooms"] == 0
    assert counts["sessions"] == 0
