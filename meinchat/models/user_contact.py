"""UserContact model — a personal address-book entry."""
from vbwd.extensions import db
from vbwd.models.base import BaseModel


class UserContact(BaseModel):
    """One contact (peer user) saved by the `owner` user."""

    __tablename__ = "meinchat_user_contact"
    __table_args__ = (
        db.UniqueConstraint(
            "owner_user_id",
            "contact_user_id",
            name="uq_meinchat_user_contact_owner_contact",
        ),
        db.CheckConstraint(
            "owner_user_id <> contact_user_id",
            name="ck_meinchat_user_contact_no_self",
        ),
    )

    owner_user_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contact_user_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    alias = db.Column(db.String(64), nullable=True)
    note = db.Column(db.String(500), nullable=True)
    pinned = db.Column(db.Boolean, nullable=False, default=False)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "owner_user_id": str(self.owner_user_id),
            "contact_user_id": str(self.contact_user_id),
            "alias": self.alias,
            "note": self.note,
            "pinned": self.pinned,
            "added_at": self.created_at.isoformat() if self.created_at else None,
        }
