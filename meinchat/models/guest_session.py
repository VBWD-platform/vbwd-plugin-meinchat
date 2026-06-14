"""Guest widget-session model (S86.3, D5).

One row per public bot-widget ``Start Conversation`` that provisions a core
``GUEST`` user. It records which guest user backs which widget room so a later
slice (D12 — session reuse + the token grant, S86.3 3c) can build return-visitor
reuse on top of it. This slice always provisions a fresh guest and writes one
row per start; reuse / idempotency is deferred to 3c.
"""
from vbwd.extensions import db
from vbwd.models.base import BaseModel


class MeinchatGuestSession(BaseModel):
    """A provisioned guest's widget session (guest user ↔ widget ↔ room)."""

    __tablename__ = "meinchat_guest_session"

    guest_user_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    widget_slug = db.Column(db.String(255), nullable=False, index=True)
    room_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("meinchat_room.id", ondelete="SET NULL"),
        nullable=True,
    )
    display_name = db.Column(db.String(120), nullable=True)
    # The JWT id of the guest's minted access token, so 3c can reuse / revoke a
    # presented session without re-provisioning.
    token_jti = db.Column(db.String(64), nullable=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "guest_user_id": str(self.guest_user_id),
            "widget_slug": self.widget_slug,
            "room_id": str(self.room_id) if self.room_id else None,
            "display_name": self.display_name,
            "token_jti": self.token_jti,
            "expires_at": (self.expires_at.isoformat() if self.expires_at else None),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
