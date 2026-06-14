"""Room membership model — who belongs to a room and in what role (S86.1, D1).

Roles are a small fixed set (`admin` / `member`) enforced by a CHECK constraint,
NOT a new PG enum — a two-value set does not earn an enum type and an enum would
need its own migration to extend. `UNIQUE(room_id, user_id)` keeps membership
idempotent (a re-invite is a no-op, not a duplicate row).
"""
from vbwd.extensions import db
from vbwd.models.base import BaseModel

ROOM_ROLE_ADMIN = "admin"
ROOM_ROLE_MEMBER = "member"
ROOM_ROLES = (ROOM_ROLE_ADMIN, ROOM_ROLE_MEMBER)


class RoomMember(BaseModel):
    """One row per (room, user). `role` gates rename / remove-other."""

    __tablename__ = "meinchat_room_member"
    __table_args__ = (
        db.UniqueConstraint(
            "room_id", "user_id", name="uq_meinchat_room_member_room_user"
        ),
        db.CheckConstraint(
            "role IN ('admin', 'member')",
            name="ck_meinchat_room_member_role",
        ),
    )

    room_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("meinchat_room.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = db.Column(db.String(16), nullable=False, default=ROOM_ROLE_MEMBER)
    invited_by_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id"),
        nullable=True,
    )
    joined_at = db.Column(db.DateTime(timezone=True), nullable=False)
    unread_count = db.Column(db.Integer, nullable=False, default=0)
    last_read_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "room_id": str(self.room_id),
            "user_id": str(self.user_id),
            "role": self.role,
            "invited_by_id": (str(self.invited_by_id) if self.invited_by_id else None),
            "joined_at": self.joined_at.isoformat() if self.joined_at else None,
            "unread_count": self.unread_count,
            "last_read_at": (
                self.last_read_at.isoformat() if self.last_read_at else None
            ),
        }
