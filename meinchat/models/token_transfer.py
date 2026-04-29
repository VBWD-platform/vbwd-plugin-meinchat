"""TokenTransferRecord model — audit trail of peer-to-peer transfers."""
from vbwd.extensions import db
from vbwd.models.base import BaseModel


class TokenTransferRecord(BaseModel):
    """One row per completed peer-to-peer token transfer."""

    __tablename__ = "token_transfer"
    __table_args__ = (
        db.CheckConstraint("amount > 0", name="ck_token_transfer_positive"),
        db.Index(
            "ix_token_transfer_sender_executed",
            "sender_user_id",
            "executed_at",
        ),
        db.Index(
            "ix_token_transfer_recipient_executed",
            "recipient_user_id",
            "executed_at",
        ),
    )

    sender_user_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    recipient_user_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    amount = db.Column(db.Integer, nullable=False)
    note = db.Column(db.String(200), nullable=True)
    executed_at = db.Column(db.DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "sender_user_id": str(self.sender_user_id),
            "recipient_user_id": str(self.recipient_user_id),
            "amount": self.amount,
            "note": self.note,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
        }
