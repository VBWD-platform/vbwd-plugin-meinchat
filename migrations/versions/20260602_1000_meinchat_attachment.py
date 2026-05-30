"""meinchat S28.4 — meinchat_attachment child table (additive).

Adds the `meinchat_attachment` child table that carries one row per stored
blob (`fullres` / `thumb`), each with a `protocol` discriminator and an
optional `envelope_header` (per-recipient wrapped key for e2e_v1 rows).

ADDITIVE ONLY in this slice: the existing `message.attachment_*` columns and
the server-resize plain path are untouched, so plaintext attachments behave
exactly as today. e2e_v1 (client-encrypted) attachments are persisted as
child rows. Folding plain attachments into the child table + dropping the
`message.attachment_*` columns is a later, destructive increment.

Chains off the meinchat e2e head (NOT the meinchat-plus migration) so
meinchat's chain still resolves standalone when meinchat-plus isn't cloned.
"""
import sqlalchemy as sa
from alembic import op

revision = "20260602_1000_meinchat_attachment"
down_revision = "20260528_1100_meinchat_e2e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "meinchat_attachment",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "message_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("message.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("storage_url", sa.Text(), nullable=False),
        sa.Column(
            "protocol", sa.String(length=32), nullable=False, server_default="plain"
        ),
        sa.Column("envelope_header", sa.JSON(), nullable=True),
        sa.Column("mime", sa.String(length=64), nullable=False),
        sa.Column("bytes_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("width_px", sa.Integer(), nullable=True),
        sa.Column("height_px", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "message_id", "kind", name="meinchat_attachment_one_kind_per_message"
        ),
        sa.CheckConstraint(
            "(protocol = 'plain' AND envelope_header IS NULL)"
            " OR (protocol <> 'plain' AND envelope_header IS NOT NULL)",
            name="meinchat_attachment_protocol_or_envelope",
        ),
        sa.CheckConstraint(
            "kind IN ('fullres', 'thumb')", name="meinchat_attachment_kind_valid"
        ),
    )
    op.create_index(
        "meinchat_attachment_message_idx", "meinchat_attachment", ["message_id"]
    )


def downgrade() -> None:
    op.drop_index("meinchat_attachment_message_idx", table_name="meinchat_attachment")
    op.drop_table("meinchat_attachment")
