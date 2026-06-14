"""Room model — an N-party meinchat conversation (S86.1, D1).

A room generalises the pair-keyed `Conversation` to multi-party membership:
who belongs (and in what role) lives in `meinchat_room_member`, while the room
row itself carries the pinned `protocol`/`capabilities` (negotiated once at
creation, exactly like a conversation) plus the inbox-summary columns. Messages
reuse `meinchat_message` via a nullable `room_id` (D2), so there is a single
message pipeline.
"""
from sqlalchemy.dialects.postgresql import JSONB

from vbwd.extensions import db
from vbwd.models.base import BaseModel


class Room(BaseModel):
    """One row per multi-party room. Membership lives in `RoomMember`."""

    __tablename__ = "meinchat_room"

    name = db.Column(db.String(120), nullable=True)
    created_by_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id"),
        nullable=False,
        index=True,
    )
    # Negotiated protocol, pinned at creation (mirrors Conversation.protocol).
    protocol = db.Column(
        db.String(32), nullable=False, server_default="plain", default="plain"
    )
    # Capabilities advertised for this room (subset agreed by all members).
    capabilities = db.Column(JSONB, nullable=False, server_default="[]", default=list)
    # S86.3 populates this when a room backs a public bot widget; just the
    # column here so the migration is single-shot.
    widget_slug = db.Column(db.String(255), nullable=True)
    last_message_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_message_preview = db.Column(db.String(120), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "created_by_id": str(self.created_by_id),
            "protocol": self.protocol,
            "capabilities": self.capabilities or [],
            "widget_slug": self.widget_slug,
            "last_message_at": (
                self.last_message_at.isoformat() if self.last_message_at else None
            ),
            "last_message_preview": self.last_message_preview,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
