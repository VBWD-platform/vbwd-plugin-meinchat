"""Attachment child table (S28.4).

One row per stored blob. A `plain` message has one or two rows (fullres +
optional thumb, server-resized as today); an `e2e_v1` message has two
client-encrypted rows (`fullres` + `thumb`) — the server stores opaque bytes
and never resizes ciphertext. `envelope_header` carries the per-recipient
wrapped symmetric key for non-plain rows; it is NULL for plain.

Replaces the per-row `message.attachment_*` columns (which can't carry the
fullres+thumb pair for e2e). Plain `db.Model` (no optimistic-lock version) —
attachments are write-once + hard-deleted with their message.
"""
from uuid import uuid4

from vbwd.extensions import db

_KIND_FULLRES = "fullres"
_KIND_THUMB = "thumb"


class MeinchatAttachment(db.Model):  # type: ignore[name-defined]
    """A single stored attachment blob belonging to a message."""

    __tablename__ = "meinchat_attachment"
    __table_args__ = (
        db.UniqueConstraint(
            "message_id", "kind", name="meinchat_attachment_one_kind_per_message"
        ),
        db.CheckConstraint(
            "(protocol = 'plain' AND envelope_header IS NULL)"
            " OR (protocol <> 'plain' AND envelope_header IS NOT NULL)",
            name="meinchat_attachment_protocol_or_envelope",
        ),
        db.CheckConstraint(
            "kind IN ('fullres', 'thumb')",
            name="meinchat_attachment_kind_valid",
        ),
        db.Index("meinchat_attachment_message_idx", "message_id"),
    )

    id = db.Column(db.UUID(as_uuid=True), primary_key=True, default=uuid4)
    message_id = db.Column(
        db.UUID(as_uuid=True),
        db.ForeignKey("message.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 'fullres' | 'thumb' — kept as a checked VARCHAR rather than a PG ENUM so
    # create_all (tests) and the migration agree without a CREATE TYPE dance.
    kind = db.Column(db.String(16), nullable=False)
    storage_url = db.Column(db.Text, nullable=False)
    protocol = db.Column(
        db.String(32), nullable=False, server_default="plain", default="plain"
    )
    # Per-recipient wrapped key material for e2e_v1 rows; NULL for plain.
    # none_as_null so a Python None persists as SQL NULL (not JSON 'null'),
    # which the protocol/envelope CHECK constraint relies on.
    envelope_header = db.Column(db.JSON(none_as_null=True), nullable=True)
    mime = db.Column(db.String(64), nullable=False)
    bytes_count = db.Column(db.Integer, nullable=False, default=0)
    # Pixel dimensions (server-set for plain `fullres`; client-set or NULL for
    # e2e, where the server can't measure ciphertext). Drive fe image layout.
    width_px = db.Column(db.Integer, nullable=True)
    height_px = db.Column(db.Integer, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=db.func.now()
    )

    def to_dict(self) -> dict:
        result = {
            "id": str(self.id),
            "kind": self.kind,
            "storage_url": self.storage_url,
            "protocol": self.protocol,
            "mime": self.mime,
            "bytes_count": self.bytes_count,
            "width_px": self.width_px,
            "height_px": self.height_px,
        }
        if self.envelope_header is not None:
            result["envelope_header"] = self.envelope_header
        return result
