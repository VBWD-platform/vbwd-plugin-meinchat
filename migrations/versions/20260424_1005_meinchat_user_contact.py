"""meinchat: create user_contact table (address book).

Revision ID: 20260424_1005
Revises: 20260424_1000
Create Date: 2026-04-24
"""
from alembic import op
import sqlalchemy as sa


revision = "20260424_1005"
down_revision = "20260424_1000"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    if _table_exists(conn, "user_contact"):
        return

    op.create_table(
        "user_contact",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "owner_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contact_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(64), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "contact_user_id",
            name="uq_user_contact_owner_contact",
        ),
        sa.CheckConstraint(
            "owner_user_id <> contact_user_id",
            name="ck_user_contact_no_self",
        ),
    )
    op.create_index(
        "ix_user_contact_owner_user_id", "user_contact", ["owner_user_id"]
    )
    op.create_index(
        "ix_user_contact_contact_user_id", "user_contact", ["contact_user_id"]
    )


def downgrade():
    op.drop_index("ix_user_contact_contact_user_id", table_name="user_contact")
    op.drop_index("ix_user_contact_owner_user_id", table_name="user_contact")
    op.drop_table("user_contact")


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = :name"
        ),
        {"name": table_name},
    )
    return result.scalar() is not None
