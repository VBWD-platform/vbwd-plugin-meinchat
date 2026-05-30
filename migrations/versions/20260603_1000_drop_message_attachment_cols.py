"""meinchat S28.4 — drop legacy message.attachment_* columns.

Attachments now live entirely in the `meinchat_attachment` child table
(plain `fullres`+`thumb` rows, or e2e client-encrypted rows). The per-row
columns on `message` are removed.

NO BACKFILL: the platform is pre-rollout (no production attachment data to
preserve) — confirmed with the product owner. The downgrade re-adds the
columns as nullable (data is not restored).

Chains off the child-table create migration so the meinchat chain stays
linear + standalone-resolvable.
"""
import sqlalchemy as sa
from alembic import op

revision = "20260603_1000_drop_msg_attach_cols"
down_revision = "20260602_1000_meinchat_attachment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("message", "attachment_url")
    op.drop_column("message", "attachment_thumb_url")
    op.drop_column("message", "attachment_width_px")
    op.drop_column("message", "attachment_height_px")


def downgrade() -> None:
    op.add_column(
        "message", sa.Column("attachment_url", sa.String(length=512), nullable=True)
    )
    op.add_column(
        "message",
        sa.Column("attachment_thumb_url", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "message", sa.Column("attachment_width_px", sa.Integer(), nullable=True)
    )
    op.add_column(
        "message", sa.Column("attachment_height_px", sa.Integer(), nullable=True)
    )
