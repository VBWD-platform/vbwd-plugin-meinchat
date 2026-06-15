"""Unit specs for ``GuestTokenAdminService`` (admin guest-token top-up / reset).

The admin capability lets an operator top-up or reset the GLOBAL core token
balance of an existing widget GUEST (D11 grants the initial budget only at
first-provision; a returning guest at balance 0 is otherwise stuck behind the
402 send gate). These specs use MagicMock collaborators (no DB) and pin the
balance arithmetic, the validation rules, and the not-a-guest guard.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from vbwd.models.enums import TokenTransactionType, UserRole

from plugins.meinchat.meinchat.services.guest_token_admin_service import (
    GuestNotFoundError,
    GuestTokenAdminService,
)


def _session_row(guest_user_id, *, widget_slug="econ", display_name="Guest"):
    return SimpleNamespace(
        guest_user_id=guest_user_id,
        widget_slug=widget_slug,
        display_name=display_name,
        created_at=None,
        updated_at=None,
    )


def _build_service(
    *,
    token_service=None,
    guest_session_repo=None,
    resolve_user_role=None,
    nickname_repo=None,
):
    token_service = token_service or MagicMock()
    guest_session_repo = guest_session_repo or MagicMock()
    nickname_repo = nickname_repo or MagicMock()
    if resolve_user_role is None:
        resolve_user_role = MagicMock(return_value=UserRole.GUEST)
    return GuestTokenAdminService(
        token_service=token_service,
        guest_session_repo=guest_session_repo,
        resolve_user_role=resolve_user_role,
        nickname_repo=nickname_repo,
    )


class TestTopup:
    def test_topup_credits_positive_amount_and_returns_new_balance(self):
        guest_id = uuid4()
        token_service = MagicMock()
        token_service.get_balance.return_value = 12
        guest_session_repo = MagicMock()
        guest_session_repo.find_by_guest_user_id.return_value = _session_row(guest_id)
        service = _build_service(
            token_service=token_service, guest_session_repo=guest_session_repo
        )

        new_balance = service.topup(guest_id, 8)

        token_service.credit_tokens.assert_called_once()
        _args, kwargs = token_service.credit_tokens.call_args
        call = token_service.credit_tokens.call_args
        # amount is the 2nd positional arg
        assert call.args[1] == 8
        assert kwargs["transaction_type"] == TokenTransactionType.ADJUSTMENT
        assert new_balance == 12

    @pytest.mark.parametrize("bad_amount", [0, -5])
    def test_topup_rejects_non_positive_amount(self, bad_amount):
        guest_id = uuid4()
        guest_session_repo = MagicMock()
        guest_session_repo.find_by_guest_user_id.return_value = _session_row(guest_id)
        service = _build_service(guest_session_repo=guest_session_repo)

        with pytest.raises(ValueError):
            service.topup(guest_id, bad_amount)

    def test_topup_unknown_guest_raises_guest_not_found(self):
        guest_session_repo = MagicMock()
        guest_session_repo.find_by_guest_user_id.return_value = None
        service = _build_service(guest_session_repo=guest_session_repo)

        with pytest.raises(GuestNotFoundError):
            service.topup(uuid4(), 5)

    def test_topup_non_guest_role_raises_guest_not_found(self):
        guest_id = uuid4()
        guest_session_repo = MagicMock()
        guest_session_repo.find_by_guest_user_id.return_value = _session_row(guest_id)
        service = _build_service(
            guest_session_repo=guest_session_repo,
            resolve_user_role=MagicMock(return_value=UserRole.USER),
        )

        with pytest.raises(GuestNotFoundError):
            service.topup(guest_id, 5)


class TestReset:
    def test_reset_above_balance_credits_delta(self):
        guest_id = uuid4()
        token_service = MagicMock()
        token_service.get_balance.side_effect = [3, 10]  # before, after
        guest_session_repo = MagicMock()
        guest_session_repo.find_by_guest_user_id.return_value = _session_row(guest_id)
        service = _build_service(
            token_service=token_service, guest_session_repo=guest_session_repo
        )

        new_balance = service.reset(guest_id, 10)

        token_service.credit_tokens.assert_called_once()
        assert token_service.credit_tokens.call_args.args[1] == 7
        token_service.debit_tokens.assert_not_called()
        assert new_balance == 10

    def test_reset_below_balance_debits_delta(self):
        guest_id = uuid4()
        token_service = MagicMock()
        token_service.get_balance.side_effect = [9, 4]  # before, after
        guest_session_repo = MagicMock()
        guest_session_repo.find_by_guest_user_id.return_value = _session_row(guest_id)
        service = _build_service(
            token_service=token_service, guest_session_repo=guest_session_repo
        )

        new_balance = service.reset(guest_id, 4)

        token_service.debit_tokens.assert_called_once()
        assert token_service.debit_tokens.call_args.args[1] == 5
        token_service.credit_tokens.assert_not_called()
        assert new_balance == 4

    def test_reset_equal_balance_is_a_no_op(self):
        guest_id = uuid4()
        token_service = MagicMock()
        token_service.get_balance.return_value = 6
        guest_session_repo = MagicMock()
        guest_session_repo.find_by_guest_user_id.return_value = _session_row(guest_id)
        service = _build_service(
            token_service=token_service, guest_session_repo=guest_session_repo
        )

        new_balance = service.reset(guest_id, 6)

        token_service.credit_tokens.assert_not_called()
        token_service.debit_tokens.assert_not_called()
        assert new_balance == 6

    @pytest.mark.parametrize("bad_target", [-1, -100])
    def test_reset_rejects_negative_target(self, bad_target):
        guest_id = uuid4()
        guest_session_repo = MagicMock()
        guest_session_repo.find_by_guest_user_id.return_value = _session_row(guest_id)
        service = _build_service(guest_session_repo=guest_session_repo)

        with pytest.raises(ValueError):
            service.reset(guest_id, bad_target)

    def test_reset_unknown_guest_raises_guest_not_found(self):
        guest_session_repo = MagicMock()
        guest_session_repo.find_by_guest_user_id.return_value = None
        service = _build_service(guest_session_repo=guest_session_repo)

        with pytest.raises(GuestNotFoundError):
            service.reset(uuid4(), 5)


class TestBulk:
    def test_topup_all_applies_to_every_distinct_guest(self):
        guest_ids = [uuid4(), uuid4(), uuid4()]
        token_service = MagicMock()
        token_service.get_balance.return_value = 0
        guest_session_repo = MagicMock()
        guest_session_repo.all_guest_user_ids.return_value = guest_ids
        guest_session_repo.find_by_guest_user_id.side_effect = lambda gid: _session_row(
            gid
        )
        service = _build_service(
            token_service=token_service, guest_session_repo=guest_session_repo
        )

        affected = service.topup_all(5)

        assert affected == 3
        assert token_service.credit_tokens.call_count == 3

    def test_reset_all_applies_to_every_distinct_guest(self):
        guest_ids = [uuid4(), uuid4()]
        token_service = MagicMock()
        token_service.get_balance.return_value = 0
        guest_session_repo = MagicMock()
        guest_session_repo.all_guest_user_ids.return_value = guest_ids
        guest_session_repo.find_by_guest_user_id.side_effect = lambda gid: _session_row(
            gid
        )
        service = _build_service(
            token_service=token_service, guest_session_repo=guest_session_repo
        )

        affected = service.reset_all(20)

        assert affected == 2


class TestListGuests:
    def test_list_dedups_and_enriches_balance_and_display_name(self):
        guest_id = uuid4()
        token_service = MagicMock()
        token_service.get_balance.return_value = 17
        guest_session_repo = MagicMock()
        guest_session_repo.list_distinct_guests.return_value = {
            "items": [_session_row(guest_id, display_name="Session Name")],
            "total": 1,
        }
        nickname_repo = MagicMock()
        nickname_repo.find_by_user_id.return_value = SimpleNamespace(nickname="nick-x")
        service = _build_service(
            token_service=token_service,
            guest_session_repo=guest_session_repo,
            nickname_repo=nickname_repo,
        )

        result = service.list_guests(page=1, per_page=50)

        assert result["total"] == 1
        assert result["page"] == 1
        assert result["per_page"] == 50
        assert len(result["items"]) == 1
        item = result["items"][0]
        assert item["guest_user_id"] == str(guest_id)
        assert item["balance"] == 17
        # nickname wins over the session display_name when present.
        assert item["display_name"] == "nick-x"

    def test_list_falls_back_to_session_display_name_without_nickname(self):
        guest_id = uuid4()
        token_service = MagicMock()
        token_service.get_balance.return_value = 0
        guest_session_repo = MagicMock()
        guest_session_repo.list_distinct_guests.return_value = {
            "items": [_session_row(guest_id, display_name="Session Name")],
            "total": 1,
        }
        nickname_repo = MagicMock()
        nickname_repo.find_by_user_id.return_value = None
        service = _build_service(
            token_service=token_service,
            guest_session_repo=guest_session_repo,
            nickname_repo=nickname_repo,
        )

        result = service.list_guests(page=1, per_page=50)

        assert result["items"][0]["display_name"] == "Session Name"
