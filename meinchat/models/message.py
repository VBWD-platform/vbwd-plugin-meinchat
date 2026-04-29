"""Message model — text / image / system (token_transfer) variants."""
from vbwd.extensions import db
from vbwd.models.base import BaseModel


class Message(BaseModel):
    """One line in a conversation. Hard-deleted on both sides (Q4)."""

    __tablename__ = "message"
    __table_args__ = (
        db.CheckConstraint("length(body) <= 4000", name="ck_message_body_len"),
        db.Index("ix_message_conversation_sent", "conversation_id", "sent_at"),
    )

    conversation_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender_nickname = db.Column(db.String(32), nullable=False)
    body = db.Column(db.Text, nullable=False, default="")

    # Attachment fields land in the next slice; declare now so the
    # migration doesn't need a follow-up ALTER.
    attachment_url = db.Column(db.String(512), nullable=True)
    attachment_thumb_url = db.Column(db.String(512), nullable=True)
    attachment_width_px = db.Column(db.Integer, nullable=True)
    attachment_height_px = db.Column(db.Integer, nullable=True)

    sent_at = db.Column(db.DateTime(timezone=True), nullable=False)
    delivered_at = db.Column(db.DateTime(timezone=True), nullable=True)
    read_at = db.Column(db.DateTime(timezone=True), nullable=True)
    system_kind = db.Column(db.String(32), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "conversation_id": str(self.conversation_id),
            "sender_id": str(self.sender_id),
            "sender_nickname": self.sender_nickname,
            "body": self.body,
            "attachment_url": self.attachment_url,
            "attachment_thumb_url": self.attachment_thumb_url,
            "attachment_width_px": self.attachment_width_px,
            "attachment_height_px": self.attachment_height_px,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "system_kind": self.system_kind,
        }
