"""Conversation model — pair key with ordered low/high for uniqueness."""
from sqlalchemy.dialects.postgresql import JSONB

from vbwd.extensions import db
from vbwd.models.base import BaseModel


class Conversation(BaseModel):
    """One row per unordered pair (a, b). `participant_low_id` is always
    the byte-order-smaller UUID; `participant_high_id` the larger. This
    removes the A↔B / B↔A duplication concern at the DB level."""

    __tablename__ = "conversation"
    __table_args__ = (
        db.UniqueConstraint(
            "participant_low_id",
            "participant_high_id",
            name="uq_conversation_pair",
        ),
        db.CheckConstraint(
            "participant_low_id <> participant_high_id",
            name="ck_conversation_no_self",
        ),
        db.CheckConstraint(
            "participant_low_id < participant_high_id",
            name="ck_conversation_ordered",
        ),
    )

    participant_low_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    participant_high_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    last_message_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_message_preview = db.Column(db.String(120), nullable=True)
    unread_low_count = db.Column(db.Integer, nullable=False, default=0)
    unread_high_count = db.Column(db.Integer, nullable=False, default=0)
    # S28.3a — negotiated protocol, pinned at creation (immutable: the route
    # refuses to change it, and a migration trigger enforces it in prod).
    protocol = db.Column(
        db.String(32), nullable=False, server_default="plain", default="plain"
    )
    # Capabilities advertised for this conversation (subset agreed by both).
    capabilities = db.Column(JSONB, nullable=False, server_default="[]", default=list)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "participant_low_id": str(self.participant_low_id),
            "participant_high_id": str(self.participant_high_id),
            "last_message_at": (
                self.last_message_at.isoformat() if self.last_message_at else None
            ),
            "last_message_preview": self.last_message_preview,
            "unread_low_count": self.unread_low_count,
            "unread_high_count": self.unread_high_count,
            "protocol": self.protocol,
            "capabilities": self.capabilities or [],
        }
