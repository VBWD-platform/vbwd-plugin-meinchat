"""meinchat: create token_transfer table (peer-to-peer audit log).

Revision ID: 20260424_1015
Revises: 20260424_1010
Create Date: 2026-04-24
"""
from alembic import op
import sqlalchemy as sa


revision = "20260424_1015"
down_revision = "20260424_1010"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    if _table_exists(conn, "token_transfer"):
        return

    op.create_table(
        "token_transfer",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "sender_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recipient_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("amount > 0", name="ck_token_transfer_positive"),
    )
    op.create_index(
        "ix_token_transfer_sender_executed",
        "token_transfer",
        ["sender_user_id", "executed_at"],
    )
    op.create_index(
        "ix_token_transfer_recipient_executed",
        "token_transfer",
        ["recipient_user_id", "executed_at"],
    )


def downgrade():
    op.drop_index("ix_token_transfer_recipient_executed", table_name="token_transfer")
    op.drop_index("ix_token_transfer_sender_executed", table_name="token_transfer")
    op.drop_table("token_transfer")


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        sa.text("SELECT 1 FROM information_schema.tables WHERE table_name = :name"),
        {"name": table_name},
    )
    return result.scalar() is not None
