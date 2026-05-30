"""Message model — text / image / system (token_transfer) variants.

S28.3a adds the e2e columns: `protocol` ('plain' | 'e2e_v1' | …), `envelope`
(opaque ciphertext for non-plain rows), and
`delivered_to_all_addressed_devices_at` (drives the E2e-aware retention prune).
For plain rows `body` is populated and `envelope` is NULL; for e2e rows it is
the reverse — enforced by the `ck_message_body_or_envelope` constraint.
"""
import base64

from vbwd.extensions import db
from vbwd.models.base import BaseModel


class Message(BaseModel):
    """One line in a conversation. Hard-deleted on both sides (Q4)."""

    __tablename__ = "message"
    __table_args__ = (
        # Plain rows: body present and within the length cap.
        db.CheckConstraint(
            "protocol <> 'plain' OR (body IS NOT NULL AND length(body) <= 4000)",
            name="ck_message_body_len",
        ),
        # Exactly one of body / envelope is populated, matching the protocol.
        db.CheckConstraint(
            "(protocol = 'plain' AND body IS NOT NULL AND envelope IS NULL)"
            " OR (protocol <> 'plain' AND body IS NULL AND envelope IS NOT NULL)",
            name="ck_message_body_or_envelope",
        ),
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
    # Plain text (NULL for e2e rows — see ck_message_body_or_envelope).
    body = db.Column(db.Text, nullable=True)
    # Opaque encrypted envelope (NULL for plain rows).
    envelope = db.Column(db.LargeBinary, nullable=True)
    protocol = db.Column(
        db.String(32), nullable=False, server_default="plain", default="plain"
    )
    # S28.4 — attachments moved to the `meinchat_attachment` child table
    # (`self.attachments`); the legacy per-row attachment_* columns are gone.

    sent_at = db.Column(db.DateTime(timezone=True), nullable=False)
    delivered_at = db.Column(db.DateTime(timezone=True), nullable=True)
    read_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Set once every addressed device has fetched an e2e row — lets the
    # S28.1 prune exempt undelivered ciphertext (async first-message delivery).
    delivered_to_all_addressed_devices_at = db.Column(
        db.DateTime(timezone=True), nullable=True
    )
    system_kind = db.Column(db.String(32), nullable=True)

    # S28.4 — child attachment blobs (fullres/thumb). String class ref avoids
    # an import cycle; selectin keeps to_dict's iteration a single extra query.
    attachments = db.relationship(
        "MeinchatAttachment",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def to_dict(self) -> dict:
        result = {
            "id": str(self.id),
            "conversation_id": str(self.conversation_id),
            "sender_id": str(self.sender_id),
            "sender_nickname": self.sender_nickname,
            "body": self.body,
            "protocol": self.protocol,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "delivered_at": self.delivered_at.isoformat()
            if self.delivered_at
            else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "system_kind": self.system_kind,
            # S28.4 — attachment blobs (fullres/thumb), empty for none.
            "attachments": [a.to_dict() for a in self.attachments],
        }
        if self.envelope is not None:
            result["envelope"] = base64.b64encode(self.envelope).decode("ascii")
        return result
