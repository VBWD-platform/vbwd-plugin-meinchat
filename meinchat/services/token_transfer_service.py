"""Peer-to-peer token transfer — atomic deduct + credit + audit + system msg."""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from plugins.meinchat.meinchat.models.token_transfer import TokenTransferRecord
from plugins.meinchat.meinchat.repositories.nickname_repository import (
    NicknameRepository,
)
from plugins.meinchat.meinchat.repositories.token_transfer_repository import (
    TokenTransferRepository,
)
from plugins.meinchat.meinchat.services.nickname_service import (
    NicknameNotFoundError,
)


class InsufficientTokensError(Exception):
    """Sender doesn't have enough tokens for the requested transfer."""


class SelfTransferError(ValueError):
    """Sender and recipient are the same user."""


class TokenTransferService:
    """Move N tokens from sender to recipient, record it, and (optionally)
    drop a system message into their shared conversation so both parties
    see the transfer inline in their chat timeline."""

    def __init__(
        self,
        transfer_repo: TokenTransferRepository,
        token_service: Any,  # vbwd.services.token_service.TokenService
        nickname_repo: NicknameRepository,
        conversation_service: Optional[Any] = None,
        message_service: Optional[Any] = None,
    ) -> None:
        self._transfer_repo = transfer_repo
        self._token_service = token_service
        self._nickname_repo = nickname_repo
        self._conv_service = conversation_service
        self._message_service = message_service

    def transfer(
        self,
        *,
        sender_user_id: UUID,
        recipient_nickname: str,
        amount: int,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Strict int check — floats and booleans shouldn't slip through.
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise ValueError("amount must be a positive integer")
        if amount <= 0:
            raise ValueError("amount must be a positive integer")

        target = self._nickname_repo.find_by_nickname_ci(recipient_nickname)
        if target is None or target.banned or target.search_hidden:
            raise NicknameNotFoundError(f"'{recipient_nickname}' not found")
        if target.user_id == sender_user_id:
            raise SelfTransferError("cannot send tokens to yourself")

        sender_nick = self._nickname_repo.find_by_user_id(sender_user_id)
        sender_nickname = sender_nick.nickname if sender_nick else ""

        # Import here to avoid a circular when the plugin boots.
        from vbwd.models.enums import TokenTransactionType

        # Deduct first — if the sender is short, this raises and no credit
        # happens. The real DB concurrency guarantee lives in the caller's
        # transaction (route commits on success).
        try:
            balance = self._token_service.debit_tokens(
                sender_user_id,
                amount,
                TokenTransactionType.ADJUSTMENT,
                description=f"transfer out to @{target.nickname}",
            )
        except ValueError as exc:
            raise InsufficientTokensError(str(exc)) from exc

        self._token_service.credit_tokens(
            target.user_id,
            amount,
            TokenTransactionType.ADJUSTMENT,
            description=f"transfer in from @{sender_nickname}",
        )

        record = TokenTransferRecord()
        record.sender_user_id = sender_user_id
        record.recipient_user_id = target.user_id
        record.amount = amount
        record.note = note
        record.executed_at = datetime.now(timezone.utc)
        self._transfer_repo.save(record)

        if self._conv_service is not None and self._message_service is not None:
            conv = self._conv_service.start_or_get(sender_user_id, target.user_id)
            self._message_service.post_system_message(
                conversation_id=conv.id,
                sender_user_id=sender_user_id,
                system_kind="token_transfer",
                payload={
                    "amount": amount,
                    "note": note,
                    "from_nickname": sender_nickname,
                    "to_nickname": target.nickname,
                    "transfer_id": str(record.id),
                },
            )

        return {
            "transfer_id": str(record.id),
            "recipient_user_id": target.user_id,
            "recipient_nickname": target.nickname,
            "amount": amount,
            "new_balance": balance.balance if balance is not None else None,
        }

    def list_history(
        self, user_id: UUID, *, direction: str = "all"
    ) -> List[TokenTransferRecord]:
        if direction not in ("in", "out", "all"):
            raise ValueError("direction must be 'in', 'out', or 'all'")
        return self._transfer_repo.list_for_user(user_id, direction=direction)
