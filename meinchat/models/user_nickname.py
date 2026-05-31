"""UserNickname model — user's public slug, the key to finding them."""
from vbwd.extensions import db
from vbwd.models.base import BaseModel


class UserNickname(BaseModel):
    """One nickname row per user. `nickname_ci` is the unique lookup key."""

    __tablename__ = "meinchat_user_nickname"

    user_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    nickname = db.Column(db.String(32), nullable=False)
    nickname_ci = db.Column(
        db.String(32),
        db.Computed("lower(nickname)", persisted=True),
        nullable=False,
        unique=True,
        index=True,
    )
    set_at = db.Column(db.DateTime(timezone=True), nullable=True)
    banned = db.Column(db.Boolean, nullable=False, default=False)
    banned_at = db.Column(db.DateTime(timezone=True), nullable=True)
    search_hidden = db.Column(db.Boolean, nullable=False, default=False)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "nickname": self.nickname,
            "set_at": self.set_at.isoformat() if self.set_at else None,
            "banned": self.banned,
            "banned_at": self.banned_at.isoformat() if self.banned_at else None,
            "search_hidden": self.search_hidden,
        }
