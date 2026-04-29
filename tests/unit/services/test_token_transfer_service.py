"""Tests for TokenTransferService — deduct + credit + record, atomic."""
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from plugins.meinchat.meinchat.services.nickname_service import (
    NicknameNotFoundError,
)
from plugins.meinchat.meinchat.services.token_transfer_service import (
    InsufficientTokensError,
    SelfTransferError,
    TokenTransferService,
)


def _nickname_row(user_id, nickname, *, banned=False, search_hidden=False):
    from plugins.meinchat.meinchat.models.user_nickname import UserNickname

    row = UserNickname()
    row.id = uuid4()
    row.user_id = user_id
    row.nickname = nickname
    row.banned = banned
    row.search_hidden = search_hidden
    return row


@pytest.fixture
def transfer_repo():
    r = MagicMock()
    r.save.side_effect = lambda row: row
    return r


@pytest.fixture
def nickname_repo():
    return MagicMock()


@pytest.fixture
def token_service():
    svc = MagicMock()
    # Balance stored on the object so post-transfer reads are meaningful.
    svc._sender_balance = 100
    svc._recipient_balance = 0

    def debit(user_id, amount, transaction_type, **kwargs):
        if svc._sender_balance < amount:
            raise ValueError("Insufficient token balance")
        svc._sender_balance -= amount
        balance = MagicMock()
        balance.balance = svc._sender_balance
        return balance

    def credit(user_id, amount, transaction_type, **kwargs):
        svc._recipient_balance += amount
        balance = MagicMock()
        balance.balance = svc._recipient_balance
        return balance

    svc.debit_tokens.side_effect = debit
    svc.credit_tokens.side_effect = credit
    return svc


@pytest.fixture
def service(transfer_repo, token_service, nickname_repo):
    return TokenTransferService(
        transfer_repo=transfer_repo,
        token_service=token_service,
        nickname_repo=nickname_repo,
    )


class TestHappyPath:
    def test_transfers_tokens_and_records_row(
        self, service, transfer_repo, token_service, nickname_repo
    ):
        alice = uuid4()
        bob = uuid4()
        nickname_repo.find_by_user_id.return_value = _nickname_row(alice, "alice")
        nickname_repo.find_by_nickname_ci.return_value = _nickname_row(bob, "bob")

        result = service.transfer(
            sender_user_id=alice,
            recipient_nickname="bob",
            amount=10,
            note="pizza",
        )

        assert result["transfer_id"] is not None
        assert result["recipient_user_id"] == bob
        assert result["amount"] == 10
        assert result["new_balance"] == 90
        transfer_repo.save.assert_called_once()
        assert token_service.debit_tokens.call_count == 1
        assert token_service.credit_tokens.call_count == 1


class TestRejections:
    def test_insufficient_balance(self, service, token_service, nickname_repo):
        token_service._sender_balance = 5
        alice, bob = uuid4(), uuid4()
        nickname_repo.find_by_user_id.return_value = _nickname_row(alice, "alice")
        nickname_repo.find_by_nickname_ci.return_value = _nickname_row(bob, "bob")

        with pytest.raises(InsufficientTokensError):
            service.transfer(sender_user_id=alice, recipient_nickname="bob", amount=10)
        # No credit must have fired if debit raised.
        token_service.credit_tokens.assert_not_called()

    def test_self_transfer(self, service, nickname_repo):
        alice = uuid4()
        nickname_repo.find_by_user_id.return_value = _nickname_row(alice, "alice")
        nickname_repo.find_by_nickname_ci.return_value = _nickname_row(alice, "alice")
        with pytest.raises(SelfTransferError):
            service.transfer(
                sender_user_id=alice, recipient_nickname="alice", amount=10
            )

    def test_unknown_nickname(self, service, nickname_repo):
        nickname_repo.find_by_user_id.return_value = _nickname_row(uuid4(), "me")
        nickname_repo.find_by_nickname_ci.return_value = None
        with pytest.raises(NicknameNotFoundError):
            service.transfer(
                sender_user_id=uuid4(), recipient_nickname="ghost", amount=5
            )

    def test_banned_nickname(self, service, nickname_repo):
        sender = uuid4()
        nickname_repo.find_by_user_id.return_value = _nickname_row(sender, "alice")
        nickname_repo.find_by_nickname_ci.return_value = _nickname_row(
            uuid4(), "spammer", banned=True
        )
        with pytest.raises(NicknameNotFoundError):
            service.transfer(
                sender_user_id=sender,
                recipient_nickname="spammer",
                amount=5,
            )

    @pytest.mark.parametrize("amount", [0, -1, -100])
    def test_non_positive_amount(self, service, nickname_repo, amount):
        alice, bob = uuid4(), uuid4()
        nickname_repo.find_by_user_id.return_value = _nickname_row(alice, "alice")
        nickname_repo.find_by_nickname_ci.return_value = _nickname_row(bob, "bob")
        with pytest.raises(ValueError):
            service.transfer(
                sender_user_id=alice, recipient_nickname="bob", amount=amount
            )

    def test_rejects_float_amount(self, service, nickname_repo):
        alice, bob = uuid4(), uuid4()
        nickname_repo.find_by_user_id.return_value = _nickname_row(alice, "alice")
        nickname_repo.find_by_nickname_ci.return_value = _nickname_row(bob, "bob")
        with pytest.raises(ValueError):
            service.transfer(
                sender_user_id=alice, recipient_nickname="bob", amount=10.5
            )


class TestHistory:
    def test_delegates_to_repo(self, service, transfer_repo):
        user = uuid4()
        transfer_repo.list_for_user.return_value = []
        service.list_history(user, direction="all")
        transfer_repo.list_for_user.assert_called_once_with(user, direction="all")


class TestIntegrationWithMessaging:
    def test_posts_system_message_to_conversation(
        self, transfer_repo, token_service, nickname_repo
    ):
        """When a conversation_service + message_service is injected, a
        system_kind='token_transfer' message appears on the shared
        conversation between sender and recipient."""
        conv_service = MagicMock()
        msg_service = MagicMock()
        conv = MagicMock()
        conv.id = uuid4()
        conv_service.start_or_get.return_value = conv

        service = TokenTransferService(
            transfer_repo=transfer_repo,
            token_service=token_service,
            nickname_repo=nickname_repo,
            conversation_service=conv_service,
            message_service=msg_service,
        )

        alice, bob = uuid4(), uuid4()
        nickname_repo.find_by_user_id.return_value = _nickname_row(alice, "alice")
        nickname_repo.find_by_nickname_ci.return_value = _nickname_row(bob, "bob")

        service.transfer(
            sender_user_id=alice, recipient_nickname="bob", amount=25, note="lunch"
        )

        conv_service.start_or_get.assert_called_once_with(alice, bob)
        msg_service.post_system_message.assert_called_once()
        kwargs = msg_service.post_system_message.call_args.kwargs
        assert kwargs["conversation_id"] == conv.id
        assert kwargs["sender_user_id"] == alice
        assert kwargs["system_kind"] == "token_transfer"
        assert kwargs["payload"]["amount"] == 25
        assert kwargs["payload"]["note"] == "lunch"
