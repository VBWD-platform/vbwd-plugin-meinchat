"""meinchat: create conversation + message tables.

Revision ID: 20260424_1010
Revises: 20260424_1005
Create Date: 2026-04-24
"""
from alembic import op
import sqlalchemy as sa


revision = "20260424_1010"
down_revision = "20260424_1005"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    if not _table_exists(conn, "conversation"):
        op.create_table(
            "conversation",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "participant_low_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "participant_high_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_message_preview", sa.String(120), nullable=True),
            sa.Column(
                "unread_low_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "unread_high_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column(
                "version", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "participant_low_id",
                "participant_high_id",
                name="uq_conversation_pair",
            ),
            sa.CheckConstraint(
                "participant_low_id <> participant_high_id",
                name="ck_conversation_no_self",
            ),
            sa.CheckConstraint(
                "participant_low_id < participant_high_id",
                name="ck_conversation_ordered",
            ),
        )
        op.create_index(
            "ix_conversation_participant_low_id",
            "conversation",
            ["participant_low_id"],
        )
        op.create_index(
            "ix_conversation_participant_high_id",
            "conversation",
            ["participant_high_id"],
        )

    if not _table_exists(conn, "message"):
        op.create_table(
            "message",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "conversation_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("conversation.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "sender_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("sender_nickname", sa.String(32), nullable=False),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("attachment_url", sa.String(512), nullable=True),
            sa.Column("attachment_thumb_url", sa.String(512), nullable=True),
            sa.Column("attachment_width_px", sa.Integer(), nullable=True),
            sa.Column("attachment_height_px", sa.Integer(), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("system_kind", sa.String(32), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column(
                "version", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.CheckConstraint(
                "length(body) <= 4000", name="ck_message_body_len"
            ),
        )
        op.create_index(
            "ix_message_conversation_sent",
            "message",
            ["conversation_id", "sent_at"],
        )


def downgrade():
    op.drop_index("ix_message_conversation_sent", table_name="message")
    op.drop_table("message")
    op.drop_index(
        "ix_conversation_participant_high_id", table_name="conversation"
    )
    op.drop_index(
        "ix_conversation_participant_low_id", table_name="conversation"
    )
    op.drop_table("conversation")


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = :name"
        ),
        {"name": table_name},
    )
    return result.scalar() is not None
