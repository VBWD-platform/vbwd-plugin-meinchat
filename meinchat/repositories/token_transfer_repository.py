"""Data access for token_transfer rows."""
from typing import List
from uuid import UUID

from plugins.meinchat.meinchat.models.token_transfer import TokenTransferRecord


class TokenTransferRepository:
    def __init__(self, session) -> None:
        self._session = session

    def save(self, row: TokenTransferRecord) -> TokenTransferRecord:
        self._session.add(row)
        self._session.flush()
        return row

    def list_for_user(
        self, user_id: UUID, *, direction: str = "all"
    ) -> List[TokenTransferRecord]:
        """direction: 'in' (received), 'out' (sent), 'all'."""
        query = self._session.query(TokenTransferRecord)
        if direction == "in":
            query = query.filter(TokenTransferRecord.recipient_user_id == user_id)
        elif direction == "out":
            query = query.filter(TokenTransferRecord.sender_user_id == user_id)
        else:
            query = query.filter(
                (TokenTransferRecord.sender_user_id == user_id)
                | (TokenTransferRecord.recipient_user_id == user_id)
            )
        return query.order_by(TokenTransferRecord.executed_at.desc()).all()
